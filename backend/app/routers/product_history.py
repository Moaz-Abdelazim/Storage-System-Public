"""
سجل الحركة التاريخي لمنتج واحد (صفحة تفاصيل المنتج): وارد + صادر + نقل + تعديل كمية + جرد، في قايمة واحدة مرقّمة من الـ DB.

مفيش نظام تسجيل جديد: ده استعلام قراءة بس على الجداول الموجودة (أسطر الوارد/الصادر + location_transfers + quantity_adjustments).

القواعد (نفس منطق النظام الحالي):
- وارد/صادر: الإذن **المكتمل** بس. عروض الأسعار والحجوزات مش حركة (بتقلل المتاح مش الفعلي) فمبتظهرش.
- الإذن الملغي (voided): الحركتين الحقيقيتين (الأصلية + العكس وقت الإلغاء) مخفيتين افتراضيًا (include_voided=0)، وبيظهروا بعلامة مع include_voided=1
  ومبيدخلوش في الإجماليات. إذن صرف اتلغى وهو quote/reserved (من غير ما يتنفذ) مش حركة أصلًا فمبيظهرش أبدًا.
- النقل: صف واحد (من ← إلى) وأثره على إجمالي المنتج صفر، فالكمية مبتتحسبش مرتين.
- تعديل الكمية / الجرد: بنستخدم الفرق (difference) بس، لأن old/new في سجلات الجرد بتعبّر عن رصيد **الموقع** مش إجمالي المنتج. الفرق صفر مش حركة.
- الرصيد قبل/بعد (إجمالي المنتج): بنحسبه رجوعًا من الرصيد الحالي الفعلي للمنتج بدالة تراكمية (window function) على كل حركات المنتج قبل أي فلتر،
  فالأرصدة صحيحة حتى لو الفلاتر مفعّلة. `opening_balance` = الرصيد قبل أول حركة مسجّلة (غير صفر لو المنتج اتفتح بكمية ابتدائية من غير إذن إضافة).
- فلتر المخزن/الموقع: الحركة اللي مكانها (أو من/إلى) جوه المخزن أو الموقع وكل اللي تحته. تعديلات الكمية اليدوية القديمة (من غير موقع مسجّل) مبتظهرش مع فلتر مكان.
- الصلاحيات (كل نوع بصلاحيته، زي /api/stock/log): in/out ← invoices.view، transfer ← transfers.view، stocktake ← stocktake.manage أو reports.view،
  adjustment ← reports.view (بيتبع إن سجل التعديلات اليدوية مش للعرض العام). الأرصدة بتظهر لمن عنده invoices.view.
"""
import math
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import Integer, and_, case, cast, func, literal_column, null, or_, select, union_all
from sqlalchemy.orm import Session, joinedload

from .. import auth, models, permissions
from ..database import get_db
from ..display_utils import format_user_display
from ..location_utils import LocationIndex, transfer_to_dict
from ..query_helpers import end_exclusive, page_args, parse_dt

router = APIRouter(prefix="/api/products", tags=["Product History"])

HISTORY_PERMS = ("invoices.view", "transfers.view", "reports.view", "stocktake.manage")
KINDS = ("in", "out", "transfer", "adjustment", "stocktake")

# rk = ترتيب النوع (للترتيب الثابت لو التاريخ اتساوى + تمييز المصدر)
RK_IN, RK_OUT, RK_TRANSFER, RK_ADJ, RK_IN_VOID, RK_OUT_VOID = 0, 1, 2, 3, 4, 5
_LOC_CHUNK = 1000


def _null_int():
    return cast(null(), Integer)


def _product_union(pid: int):
    """كل حركات المنتج في UNION ALL بأعمدة موحّدة: rk, ref_id, doc_id, date, delta, qty, loc, frm, to, voided, sub."""
    I, II = models.StockInItem, models.StockInInvoice
    O, OI = models.StockOutItem, models.StockOutInvoice
    T, A = models.LocationTransfer, models.QuantityAdjustment

    def sel(rk, ref, doc, date, delta, qty, loc, frm, to, voided, sub):
        return select(
            literal_column(str(rk)).label("rk"), ref.label("ref_id"), doc.label("doc_id"), date.label("date"),
            delta.label("delta"), qty.label("qty"), loc.label("loc"), frm.label("frm"), to.label("to_loc"),
            literal_column(str(voided)).label("voided"), literal_column(str(sub)).label("sub"),
        )

    zero = literal_column("0")
    parts = [
        # وارد مكتمل / ملغي (الأصلية + العكس وقت الإلغاء)
        sel(RK_IN, I.id, II.id, II.date, I.quantity, I.quantity, I.location_id, _null_int(), _null_int(), 0, 0)
        .select_from(I.__table__.join(II.__table__, II.id == I.invoice_id)).where(I.product_id == pid, II.status == "completed"),
        sel(RK_IN, I.id, II.id, II.date, I.quantity, I.quantity, I.location_id, _null_int(), _null_int(), 1, 0)
        .select_from(I.__table__.join(II.__table__, II.id == I.invoice_id)).where(I.product_id == pid, II.status == "voided"),
        sel(RK_IN_VOID, I.id, II.id, II.voided_at, -I.quantity, I.quantity, I.location_id, _null_int(), _null_int(), 1, 0)
        .select_from(I.__table__.join(II.__table__, II.id == I.invoice_id)).where(I.product_id == pid, II.status == "voided", II.voided_at.is_not(None)),
        # صادر مكتمل / ملغي بعد التنفيذ (completed_at موجود) - الملغي وهو quote/reserved مفيهوش حركة
        sel(RK_OUT, O.id, OI.id, OI.date, -O.quantity, O.quantity, O.location_id, _null_int(), _null_int(), 0, 0)
        .select_from(O.__table__.join(OI.__table__, OI.id == O.invoice_id)).where(O.product_id == pid, OI.status == "completed"),
        sel(RK_OUT, O.id, OI.id, OI.date, -O.quantity, O.quantity, O.location_id, _null_int(), _null_int(), 1, 0)
        .select_from(O.__table__.join(OI.__table__, OI.id == O.invoice_id)).where(
            O.product_id == pid, OI.status == "voided", OI.completed_at.is_not(None)),
        sel(RK_OUT_VOID, O.id, OI.id, OI.voided_at, O.quantity, O.quantity, O.location_id, _null_int(), _null_int(), 1, 0)
        .select_from(O.__table__.join(OI.__table__, OI.id == O.invoice_id)).where(
            O.product_id == pid, OI.status == "voided", OI.completed_at.is_not(None), OI.voided_at.is_not(None)),
        # نقل: أثره على إجمالي المنتج صفر
        sel(RK_TRANSFER, T.id, _null_int(), T.date, zero, T.quantity, _null_int(), T.from_location_id, T.to_location_id, 0, 0)
        .select_from(T.__table__).where(T.product_id == pid),
        # تعديل كمية / جرد (الفرق بس؛ الفرق صفر مش حركة). sub = 1 لو من جلسة جرد
        select(
            literal_column(str(RK_ADJ)).label("rk"), A.id.label("ref_id"), _null_int().label("doc_id"), A.date.label("date"),
            A.difference.label("delta"), func.abs(A.difference).label("qty"), A.location_id.label("loc"),
            _null_int().label("frm"), _null_int().label("to_loc"), literal_column("0").label("voided"),
            case((A.stocktake_id.is_not(None), 1), else_=0).label("sub"),
        ).select_from(A.__table__).where(A.product_id == pid, A.difference != 0),
    ]
    return union_all(*parts).subquery("m")


def _in_chunks(column, ids):
    ids = sorted(int(i) for i in ids)
    if not ids:
        return literal_column("1 = 0")
    return or_(*[column.in_(ids[i:i + _LOC_CHUNK]) for i in range(0, len(ids), _LOC_CHUNK)])


def _where_of(idx: LocationIndex, location_id: Optional[int]) -> Optional[dict]:
    loc = idx.locations.get(location_id) if location_id else None
    if loc is None:
        return None
    wh = idx.warehouses.get(loc.warehouse_id)
    return {"location_id": loc.id, "code": idx.full_code(loc.id), "warehouse_id": wh.id if wh else None,
            "warehouse_name": wh.name if wh else None}


@router.get("/{product_id}/history")
def product_history(
    product_id: int,
    kind: Optional[str] = None,        # in / out / transfer / adjustment / stocktake (من غيره = الكل)
    warehouse_id: Optional[int] = None,
    location_id: Optional[int] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    include_voided: int = 0,
    order: str = "desc",
    page: int = 1,
    page_size: int = 15,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(auth.require_any_permission(*HISTORY_PERMS)),
):
    product = db.query(models.Product).filter(models.Product.id == product_id).first()
    if not product:
        raise HTTPException(status_code=404, detail="المنتج مش موجود")
    if kind not in (None, "", "all") and kind not in KINDS:
        raise HTTPException(status_code=400, detail="نوع الحركة لازم يكون in أو out أو transfer أو adjustment أو stocktake")
    if order not in ("asc", "desc"):
        raise HTTPException(status_code=400, detail="الترتيب لازم يكون asc أو desc")
    start, end_excl = parse_dt(date_from, "تاريخ البداية"), end_exclusive(date_to)
    pg, size = page_args(page, page_size, default=15)

    has = lambda k: permissions.has_permission(current_user, k)  # noqa: E731
    allowed = {
        "in": has("invoices.view"), "out": has("invoices.view"), "transfer": has("transfers.view"),
        "adjustment": has("reports.view"), "stocktake": has("stocktake.manage") or has("reports.view"),
    }
    wanted = [k for k in KINDS if allowed[k] and kind in (None, "", "all", k)]
    balances_available = has("invoices.view")

    idx = LocationIndex(db)
    scope = idx.scope_ids(warehouse_id, location_id)

    m = _product_union(product_id)
    cum = func.sum(m.c.delta).over(order_by=[m.c.date, m.c.rk, m.c.ref_id], rows=(None, 0)).label("cum")
    b = select(*m.c, cum).subquery("b")

    rk, sub = b.c.rk, b.c.sub
    kind_clause = {
        "in": rk.in_([RK_IN, RK_IN_VOID]), "out": rk.in_([RK_OUT, RK_OUT_VOID]), "transfer": rk == RK_TRANSFER,
        "adjustment": and_(rk == RK_ADJ, sub == 0), "stocktake": and_(rk == RK_ADJ, sub == 1),
    }
    conds = [or_(*[kind_clause[k] for k in wanted])] if wanted else [literal_column("1 = 0")]
    if not include_voided:
        conds.append(b.c.voided == 0)
    if start:
        conds.append(b.c.date >= start)
    if end_excl:
        conds.append(b.c.date < end_excl)
    if scope is not None:
        conds.append(or_(_in_chunks(b.c.loc, scope), _in_chunks(b.c.frm, scope), _in_chunks(b.c.to_loc, scope)))

    # ---- إجماليات الفلتر + إجمالي كل حركات المنتج (للرصيد الافتتاحي) ----
    ok = b.c.voided == 0
    agg = db.execute(
        select(
            func.count(),
            func.coalesce(func.sum(case((and_(ok, rk == RK_IN), b.c.qty), else_=0)), 0),
            func.coalesce(func.sum(case((and_(ok, rk == RK_OUT), b.c.qty), else_=0)), 0),
            func.coalesce(func.sum(case((and_(ok, rk == RK_TRANSFER), b.c.qty), else_=0)), 0),
            func.coalesce(func.sum(case((and_(ok, rk == RK_ADJ), b.c.delta), else_=0)), 0),
            func.coalesce(func.sum(case((ok, b.c.delta), else_=0)), 0),
        ).select_from(b).where(*conds)
    ).one()
    total = int(agg[0] or 0)
    all_delta = float(db.execute(select(func.coalesce(func.sum(m.c.delta), 0)).select_from(m)).scalar() or 0)
    current = float(product.quantity or 0)

    order_cols = (b.c.date.desc(), b.c.rk.desc(), b.c.ref_id.desc()) if order == "desc" else (b.c.date.asc(), b.c.rk.asc(), b.c.ref_id.asc())
    rows = db.execute(select(b).where(*conds).order_by(*order_cols).limit(size).offset((pg - 1) * size)).all() if total else []

    # ---- تفاصيل الصفحة الحالية بس ----
    ids = {r: [x.ref_id for x in rows if x.rk == r] for r in range(6)}
    details = {}
    for rks, Item, Inv, party, user_rel, snap in (
        ((RK_IN, RK_IN_VOID), models.StockInItem, models.StockInInvoice, "supplier", "received_by", "received_by_username"),
        ((RK_OUT, RK_OUT_VOID), models.StockOutItem, models.StockOutInvoice, "recipient", "delivered_by", "delivered_by_username"),
    ):
        item_ids = [i for r in rks for i in ids[r]]
        if not item_ids:
            continue
        for it in (
            db.query(Item).options(joinedload(Item.invoice).joinedload(getattr(Inv, party)), joinedload(Item.invoice).joinedload(getattr(Inv, user_rel)),
                                   joinedload(Item.invoice).joinedload(Inv.voided_by))
            .filter(Item.id.in_(item_ids)).all()
        ):
            inv = it.invoice
            p = getattr(inv, party)
            reverse = inv.status == "voided"
            details[(Item, it.id)] = {
                "document_id": inv.id, "party_name": p.name if p else None,
                "user_name": format_user_display(getattr(inv, user_rel), getattr(inv, snap)),
                "notes": inv.notes, "status": inv.status or "completed",
                "void_reason": inv.void_reason if reverse else None,
                "voided_by": format_user_display(inv.voided_by, inv.voided_by_username) if reverse else None,
                "voided_at": inv.voided_at if reverse else None,
            }
    if ids[RK_TRANSFER]:
        T = models.LocationTransfer
        for t in db.query(T).options(joinedload(T.moved_by)).filter(T.id.in_(ids[RK_TRANSFER])).all():
            d = transfer_to_dict(t, idx)
            details[(T, t.id)] = {"user_name": d["moved_by"], "notes": d["notes"], "document_id": None, "status": "completed"}
    if ids[RK_ADJ]:
        A = models.QuantityAdjustment
        for a in db.query(A).options(joinedload(A.changed_by)).filter(A.id.in_(ids[RK_ADJ])).all():
            details[(A, a.id)] = {"user_name": format_user_display(a.changed_by, a.changed_by_username), "notes": a.reason,
                                  "document_id": None, "status": "completed", "stocktake_id": a.stocktake_id}

    model_of = {RK_IN: models.StockInItem, RK_IN_VOID: models.StockInItem, RK_OUT: models.StockOutItem, RK_OUT_VOID: models.StockOutItem,
                RK_TRANSFER: models.LocationTransfer, RK_ADJ: models.QuantityAdjustment}
    items = []
    for r in rows:
        d = details.get((model_of[r.rk], r.ref_id), {})
        if r.rk in (RK_IN, RK_IN_VOID):
            k = "in"
        elif r.rk in (RK_OUT, RK_OUT_VOID):
            k = "out"
        elif r.rk == RK_TRANSFER:
            k = "transfer"
        else:
            k = "stocktake" if r.sub == 1 else "adjustment"
        delta = round(float(r.delta or 0), 3)
        after = round(current - all_delta + float(r.cum or 0), 3)
        item = {
            "key": f"{r.rk}-{r.ref_id}", "kind": k, "is_reversal": r.rk in (RK_IN_VOID, RK_OUT_VOID), "voided": bool(r.voided),
            "date": r.date, "quantity": round(float(r.qty or 0), 3), "delta": delta,
            "balance_before": round(after - delta, 3) if balances_available else None,
            "balance_after": after if balances_available else None,
            "location": _where_of(idx, r.loc), "from": _where_of(idx, r.frm), "to": _where_of(idx, r.to_loc),
            "document_id": d.get("document_id"), "party_name": d.get("party_name"), "user_name": d.get("user_name"),
            "notes": d.get("notes"), "status": d.get("status"), "void_reason": d.get("void_reason"),
            "voided_by": d.get("voided_by"), "voided_at": d.get("voided_at"), "stocktake_id": d.get("stocktake_id"),
        }
        items.append(item)

    return {
        "items": items, "total": total, "page": pg, "page_size": size,
        "total_pages": max(1, math.ceil(total / size)) if total else 1,
        "summary": {
            "in_quantity": round(float(agg[1]), 3), "out_quantity": round(float(agg[2]), 3),
            "transfers_quantity": round(float(agg[3]), 3), "adjustments_net": round(float(agg[4]), 3),
            "net_change": round(float(agg[5]), 3),
        },
        "current_quantity": round(current, 3),
        "opening_balance": round(current - all_delta, 3) if balances_available else None,
        "balances_available": balances_available,
    }

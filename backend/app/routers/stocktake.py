"""
الجرد الدوري. الدورة: draft (إنشاء + لقطة بالأرصدة) ← counting (إدخال/استيراد الكميات المعدودة) ← review (مراجعة الفروقات)
← approved (التسوية الفعلية). أو cancelled. التسوية بتحصل وقت الاعتماد بس.

قواعد:
- لقطة النظام (system_qty) بتتاخد وقت الإنشاء. وقت الاعتماد بنطبّق الفرق (المعدود - لقطة النظام) على **الرصيد الحالي** للمكان،
  فالحركات اللي حصلت أثناء العدّ (وارد/صادر/نقل) ما بتضيعش. أي سطر لسه ماتعدّش (counted_qty = NULL) بيتساب زي ما هو.
- الرصيد الجديد مينفعش يبقى سالب ولا أقل من المحجوز في المكان (409 برسالة فيها كل الأسطر المتأثرة، ومفيش حاجة بتتغيّر).
- الاعتماد ذرّي: UPDATE ... WHERE status='review' (rowcount=1 وإلا 409) فمفيش اعتماد مزدوج، وكل شيء في transaction واحدة.
- كل فرق بيعمل QuantityAdjustment (سبب: جرد #id) + سجل تدقيق. القيم المالية بتظهر بـ finance.view بس.
"""
import io
from typing import List, Optional

import pandas as pd
from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import and_, func, insert, update
from sqlalchemy.orm import Session

from .. import auth, models, permissions
from ..database import get_db
from ..location_utils import LocationIndex, lock_products
from ..money import q3
from ..query_helpers import contains, page_args, paged
from ..services import inventory
from ..services.audit import audit
from ..timezone_utils import now_cairo
from . import excel_io

router = APIRouter(prefix="/api/stocktakes", tags=["Stocktake"])

MAX_LINES = 50_000
MAX_COUNTS_PER_CALL = 1000
EDITABLE = ("draft", "counting")
STATUS_LABEL = {"draft": "مسودة", "counting": "جاري العدّ", "review": "مراجعة", "approved": "معتمد", "cancelled": "ملغي"}
S, SL = models.Stocktake, models.StocktakeLine


class StocktakeCreate(BaseModel):
    warehouse_id: Optional[int] = None
    location_id: Optional[int] = None
    note: Optional[str] = Field(default=None, max_length=500)


class CountIn(BaseModel):
    line_id: int
    counted_qty: Optional[float] = Field(default=None, ge=0, le=1e9)


class CountsIn(BaseModel):
    counts: List[CountIn] = Field(max_length=MAX_COUNTS_PER_CALL)


class LineIn(BaseModel):
    product_id: int
    location_id: int
    counted_qty: float = Field(ge=0, le=1e9)


def _get(db: Session, stocktake_id: int) -> models.Stocktake:
    st = db.get(S, stocktake_id)
    if not st:
        raise HTTPException(status_code=404, detail="جلسة الجرد مش موجودة")
    return st


def _transition(db: Session, st: models.Stocktake, allowed: tuple, new: str, **extra) -> None:
    """انتقال حالة ذرّي: UPDATE ... WHERE status IN (allowed). rowcount != 1 يعني حد غيّرها قبلنا (409)."""
    res = db.execute(
        update(S).where(S.id == st.id, S.status.in_(allowed)).values(status=new, **extra),
        execution_options={"synchronize_session": False},
    )
    if res.rowcount != 1:
        db.rollback()
        raise HTTPException(status_code=409, detail="حالة الجرد اتغيّرت - حدّث الصفحة وجرّب تاني")
    db.refresh(st)


def _summary(db: Session, st_id: int, show_value: bool) -> dict:
    base = (SL.stocktake_id == st_id,)
    diff = SL.counted_qty - SL.system_qty
    counted_ok = SL.counted_qty.isnot(None)
    total = db.query(func.count(SL.id)).filter(*base).scalar() or 0
    counted = db.query(func.count(SL.id)).filter(*base, counted_ok).scalar() or 0
    diff_lines = db.query(func.count(SL.id)).filter(*base, counted_ok, SL.counted_qty != SL.system_qty).scalar() or 0
    net, net_value = db.query(
        func.coalesce(func.sum(diff), 0), func.coalesce(func.sum(diff * func.coalesce(SL.unit_price, 0)), 0)
    ).filter(*base, counted_ok).first()
    surplus = db.query(func.coalesce(func.sum(diff), 0)).filter(*base, counted_ok, diff > 0).scalar() or 0
    shortage = db.query(func.coalesce(func.sum(-diff), 0)).filter(*base, counted_ok, diff < 0).scalar() or 0
    return {
        "lines_total": int(total), "counted": int(counted), "uncounted": int(total) - int(counted), "diff_lines": int(diff_lines),
        "surplus_qty": round(float(surplus), 3), "shortage_qty": round(float(shortage), 3), "net_qty": round(float(net or 0), 3),
        "net_value": round(float(net_value or 0), 2) if show_value else None,  # بأسعار البيع وقت إنشاء الجرد
    }


def _header(st: models.Stocktake, idx: LocationIndex) -> dict:
    scope = None
    if st.location_id and st.location_id in idx.locations:
        scope = idx.full_code(st.location_id)
    elif st.warehouse_id and st.warehouse_id in idx.warehouses:
        scope = idx.warehouses[st.warehouse_id].code
    return {
        "id": st.id, "status": st.status, "status_label": STATUS_LABEL.get(st.status, st.status), "note": st.note,
        "warehouse_id": st.warehouse_id, "location_id": st.location_id, "scope": scope,
        "created_by": st.created_by_username, "created_at": st.created_at,
        "approved_by": st.approved_by_username, "approved_at": st.approved_at,
    }


@router.post("", status_code=201)
def create_stocktake(
    payload: StocktakeCreate,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(auth.require_permission("stocktake.manage")),
):
    """إنشاء جلسة جرد لمخزن أو موقع (بكل اللي تحته) + لقطة بأرصدة كل المنتجات الموجودة فيه (الأرصدة > 0)."""
    if bool(payload.warehouse_id) == bool(payload.location_id):
        raise HTTPException(status_code=400, detail="حدد مخزن أو موقع (واحد بس منهم)")
    idx = LocationIndex(db)
    if payload.warehouse_id and payload.warehouse_id not in idx.warehouses:
        raise HTTPException(status_code=404, detail="المخزن مش موجود")
    if payload.location_id and payload.location_id not in idx.locations:
        raise HTTPException(status_code=404, detail="الموقع مش موجود")
    scope = idx.scope_ids(payload.warehouse_id, payload.location_id)
    PL, P = models.ProductLocation, models.Product
    rows = (
        db.query(PL.product_id, PL.location_id, PL.quantity, P.price, P.name)
        .join(P, P.id == PL.product_id)
        .filter(PL.location_id.in_(scope), PL.quantity > 0)
        .all()
    )
    if len(rows) > MAX_LINES:
        raise HTTPException(status_code=400, detail=f"الجرد فيه أسطر كتير ({len(rows)}) - اختار موقع أصغر (الحد {MAX_LINES})")
    rows.sort(key=lambda r: (idx.full_code(r[1]), r[4] or "", r[0]))
    st = S(warehouse_id=payload.warehouse_id, location_id=payload.location_id, status="draft",
           note=(payload.note or "").strip() or None, created_by_id=current_user.id, created_by_username=current_user.username)
    db.add(st)
    db.flush()
    if rows:
        db.execute(insert(SL), [
            {"stocktake_id": st.id, "product_id": r[0], "location_id": r[1], "system_qty": r[2], "counted_qty": None, "unit_price": r[3]}
            for r in rows
        ])
    audit(db, current_user, "stocktake", st.id, "create", {"scope": _header(st, idx)["scope"], "lines": len(rows)})
    db.commit()
    db.refresh(st)
    return {**_header(st, idx), "lines_total": len(rows)}


@router.get("")
def list_stocktakes(
    status: Optional[str] = None, page: int = 1, page_size: int = 15,
    db: Session = Depends(get_db), current_user: models.User = Depends(auth.require_permission("stocktake.manage")),
):
    idx = LocationIndex(db)
    q = db.query(S)
    if status in STATUS_LABEL:
        q = q.filter(S.status == status)
    pg, size = page_args(page, page_size, default=15)
    total = q.count()
    rows = q.order_by(S.id.desc()).offset((pg - 1) * size).limit(size).all()
    ids = [r.id for r in rows]
    counts = {}
    if ids:
        for sid, n, c in db.query(SL.stocktake_id, func.count(SL.id), func.count(SL.counted_qty)).filter(SL.stocktake_id.in_(ids)).group_by(SL.stocktake_id).all():
            counts[sid] = (n, c)
    items = [{**_header(r, idx), "lines_total": counts.get(r.id, (0, 0))[0], "counted": counts.get(r.id, (0, 0))[1]} for r in rows]
    return paged(items, total, pg, size)


@router.get("/{stocktake_id}")
def get_stocktake(
    stocktake_id: int, page: int = 1, page_size: int = 50, search: Optional[str] = None,
    only_diff: int = 0, only_uncounted: int = 0,
    db: Session = Depends(get_db), current_user: models.User = Depends(auth.require_permission("stocktake.manage")),
):
    st = _get(db, stocktake_id)
    idx = LocationIndex(db)
    show_value = permissions.has_permission(current_user, "finance.view")
    P = models.Product
    q = db.query(SL, P).join(P, P.id == SL.product_id).filter(SL.stocktake_id == st.id)
    if search and search.strip():
        q = q.filter(contains(P.name, search) | contains(P.sku, search) | contains(P.barcode, search))
    if only_diff:
        q = q.filter(SL.counted_qty.isnot(None), SL.counted_qty != SL.system_qty)
    if only_uncounted:
        q = q.filter(SL.counted_qty.is_(None))
    pg, size = page_args(page, page_size, default=50, max_value=200)
    total = q.count()
    rows = q.order_by(SL.id).offset((pg - 1) * size).limit(size).all()
    items = []
    for line, p in rows:
        diff = None if line.counted_qty is None else round(line.counted_qty - line.system_qty, 3)
        items.append({
            "line_id": line.id, "product_id": p.id, "product_name": p.name, "sku": p.sku, "barcode": p.barcode, "unit": p.unit,
            "location_id": line.location_id, "location_code": idx.full_code(line.location_id) if line.location_id in idx.locations else None,
            "system_qty": line.system_qty, "counted_qty": line.counted_qty, "diff": diff,
            "unit_price": line.unit_price if show_value else None,
            "diff_value": round(diff * (line.unit_price or 0), 2) if (diff is not None and show_value) else None,
        })
    return paged(items, total, pg, size, stocktake=_header(st, idx), summary=_summary(db, st.id, show_value))


@router.put("/{stocktake_id}/counts")
def set_counts(
    stocktake_id: int, payload: CountsIn,
    db: Session = Depends(get_db), current_user: models.User = Depends(auth.require_permission("stocktake.manage")),
):
    """إدخال/تعديل الكميات المعدودة (دفعة حتى 1000 سطر). counted_qty=null يمسح العدّ. أول إدخال بيحوّل الحالة لـ counting."""
    st = _get(db, stocktake_id)
    if st.status not in EDITABLE:
        raise HTTPException(status_code=409, detail="الجرد مقفول للتعديل (ارجعه للعدّ الأول)")
    want = {c.line_id: c.counted_qty for c in payload.counts}
    lines = {l.id: l for l in db.query(SL).filter(SL.stocktake_id == st.id, SL.id.in_(list(want))).all()} if want else {}
    missing = [i for i in want if i not in lines]
    if missing:
        raise HTTPException(status_code=400, detail=f"أسطر مش تابعة للجرد ده: {missing[:5]}")
    for lid, qty in want.items():
        lines[lid].counted_qty = None if qty is None else float(q3(qty))
    if st.status == "draft" and want:
        st.status = "counting"
    db.commit()
    return {"updated": len(want), "status": st.status}


@router.post("/{stocktake_id}/lines", status_code=201)
def add_line(
    stocktake_id: int, payload: LineIn,
    db: Session = Depends(get_db), current_user: models.User = Depends(auth.require_permission("stocktake.manage")),
):
    """إضافة صنف لقيته في مكان من نطاق الجرد ومكنش ليه رصيد (رصيد النظام 0)."""
    st = _get(db, stocktake_id)
    if st.status not in EDITABLE:
        raise HTTPException(status_code=409, detail="الجرد مقفول للتعديل")
    idx = LocationIndex(db)
    scope = idx.scope_ids(st.warehouse_id, st.location_id)
    if payload.location_id not in idx.locations or payload.location_id not in scope:
        raise HTTPException(status_code=400, detail="المكان ده خارج نطاق الجرد")
    if not idx.is_leaf(payload.location_id):
        raise HTTPException(status_code=400, detail="حدد المكان النهائي")
    p = db.get(models.Product, payload.product_id)
    if not p:
        raise HTTPException(status_code=404, detail="المنتج مش موجود")
    if db.query(SL.id).filter(SL.stocktake_id == st.id, SL.product_id == p.id, SL.location_id == payload.location_id).first():
        raise HTTPException(status_code=409, detail="الصنف ده موجود بالفعل في الجرد في المكان ده")
    line = SL(stocktake_id=st.id, product_id=p.id, location_id=payload.location_id, system_qty=0.0,
              counted_qty=float(q3(payload.counted_qty)), unit_price=p.price)
    db.add(line)
    if st.status == "draft":
        st.status = "counting"
    db.commit()
    return {"line_id": line.id}


@router.post("/{stocktake_id}/submit")
def submit_for_review(
    stocktake_id: int,
    db: Session = Depends(get_db), current_user: models.User = Depends(auth.require_permission("stocktake.manage")),
):
    st = _get(db, stocktake_id)
    if not db.query(SL.id).filter(SL.stocktake_id == st.id, SL.counted_qty.isnot(None)).first():
        raise HTTPException(status_code=400, detail="مفيش أي كمية معدودة - أدخل العدّ الأول")
    _transition(db, st, EDITABLE, "review")
    audit(db, current_user, "stocktake", st.id, "submit", None)
    db.commit()
    return {"status": st.status}


@router.post("/{stocktake_id}/reopen")
def reopen(
    stocktake_id: int,
    db: Session = Depends(get_db), current_user: models.User = Depends(auth.require_permission("stocktake.manage")),
):
    st = _get(db, stocktake_id)
    _transition(db, st, ("review",), "counting")
    audit(db, current_user, "stocktake", st.id, "reopen", None)
    db.commit()
    return {"status": st.status}


@router.post("/{stocktake_id}/cancel")
def cancel(
    stocktake_id: int,
    db: Session = Depends(get_db), current_user: models.User = Depends(auth.require_permission("stocktake.manage")),
):
    st = _get(db, stocktake_id)
    _transition(db, st, ("draft", "counting", "review"), "cancelled")
    audit(db, current_user, "stocktake", st.id, "cancel", None)
    db.commit()
    return {"status": st.status}


# ---------------------------------------------------------------- Excel
_EXPORT_COLS = ["رقم السطر", "المنتج", "SKU", "الباركود", "الموقع", "الكمية بالنظام", "الكمية المعدودة"]


@router.get("/{stocktake_id}/export")
def export_sheet(
    stocktake_id: int,
    db: Session = Depends(get_db), current_user: models.User = Depends(auth.require_permission("stocktake.manage")),
):
    """كشف جرد Excel: املا عمود (الكمية المعدودة) وارفعه تاني. الاستيراد بيطابق بـ (رقم السطر)."""
    st = _get(db, stocktake_id)
    idx = LocationIndex(db)
    P = models.Product
    rows = db.query(SL, P).join(P, P.id == SL.product_id).filter(SL.stocktake_id == st.id).order_by(SL.id).limit(excel_io.MAX_EXPORT_ROWS).all()
    data = [
        {"رقم السطر": l.id, "المنتج": p.name, "SKU": p.sku, "الباركود": p.barcode,
         "الموقع": idx.full_code(l.location_id) if l.location_id in idx.locations else None,
         "الكمية بالنظام": l.system_qty, "الكمية المعدودة": l.counted_qty}
        for l, p in rows
    ]
    df = pd.DataFrame(data, columns=_EXPORT_COLS)
    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as writer:
        df.pipe(excel_io._safe_df).to_excel(writer, index=False, sheet_name="الجرد")
    buf.seek(0)
    return StreamingResponse(
        buf, media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f"attachment; filename=stocktake_{st.id}.xlsx"},
    )


@router.post("/{stocktake_id}/import")
def import_counts(
    stocktake_id: int, file: UploadFile = File(...),
    db: Session = Depends(get_db), current_user: models.User = Depends(auth.require_permission("stocktake.manage")),
):
    """استيراد الكميات المعدودة من كشف الجرد. أخطاء الصفوف بشكل {row, column, message}؛ الصفوف السليمة بتتحفظ."""
    st = _get(db, stocktake_id)
    if st.status not in EDITABLE:
        raise HTTPException(status_code=409, detail="الجرد مقفول للتعديل")
    if not (file.filename or "").lower().endswith(".xlsx"):
        raise HTTPException(status_code=400, detail="لازم الملف يكون Excel بصيغة .xlsx")
    content = file.file.read(excel_io.MAX_UPLOAD_BYTES + 1)
    if len(content) > excel_io.MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail="الملف كبير جدًا (الحد الأقصى 10 ميجا)")
    excel_io._validate_xlsx_container(content)
    try:
        df = pd.read_excel(io.BytesIO(content), dtype=str)
    except Exception:
        raise HTTPException(status_code=400, detail="مقدرش أقرأ الملف، اتأكد إنه شيت اكسل سليم")
    if len(df) > MAX_LINES:
        raise HTTPException(status_code=400, detail="الملف فيه صفوف كتير")
    df.columns = [str(c).strip() for c in df.columns]
    if "رقم السطر" not in df.columns or "الكمية المعدودة" not in df.columns:
        raise HTTPException(status_code=400, detail="الملف لازم يحتوي على عمودي (رقم السطر) و(الكمية المعدودة) - نزّل كشف الجرد من الصفحة")
    errors = excel_io._ImportErrors()
    lines = {l.id: l for l in db.query(SL).filter(SL.stocktake_id == st.id).all()}
    applied = 0
    seen = set()
    for i, row in df.iterrows():
        rn = i + 2
        lid_raw, qty_raw = excel_io._clean(row.get("رقم السطر")), row.get("الكمية المعدودة")
        if lid_raw is None:
            errors.add(rn, "رقم السطر", "رقم السطر فاضي")
            continue
        try:
            lid = int(float(lid_raw))
        except (ValueError, TypeError):
            errors.add(rn, "رقم السطر", "رقم السطر غير صالح")
            continue
        line = lines.get(lid)
        if line is None:
            errors.add(rn, "رقم السطر", f"السطر ({lid}) مش تابع للجرد ده")
            continue
        if lid in seen:
            errors.add(rn, "رقم السطر", f"السطر ({lid}) مكرر في الملف")
            continue
        seen.add(lid)
        if pd.isna(qty_raw) or not str(qty_raw).strip():
            continue  # فاضي = سيبه زي ما هو
        qty = excel_io._strict_num(qty_raw)
        if qty is None or qty < 0 or qty > 1e9:
            errors.add(rn, "الكمية المعدودة", "الكمية لازم تكون رقم صحيح وموجب")
            continue
        line.counted_qty = float(q3(qty))
        applied += 1
    if st.status == "draft" and applied:
        st.status = "counting"
    audit(db, current_user, "stocktake", st.id, "import_counts", {"applied": applied, "errors": errors.total})
    db.commit()
    return {"applied": applied, "errors": errors.items, "errors_total": errors.total, "errors_truncated": errors.truncated, "status": st.status}


# ---------------------------------------------------------------- الاعتماد
@router.post("/{stocktake_id}/approve")
def approve(
    stocktake_id: int,
    db: Session = Depends(get_db), current_user: models.User = Depends(auth.require_permission("stocktake.approve")),
):
    """
    اعتماد الجرد وتسوية الأرصدة (set_location_quantity) في transaction واحدة. بيرفض (409) لو أي سطر هيخلّي الرصيد سالب أو أقل من
    المحجوز، برسالة فيها كل الأسطر المتأثرة. الاعتماد المزدوج بيتمنع بشرط الحالة في الـ UPDATE نفسه.
    """
    def _tx():
        st = _get(db, stocktake_id)
        _transition(db, st, ("review",), "approved", approved_by_id=current_user.id,
                    approved_by_username=current_user.username, approved_at=now_cairo())
        changed = (
            db.query(SL).filter(SL.stocktake_id == st.id, SL.counted_qty.isnot(None), SL.counted_qty != SL.system_qty)
            .order_by(SL.product_id, SL.location_id).all()
        )
        lock_products(db, sorted({l.product_id for l in changed}))
        plan, problems = [], []
        names = {p.id: p.name for p in db.query(models.Product.id, models.Product.name).filter(
            models.Product.id.in_({l.product_id for l in changed})).all()} if changed else {}
        idx = LocationIndex(db)
        for l in changed:
            on_hand, reserved = inventory.location_balances(db, l.product_id, l.location_id)
            target = on_hand + q3(l.counted_qty - l.system_qty)
            code = idx.full_code(l.location_id) if l.location_id in idx.locations else str(l.location_id)
            if target < 0:
                problems.append(f"({names.get(l.product_id)}) في {code}: الرصيد الحالي {float(on_hand):g} والفرق {l.counted_qty - l.system_qty:+g} هيخلّيه سالب")
            elif target < reserved:
                problems.append(f"({names.get(l.product_id)}) في {code}: الرصيد الجديد {float(target):g} أقل من المحجوز {float(reserved):g}")
            else:
                plan.append((l, target))
        if problems:
            db.rollback()
            raise HTTPException(status_code=409, detail="مينفعش نعتمد الجرد: " + "؛ ".join(problems))
        old_totals = {pid: float(inventory.product_total(db, pid)) for pid in {l.product_id for l, _ in plan}}
        for l, target in plan:
            inventory.set_location_quantity(db, l.product_id, l.location_id, target)
        shortage = surplus = 0.0
        for pid, old in old_totals.items():
            new = float(inventory.product_total(db, pid))
            if new != old:
                db.add(models.QuantityAdjustment(
                    product_id=pid, old_quantity=old, new_quantity=new, difference=round(new - old, 3),
                    changed_by_id=current_user.id, changed_by_username=current_user.username,
                    reason=f"جرد #{st.id}", date=now_cairo(),
                ))
        for l, _t in plan:
            d = l.counted_qty - l.system_qty
            surplus += max(d, 0)
            shortage += max(-d, 0)
        audit(db, current_user, "stocktake", st.id, "approve",
              {"lines_adjusted": len(plan), "surplus_qty": round(surplus, 3), "shortage_qty": round(shortage, 3)})
        db.commit()
        db.refresh(st)
        return st

    st = inventory.with_retry(db, _tx)
    return {"status": st.status, "approved_by": st.approved_by_username, "approved_at": st.approved_at}

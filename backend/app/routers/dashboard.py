"""
لوحة القيادة: endpoint واحد (/api/dashboard/overview) بيرجّع كل اللي الصفحة الرئيسية محتاجاه بدل ما الواجهة
تعمل 7+ طلبات، وكل الحسابات بتتعمل بـ SQL مجمّع (GROUP BY) مش بتحميل صفوف في الذاكرة.
الأرقام المالية (قيمة المخزون / إجمالي الصادر) بتترجع للأدمن بس.
"""
from datetime import datetime, time, timedelta
from typing import Dict, List, Optional

from fastapi import APIRouter, Depends
from sqlalchemy import func
from sqlalchemy.orm import Session, joinedload

from .. import models, auth, permissions
from ..database import get_db
from ..display_utils import format_user_display
from ..location_utils import LocationIndex
from ..query_helpers import invoice_totals
from ..timezone_utils import now_cairo

router = APIRouter(prefix="/api/dashboard", tags=["Dashboard"])


def _day_key(value) -> str:
    return str(value)[:10]  # SQLite بيرجّع نص 'YYYY-MM-DD' و MySQL بيرجّع date


def _flow(db: Session, days: int, with_values: bool) -> List[dict]:
    today = now_cairo().date()
    first = today - timedelta(days=days - 1)
    start = datetime.combine(first, time.min)
    end = datetime.combine(today + timedelta(days=1), time.min)

    def aggregate(inv_model, item_model) -> Dict[str, tuple]:
        day = func.date(inv_model.date)
        cols = [day, func.coalesce(func.sum(item_model.quantity), 0), func.count(func.distinct(inv_model.id))]
        if with_values:
            cols.append(func.coalesce(func.sum(item_model.quantity * func.coalesce(item_model.unit_price, 0)), 0))
        rows = (
            db.query(*cols)
            .select_from(inv_model)
            .join(item_model, item_model.invoice_id == inv_model.id)
            .filter(inv_model.date >= start, inv_model.date < end)
            .group_by(day)
            .all()
        )
        return {_day_key(r[0]): r[1:] for r in rows}

    ins = aggregate(models.StockInInvoice, models.StockInItem)
    outs = aggregate(models.StockOutInvoice, models.StockOutItem)

    result = []
    for i in range(days):
        d = (first + timedelta(days=i)).isoformat()
        a, b = ins.get(d), outs.get(d)
        row = {
            "date": d,
            "in_qty": round(float(a[0]), 3) if a else 0,
            "in_count": int(a[1]) if a else 0,
            "out_qty": round(float(b[0]), 3) if b else 0,
            "out_count": int(b[1]) if b else 0,
        }
        if with_values:
            row["in_value"] = round(float(a[2]), 2) if a else 0
            row["out_value"] = round(float(b[2]), 2) if b else 0
        result.append(row)
    return result


@router.get("/flow")
def flow(
    days: int = 7,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(auth.get_current_user),
):
    days = max(1, min(days, 90))
    return {"days": _flow(db, days, with_values=permissions.has_permission(current_user, "finance.view"))}


def _distribution(db: Session) -> List[dict]:
    PL, L, W = models.ProductLocation, models.Location, models.Warehouse
    rows = (
        db.query(W.id, W.code, W.name, func.sum(PL.quantity))
        .select_from(PL)
        .join(L, L.id == PL.location_id)
        .join(W, W.id == L.warehouse_id)
        .filter(PL.quantity > 0)
        .group_by(W.id, W.code, W.name)
        .all()
    )
    total = sum(float(r[3] or 0) for r in rows)
    items = [
        {"warehouse_id": r[0], "code": r[1], "name": r[2], "quantity": round(float(r[3] or 0), 3),
         "pct": round(float(r[3] or 0) / total * 100, 1) if total else 0}
        for r in rows
    ]
    items.sort(key=lambda x: -x["quantity"])
    return items


def _recent_movements(db: Session, is_admin: bool, limit: int = 6, can_invoices: bool = True, can_transfers: bool = True) -> List[dict]:
    I, O, T = models.StockInInvoice, models.StockOutInvoice, models.LocationTransfer
    # كل نوع بصلاحيته: وارد/صادر ← invoices.view، نقل ← transfers.view
    ins = db.query(I).options(joinedload(I.supplier), joinedload(I.received_by)).order_by(I.date.desc()).limit(limit).all() if can_invoices else []
    outs = db.query(O).options(joinedload(O.recipient), joinedload(O.delivered_by)).order_by(O.date.desc()).limit(limit).all() if can_invoices else []
    trs = db.query(T).options(joinedload(T.product), joinedload(T.moved_by)).order_by(T.date.desc()).limit(limit).all() if can_transfers else []

    in_tot = invoice_totals(db, models.StockInItem, [i.id for i in ins])
    out_tot = invoice_totals(db, models.StockOutItem, [o.id for o in outs])
    idx = LocationIndex(db) if trs else None

    rows = []
    for i in ins:
        rows.append({
            "kind": "in", "invoice_id": i.id, "date": i.date, "party_role": "المورد",
            "party_name": i.supplier.name if i.supplier else None,
            "user_name": format_user_display(i.received_by, i.received_by_username),
            "items_total": in_tot.get(i.id, (0, 0))[0],
            "total_value": in_tot.get(i.id, (0, 0))[1] if is_admin else None,
        })
    for o in outs:
        rows.append({
            "kind": "out", "invoice_id": o.id, "date": o.date, "party_role": "الجهة المستلمة",
            "party_name": o.recipient.name if o.recipient else None,
            "user_name": format_user_display(o.delivered_by, o.delivered_by_username),
            "items_total": out_tot.get(o.id, (0, 0))[0],
            "total_value": out_tot.get(o.id, (0, 0))[1] if is_admin else None,
        })
    for t in trs:
        rows.append({
            "kind": "transfer", "invoice_id": t.id, "id": t.id, "date": t.date,
            "product_name": t.product.name if t.product else None, "quantity": t.quantity,
            "from_code": idx.full_code(t.from_location_id), "to_code": idx.full_code(t.to_location_id),
            "user_name": t.moved_by_username or (t.moved_by.username if t.moved_by else None),
            "total_value": None,
        })
    rows.sort(key=lambda r: r["date"], reverse=True)
    return rows[:limit]


@router.get("/overview")
def overview(
    days: int = 7,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(auth.get_current_user),
):
    days = max(1, min(days, 30))
    is_admin = permissions.has_permission(current_user, "finance.view")
    P = models.Product

    total_products = db.query(func.count(P.id)).filter(P.is_active.is_(True)).scalar() or 0
    total_quantity = db.query(func.coalesce(func.sum(P.quantity), 0)).scalar() or 0
    low_q = db.query(P).filter(P.is_active.is_(True), P.quantity <= P.min_quantity)
    low_total = low_q.count()
    low_items = low_q.order_by((P.quantity - P.min_quantity).asc(), P.name).limit(6).all()
    out_of_stock = db.query(func.count(P.id)).filter(P.is_active.is_(True), P.quantity <= 0).scalar() or 0

    flow_rows = _flow(db, days, with_values=is_admin)
    today_row = flow_rows[-1]
    today_start = datetime.combine(now_cairo().date(), time.min)
    transfers_today = db.query(func.count(models.LocationTransfer.id)).filter(models.LocationTransfer.date >= today_start).scalar() or 0

    data = {
        "summary": {
            "total_products": total_products,
            "total_quantity": round(float(total_quantity), 3),
            "low_stock_count": low_total,
            "out_of_stock_count": out_of_stock,
            "total_suppliers": db.query(func.count(models.Supplier.id)).scalar() or 0,
            "total_recipients": db.query(func.count(models.Recipient.id)).scalar() or 0,
        },
        "today": {**today_row, "transfers_count": transfers_today},
        "flow": flow_rows,
        "distribution": _distribution(db),
        "low_stock": {
            "total": low_total,
            "items": [
                {"id": p.id, "sku": p.sku, "name": p.name, "quantity": p.quantity, "min_quantity": p.min_quantity}
                for p in low_items
            ],
        },
        "recent": _recent_movements(
            db, is_admin,
            can_invoices=permissions.has_permission(current_user, "invoices.view"),
            can_transfers=permissions.has_permission(current_user, "transfers.view"),
        ),
        "admin": None,
    }

    if is_admin:
        now = now_cairo()
        day_start = datetime(now.year, now.month, now.day)
        month_start = datetime(now.year, now.month, 1)
        year_start = datetime(now.year, 1, 1)

        def out_since(start):
            row = (
                db.query(
                    func.coalesce(func.sum(models.StockOutItem.quantity), 0),
                    func.coalesce(func.sum(models.StockOutItem.quantity * func.coalesce(models.StockOutItem.unit_price, 0)), 0),
                )
                .join(models.StockOutInvoice, models.StockOutInvoice.id == models.StockOutItem.invoice_id)
                .filter(models.StockOutInvoice.date >= start)
                .first()
            )
            return {"quantity": round(float(row[0]), 3), "value": round(float(row[1]), 2)}

        stock_value = db.query(func.coalesce(func.sum(P.price * P.quantity), 0)).scalar() or 0
        data["admin"] = {
            "total_stock_value": round(float(stock_value), 2),
            "out_totals": {"today": out_since(day_start), "month": out_since(month_start), "year": out_since(year_start)},
        }
    return data

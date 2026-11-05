"""
بيانات طباعة الفاتورة (وارد/صادر) في ضربة واحدة: بيانات الشركة + الفاتورة + كل الأصناف (حتى 500) + اسم المخزن لكل صنف
+ الإجماليات + مسار الـ QR. الأداء: 3 استعلامات بس (الفاتورة بالـ JOIN، الأصناف بالـ JOIN على المنتجات والمخازن، الشركة).

الصلاحيات: invoices.print. أسعار وإجماليات الوارد (أسعار شراء) بتتحجب (null) بدون finance.view.
"""
from decimal import Decimal, ROUND_HALF_UP
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session, joinedload

from .. import models, auth, invoice_calc, permissions
from ..database import get_db
from ..display_utils import format_user_display
from ..location_utils import LocationIndex
from ..services import app_settings

router = APIRouter(prefix="/api/stock", tags=["Invoice Print"])


# عناوين المستندات المطبوعة (مصدر واحد للنصوص عشان الواجهة والطباعة يطابقوا): كلها مستندات مخزنية غير ضريبية
OUT_LABELS = {
    "quote": {"title": "عرض سعر", "banner": "عرض سعر — غير مؤثر على المخزون", "notice": "عرض سعر — مستند غير ضريبي"},
    "reserved": {"title": "إذن حجز", "banner": "إذن حجز — الكميات محجوزة ولم تخرج من المخزن", "notice": "مستند مخزني غير ضريبي"},
    "completed": {"title": "إذن صرف", "banner": None, "notice": "مستند مخزني غير ضريبي"},
    "voided": {"title": "إذن صرف", "banner": "ملغاة", "notice": "مستند مخزني غير ضريبي"},
}
IN_LABELS = {"title": "إذن إضافة", "banner": None, "notice": "مستند مخزني غير ضريبي"}


_TWO = Decimal("0.01")


def _line_total(qty: float, price):
    if price is None:
        return None
    return float((Decimal(str(round(qty, 3))) * Decimal(str(price))).quantize(_TWO, rounding=ROUND_HALF_UP))


@router.get("/{kind}/{invoice_id}/print")
def invoice_print_data(
    kind: Literal["in", "out"],
    invoice_id: int,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(auth.require_permission("invoices.print")),
):
    if kind == "in":
        Inv, Item = models.StockInInvoice, models.StockInItem
        inv = (db.query(Inv).options(joinedload(Inv.supplier), joinedload(Inv.received_by), joinedload(Inv.voided_by)).filter(Inv.id == invoice_id).first())
    else:
        Inv, Item = models.StockOutInvoice, models.StockOutItem
        inv = (db.query(Inv).options(joinedload(Inv.recipient), joinedload(Inv.delivered_by), joinedload(Inv.voided_by)).filter(Inv.id == invoice_id).first())
    if not inv:
        raise HTTPException(status_code=404, detail="المستند مش موجود")

    P, L, W = models.Product, models.Location, models.Warehouse
    rows = (
        db.query(Item.quantity, Item.unit_price, P.name, P.sku, P.unit, W.name, Item.location_id)
        .join(P, P.id == Item.product_id)
        .outerjoin(L, L.id == Item.location_id)
        .outerjoin(W, W.id == L.warehouse_id)
        .filter(Item.invoice_id == invoice_id)
        .order_by(Item.id)
        .all()
    )
    idx = LocationIndex(db)
    items = [
        {"n": i + 1, "product_name": r[2], "sku": r[3], "unit": r[4], "warehouse_name": r[5],
         "quantity": r[0], "unit_price": r[1], "line_total": _line_total(r[0], r[1]),
         # الموقع التفصيلي للنسخة الداخلية فقط - نسخة العميل بتعرض اسم المخزن بس
         "location_code": idx.full_code(r[6]) if (r[6] and r[6] in idx.locations) else None}
        for i, r in enumerate(rows)
    ]

    sub = invoice_calc.subtotal_of([(r[0], r[1] or 0) for r in rows])
    totals = {"items_count": len(items), "total_quantity": round(sum(r[0] for r in rows), 3),
              "subtotal": sub, "discount": None, "net_amount": sub, "tax": None, "grand_total": sub}
    if kind == "out":
        if inv.grand_total is not None and inv.subtotal_amount is not None:
            totals["subtotal"], totals["grand_total"] = inv.subtotal_amount, inv.grand_total
            totals["net_amount"] = round(inv.subtotal_amount - (inv.discount_amount or 0), 2)  # صافي قيمة الأصناف بعد الخصم
            if inv.discount_type and (inv.discount_amount or 0) > 0:
                totals["discount"] = {"type": inv.discount_type, "value": inv.discount_value, "amount": inv.discount_amount}
            if inv.tax_type and (inv.tax_amount or 0) > 0:
                totals["tax"] = {"type": inv.tax_type, "value": inv.tax_value, "amount": inv.tax_amount}

    if kind == "in" and not permissions.has_permission(current_user, "finance.view"):
        # الوارد = أسعار شراء: من غير finance.view بنرجّع null والواجهة بتطبع "-"
        for it in items:
            it["unit_price"], it["line_total"] = None, None
        totals["subtotal"], totals["grand_total"], totals["net_amount"] = None, None, None

    C = models.CompanySettings
    c = db.query(C.name, C.phone1, C.phone2, C.logo_etag).filter(C.id == 1).first()
    company = {"name": c.name if c else None, "phone1": c.phone1 if c else None, "phone2": c.phone2 if c else None,
               "has_logo": bool(c and c.logo_etag), "logo_version": c.logo_etag if c else None}

    if kind == "in":
        pobj, user_name = inv.supplier, format_user_display(inv.received_by, inv.received_by_username)
    else:
        pobj, user_name = inv.recipient, format_user_display(inv.delivered_by, inv.delivered_by_username)
    party = pobj.name if pobj else None

    status = inv.status or "completed"
    labels = dict(OUT_LABELS.get(status, OUT_LABELS["completed"]) if kind == "out" else IN_LABELS)
    if status == "voided" and kind == "in":
        labels["banner"] = "ملغاة"
    base_url, _src = app_settings.effective_public_base_url(db)
    return {
        "kind": kind,
        "labels": labels,
        "company": company,
        "invoice": {"id": inv.id, "date": inv.date.isoformat(timespec="seconds"), "notes": inv.notes,
                    "party_name": party, "party_phone": pobj.phone if pobj else None, "party_address": pobj.address if pobj else None,
                    "user_name": user_name,
                    "status": status, "void_reason": inv.void_reason if status == "voided" else None,
                    "voided_at": inv.voided_at.isoformat(timespec="seconds") if inv.voided_at else None,
                    "voided_by": format_user_display(inv.voided_by, inv.voided_by_username) if inv.voided_at else None},
        "items": items,
        "totals": totals,
        # مسار نسبي لصفحة الفاتورة (من غير دومين). الفرونت بيبني الرابط الكامل من دومين الصفحة أو من public_base_url
        "qr_path": f"invoice.html#kind={kind}&id={inv.id}",
        "public_base_url": base_url,
    }

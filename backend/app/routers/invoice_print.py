"""
بيانات طباعة الفاتورة (وارد/صادر) في ضربة واحدة: بيانات الشركة + الفاتورة + كل الأصناف (حتى 500) + اسم المخزن لكل صنف
+ الإجماليات + مسار الـ QR. الأداء: 3 استعلامات بس (الفاتورة بالـ JOIN، الأصناف بالـ JOIN على المنتجات والمخازن، الشركة).

الصلاحيات: نفس صلاحيات GET /api/stock/in|out/{id} بالظبط (أي مستخدم مسجّل) ومن غير ما نكشف أكتر منها.
"""
import os
import re
from decimal import Decimal, ROUND_HALF_UP
from typing import Literal
from urllib.parse import urlparse

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session, joinedload

from .. import models, auth, invoice_calc
from ..database import get_db
from ..display_utils import format_user_display

router = APIRouter(prefix="/api/stock", tags=["Invoice Print"])


def _read_public_base_url() -> str:
    """PUBLIC_BASE_URL اختياري: لو اتحدد بيتثبّت دومين الـ QR، ولو فاضي الفرونت بيستخدم دومين الصفحة نفسها."""
    raw = os.getenv("PUBLIC_BASE_URL", "").strip().rstrip("/")
    if not raw:
        return ""
    u = urlparse(raw)
    if u.scheme not in ("http", "https") or not u.netloc or u.path not in ("", "/") or re.search(r"\s", raw):
        print("⚠ PUBLIC_BASE_URL غير صالح (لازم https://domain من غير مسار) - اتتجاهل")
        return ""
    return f"{u.scheme}://{u.netloc}"


PUBLIC_BASE_URL = _read_public_base_url()
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
    current_user: models.User = Depends(auth.get_current_user),
):
    if kind == "in":
        Inv, Item = models.StockInInvoice, models.StockInItem
        inv = (db.query(Inv).options(joinedload(Inv.supplier), joinedload(Inv.received_by)).filter(Inv.id == invoice_id).first())
    else:
        Inv, Item = models.StockOutInvoice, models.StockOutItem
        inv = (db.query(Inv).options(joinedload(Inv.recipient), joinedload(Inv.delivered_by)).filter(Inv.id == invoice_id).first())
    if not inv:
        raise HTTPException(status_code=404, detail="الفاتورة مش موجودة")

    P, L, W = models.Product, models.Location, models.Warehouse
    rows = (
        db.query(Item.quantity, Item.unit_price, P.name, P.sku, P.unit, W.name)
        .join(P, P.id == Item.product_id)
        .outerjoin(L, L.id == Item.location_id)
        .outerjoin(W, W.id == L.warehouse_id)
        .filter(Item.invoice_id == invoice_id)
        .order_by(Item.id)
        .all()
    )
    items = [
        {"n": i + 1, "product_name": r[2], "sku": r[3], "unit": r[4], "warehouse_name": r[5],
         "quantity": r[0], "unit_price": r[1], "line_total": _line_total(r[0], r[1])}
        for i, r in enumerate(rows)
    ]

    sub = invoice_calc.subtotal_of([(r[0], r[1] or 0) for r in rows])
    totals = {"items_count": len(items), "total_quantity": round(sum(r[0] for r in rows), 3),
              "subtotal": sub, "discount": None, "tax": None, "grand_total": sub}
    if kind == "out":
        if inv.grand_total is not None and inv.subtotal_amount is not None:
            totals["subtotal"], totals["grand_total"] = inv.subtotal_amount, inv.grand_total
            if inv.discount_type and (inv.discount_amount or 0) > 0:
                totals["discount"] = {"type": inv.discount_type, "value": inv.discount_value, "amount": inv.discount_amount}
            if inv.tax_type and (inv.tax_amount or 0) > 0:
                totals["tax"] = {"type": inv.tax_type, "value": inv.tax_value, "amount": inv.tax_amount}

    C = models.CompanySettings
    c = db.query(C.name, C.phone1, C.phone2, C.logo_etag).filter(C.id == 1).first()
    company = {"name": c.name if c else None, "phone1": c.phone1 if c else None, "phone2": c.phone2 if c else None,
               "has_logo": bool(c and c.logo_etag), "logo_version": c.logo_etag if c else None}

    if kind == "in":
        pobj, user_name = inv.supplier, format_user_display(inv.received_by, inv.received_by_username)
    else:
        pobj, user_name = inv.recipient, format_user_display(inv.delivered_by, inv.delivered_by_username)
    party = pobj.name if pobj else None

    return {
        "kind": kind,
        "company": company,
        "invoice": {"id": inv.id, "date": inv.date.isoformat(timespec="seconds"), "notes": inv.notes,
                    "party_name": party, "party_phone": pobj.phone if pobj else None, "party_address": pobj.address if pobj else None,
                    "user_name": user_name,
                    "status": (inv.status or "completed") if kind == "out" else "completed"},
        "items": items,
        "totals": totals,
        # مسار نسبي لصفحة الفاتورة (من غير دومين). الفرونت بيبني الرابط الكامل من دومين الصفحة أو من public_base_url
        "qr_path": f"invoice.html#kind={kind}&id={inv.id}",
        "public_base_url": PUBLIC_BASE_URL,
    }

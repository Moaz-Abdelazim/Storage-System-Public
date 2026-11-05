from datetime import datetime, timedelta
from ..timezone_utils import now_cairo
from ..display_utils import format_user_display
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import and_, func
from sqlalchemy.orm import Session, joinedload

from .. import models, auth, permissions
from ..database import get_db
from ..query_helpers import contains, end_exclusive, join_valid, movement_invoice, out_net_factor, page_args, paged, parse_dt, valid_invoice

router = APIRouter(prefix="/api/reports", tags=["Reports"])


@router.get("/summary")
def summary(db: Session = Depends(get_db), current_user: models.User = Depends(auth.get_current_user)):
    total_products = db.query(func.count(models.Product.id)).filter(models.Product.is_active.is_(True)).scalar() or 0
    total_quantity = db.query(func.sum(models.Product.quantity)).scalar() or 0
    is_admin = permissions.has_permission(current_user, "finance.view")
    total_stock_value = (db.query(func.sum(models.Product.price * models.Product.quantity)).scalar() or 0) if is_admin else None
    low_stock_count = (
        db.query(func.count(models.Product.id))
        .filter(models.Product.quantity <= models.Product.min_quantity, models.Product.is_active.is_(True))
        .scalar()
        or 0
    )
    total_suppliers = db.query(func.count(models.Supplier.id)).scalar() or 0
    total_recipients = db.query(func.count(models.Recipient.id)).scalar() or 0
    total_categories = db.query(func.count(models.Category.id)).scalar() or 0

    return {
        "total_products": total_products,
        "total_quantity": round(total_quantity, 3),
        "total_stock_value": round(total_stock_value, 2) if total_stock_value is not None else None,  # للأدمن بس
        "low_stock_count": low_stock_count,
        "total_suppliers": total_suppliers,
        "total_recipients": total_recipients,
        "total_categories": total_categories,
    }


@router.get("/out-totals")
def out_totals(db: Session = Depends(get_db), admin: models.User = Depends(auth.require_permission("finance.view"))):
    """
    إجمالي الصادر (كمية وقيمة) - النهاردة / الشهر الحالي / السنة الحالية،
    كل الحسابات بتوقيت القاهرة (زي باقي تواريخ النظام) وبتاخد لحد دلوقتي بالظبط.
    """
    now = now_cairo()
    day_start = datetime(now.year, now.month, now.day)
    month_start = datetime(now.year, now.month, 1)
    year_start = datetime(now.year, 1, 1)

    O, OI = models.StockOutInvoice, models.StockOutItem

    def _totals_since(start: datetime):
        """إذونات الصرف **المكتملة** بس. value = صافي المبيعات بعد الخصم (من غير ضريبة)؛ الباقي تفصيل: قبل الخصم/الخصومات/الضريبة/الإجمالي."""
        qty = (
            db.query(func.coalesce(func.sum(OI.quantity), 0))
            .join(O, join_valid(O, OI)).filter(O.date >= start).scalar()
        )
        row = (
            db.query(func.coalesce(func.sum(O.subtotal_amount), 0), func.coalesce(func.sum(O.discount_amount), 0),
                     func.coalesce(func.sum(O.tax_amount), 0), func.coalesce(func.sum(O.grand_total), 0))
            .filter(valid_invoice(O), O.date >= start).first()
        )
        gross, disc, tax, grand = (round(float(x or 0), 2) for x in row)
        return {"quantity": round(float(qty or 0), 3), "value": round(gross - disc, 2), "gross_sales": gross, "discounts": disc,
                "net_sales": round(gross - disc, 2), "tax_collected": tax, "grand_total": grand}

    return {
        "today": _totals_since(day_start),
        "month": _totals_since(month_start),
        "year": _totals_since(year_start),
    }


@router.get("/low-stock")
def low_stock(
    page: Optional[int] = None,
    page_size: int = 15,
    search: Optional[str] = None,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(auth.require_permission("products.view")),
):
    """المنتجات تحت الحد الأدنى. مع page بيرجّع صفحة (items + total..)، ومن غيرها أول 200 بس (مفيش تحميل للكل)."""
    P = models.Product
    q = db.query(P).filter(P.quantity <= P.min_quantity, P.is_active.is_(True))
    if search and search.strip():
        q = q.filter(contains(P.name, search) | contains(P.sku, search) | contains(P.barcode, search))
    q = q.order_by((P.quantity - P.min_quantity).asc(), P.name)
    to_row = lambda p: {"id": p.id, "sku": p.sku, "name": p.name, "quantity": p.quantity, "min_quantity": p.min_quantity}
    if page is None:
        return [to_row(p) for p in q.limit(200).all()]
    pg, size = page_args(page, page_size)
    total = q.count()
    return paged([to_row(p) for p in q.offset((pg - 1) * size).limit(size).all()], total, pg, size)


@router.get("/movements")
def movements(
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(auth.require_permission("reports.view")),
):
    """
    تقرير حركة الوارد والصادر في فترة معينة. تاريخ النهاية (YYYY-MM-DD) شامل اليوم كله،
    والحسابات بتتعمل بـ SQL مجمّع (مش بتحميل الفواتير والأصناف في الذاكرة).
    القيم المالية (total_*_value) للأدمن بس.
    """
    now = now_cairo()
    start = parse_dt(date_from, "تاريخ البداية") or now - timedelta(days=30)
    end_excl = end_exclusive(date_to) or now + timedelta(minutes=1)
    is_admin = permissions.has_permission(current_user, "finance.view")

    def stats(inv, item, net: bool = False):
        """مستندات completed بس. للصادر: value = صافي بعد توزيع الخصم، و gross = قبل الخصم."""
        count = db.query(func.count(inv.id)).filter(valid_invoice(inv), inv.date >= start, inv.date < end_excl).scalar() or 0
        line = item.quantity * func.coalesce(item.unit_price, 0)
        qty, value, gross = (
            db.query(
                func.coalesce(func.sum(item.quantity), 0),
                func.coalesce(func.sum(line * out_net_factor(inv) if net else line), 0),
                func.coalesce(func.sum(line), 0),
            )
            .join(inv, join_valid(inv, item))
            .filter(inv.date >= start, inv.date < end_excl)
            .first()
        )
        return count, float(qty or 0), float(value or 0), float(gross or 0)

    in_count, in_qty, in_value, _ = stats(models.StockInInvoice, models.StockInItem)
    out_count, out_qty, out_value, out_gross = stats(models.StockOutInvoice, models.StockOutItem, net=True)
    O = models.StockOutInvoice
    tax_total = float(db.query(func.coalesce(func.sum(O.tax_amount), 0)).filter(valid_invoice(O), O.date >= start, O.date < end_excl).scalar() or 0)
    return {
        "period": {"from": start.isoformat(), "to": end_excl.isoformat()},
        "stock_in_invoices_count": in_count,
        "stock_out_invoices_count": out_count,
        "total_in_qty": round(in_qty, 3),
        "total_out_qty": round(out_qty, 3),
        "total_in_value": round(in_value, 2) if is_admin else None,
        "total_out_value": round(out_value, 2) if is_admin else None,  # صافي بعد الخصم (من غير ضريبة)
        "total_out_gross_value": round(out_gross, 2) if is_admin else None,  # إجمالي قيمة الأصناف قبل الخصم
        "total_out_discount": round(out_gross - out_value, 2) if is_admin else None,
        "total_out_tax": round(tax_total, 2) if is_admin else None,
    }


@router.get("/top-products")
def top_products(
    direction: str = "out",  # "out" = الأكثر صادرًا (طلبًا) / "in" = الأكثر واردًا
    limit: int = 10,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(auth.require_permission("reports.view")),
):
    """أعلى N منتج صادر أو وارد (حسب direction) - العدد N قابل للتحكم من limit."""
    limit = max(1, min(limit, 100))
    if direction == "in":
        rows = (
            db.query(
                models.Product.id,
                models.Product.name,
                func.sum(models.StockInItem.quantity).label("total_qty"),
            )
            .join(models.StockInItem, models.StockInItem.product_id == models.Product.id)
            .join(models.StockInInvoice, join_valid(models.StockInInvoice, models.StockInItem))
            .group_by(models.Product.id)
            .order_by(func.sum(models.StockInItem.quantity).desc())
            .limit(limit)
            .all()
        )
    else:
        direction = "out"
        rows = (
            db.query(
                models.Product.id,
                models.Product.name,
                func.sum(models.StockOutItem.quantity).label("total_qty"),
            )
            .join(models.StockOutItem, models.StockOutItem.product_id == models.Product.id)
            .join(models.StockOutInvoice, join_valid(models.StockOutInvoice, models.StockOutItem))
            .group_by(models.Product.id)
            .order_by(func.sum(models.StockOutItem.quantity).desc())
            .limit(limit)
            .all()
        )
    return [{"product_id": r[0], "name": r[1], "total_quantity": r[2], "direction": direction} for r in rows]


@router.get("/by-category")
def by_category(db: Session = Depends(get_db), current_user: models.User = Depends(auth.require_permission("reports.view"))):
    """قيمة وكمية المخزون مقسّمة حسب التصنيف - مفيد للـ analysis."""
    rows = (
        db.query(
            models.Category.name,
            func.count(models.Product.id),
            func.sum(models.Product.quantity),
            func.sum(models.Product.price * models.Product.quantity),
        )
        .join(models.Product, models.Product.category_id == models.Category.id)
        .group_by(models.Category.id)
        .all()
    )
    result = [
        {"category": r[0], "products_count": r[1], "total_quantity": r[2] or 0, "total_value": round(r[3] or 0, 2) if permissions.has_permission(current_user, "finance.view") else None}
        for r in rows
    ]
    uncategorized_count = db.query(func.count(models.Product.id)).filter(models.Product.category_id.is_(None)).scalar() or 0
    if uncategorized_count:
        result.append({"category": "بدون تصنيف", "products_count": uncategorized_count, "total_quantity": None, "total_value": None})
    return result


@router.get("/by-brand")
def by_brand(db: Session = Depends(get_db), current_user: models.User = Depends(auth.require_permission("reports.view"))):
    """قيمة وكمية المخزون مقسّمة حسب الماركة - نفس فكرة by-category بالظبط."""
    rows = (
        db.query(
            models.Brand.name,
            func.count(models.Product.id),
            func.sum(models.Product.quantity),
            func.sum(models.Product.price * models.Product.quantity),
        )
        .join(models.Product, models.Product.brand_id == models.Brand.id)
        .group_by(models.Brand.id)
        .all()
    )
    result = [
        {"brand": r[0], "products_count": r[1], "total_quantity": r[2] or 0, "total_value": round(r[3] or 0, 2) if permissions.has_permission(current_user, "finance.view") else None}
        for r in rows
    ]
    unbranded_count = db.query(func.count(models.Product.id)).filter(models.Product.brand_id.is_(None)).scalar() or 0
    if unbranded_count:
        result.append({"brand": "بدون ماركة", "products_count": unbranded_count, "total_quantity": None, "total_value": None})
    return result


@router.get("/product-movements/{product_id}")
def product_movements(
    product_id: int,
    include_voided: int = 0,  # 1 = إظهار المستندات الملغاة بعلامة (مبتدخلش في الإجماليات)
    db: Session = Depends(get_db),
    current_user: models.User = Depends(auth.require_permission("invoices.view")),
):
    """كل حركات الوارد والصادر الخاصة بمنتج معين - تاريخ كامل."""
    product = db.query(models.Product).filter(models.Product.id == product_id).first()
    if not product:
        raise HTTPException(status_code=404, detail="المنتج مش موجود")
    show_finance = permissions.has_permission(current_user, "finance.view")

    in_items = (
        db.query(models.StockInItem)
        .join(models.StockInInvoice)
        .filter(models.StockInItem.product_id == product_id, movement_invoice(models.StockInInvoice, bool(include_voided)))
        .order_by(models.StockInInvoice.date.desc())
        .all()
    )
    out_items = (
        db.query(models.StockOutItem)
        .join(models.StockOutInvoice)
        .filter(models.StockOutItem.product_id == product_id, movement_invoice(models.StockOutInvoice, bool(include_voided)))
        .order_by(models.StockOutInvoice.date.desc())
        .all()
    )

    stock_in = [
        {
            "invoice_id": it.invoice_id,
            "date": it.invoice.date,
            "quantity": it.quantity,
            "unit_price": it.unit_price if show_finance else None,  # سعر الشراء: finance.view بس
            "party_name": it.invoice.supplier.name if it.invoice.supplier else None,
            "user_name": format_user_display(it.invoice.received_by, it.invoice.received_by_username),
            "notes": it.invoice.notes,
            "status": it.invoice.status,
        }
        for it in in_items
    ]
    stock_out = [
        {
            "invoice_id": it.invoice_id,
            "date": it.invoice.date,
            "quantity": it.quantity,
            "unit_price": it.unit_price,
            "party_name": it.invoice.recipient.name if it.invoice.recipient else None,
            "user_name": format_user_display(it.invoice.delivered_by, it.invoice.delivered_by_username),
            "notes": it.invoice.notes,
            "status": it.invoice.status,
        }
        for it in out_items
    ]

    return {
        "product": {"id": product.id, "sku": product.sku, "name": product.name, "quantity": product.quantity},
        "stock_in": stock_in,
        "stock_out": stock_out,
        "total_in_qty": sum(i["quantity"] for i in stock_in if i["status"] == "completed"),
        "total_out_qty": sum(i["quantity"] for i in stock_out if i["status"] == "completed"),
    }


@router.get("/top-suppliers")
def top_suppliers(
    limit: int = 10,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(auth.require_permission("reports.view")),
):
    """أعلى N مورد (العدد N قابل للتحكم من limit)."""
    limit = max(1, min(limit, 100))
    rows = (
        db.query(
            models.Supplier.id,
            models.Supplier.name,
            func.sum(models.StockInItem.quantity),
            func.sum(models.StockInItem.quantity * func.coalesce(models.StockInItem.unit_price, 0)),
            func.count(func.distinct(models.StockInInvoice.id)),
        )
        .join(models.StockInInvoice, and_(models.StockInInvoice.supplier_id == models.Supplier.id, valid_invoice(models.StockInInvoice)))
        .join(models.StockInItem, models.StockInItem.invoice_id == models.StockInInvoice.id)
        .group_by(models.Supplier.id)
        .order_by(func.sum(models.StockInItem.quantity).desc())
        .limit(limit)
        .all()
    )
    return [
        {"supplier_id": r[0], "name": r[1], "total_quantity": r[2] or 0, "total_value": round(r[3] or 0, 2) if permissions.has_permission(current_user, "finance.view") else None, "invoices_count": r[4]}
        for r in rows
    ]


@router.get("/top-recipients")
def top_recipients(
    limit: int = 10,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(auth.require_permission("reports.view")),
):
    """أعلى N جهة استلام (العدد N قابل للتحكم من limit)."""
    limit = max(1, min(limit, 100))
    rows = (
        db.query(
            models.Recipient.id,
            models.Recipient.name,
            func.sum(models.StockOutItem.quantity),
            func.sum(models.StockOutItem.quantity * func.coalesce(models.StockOutItem.unit_price, 0) * out_net_factor(models.StockOutInvoice)),
            func.count(func.distinct(models.StockOutInvoice.id)),
        )
        .join(models.StockOutInvoice, and_(models.StockOutInvoice.recipient_id == models.Recipient.id, valid_invoice(models.StockOutInvoice)))
        .join(models.StockOutItem, models.StockOutItem.invoice_id == models.StockOutInvoice.id)
        .group_by(models.Recipient.id)
        .order_by(func.sum(models.StockOutItem.quantity).desc())
        .limit(limit)
        .all()
    )
    return [
        {"recipient_id": r[0], "name": r[1], "total_quantity": r[2] or 0, "total_value": round(r[3] or 0, 2) if permissions.has_permission(current_user, "finance.view") else None, "invoices_count": r[4]}
        for r in rows
    ]


def _party_products(db: Session, inv, item, fk: str, party_id: int, search: Optional[str], page: int, page_size: int):
    """منتجات جهة (مورد/مستلم) مجمّعة بالصنف: الكمية وعدد الفواتير، بترقيم من قاعدة البيانات."""
    pg, size = page_args(page, page_size, default=10)
    P = models.Product
    grouped = (
        db.query(P.id, P.name, P.sku, func.sum(item.quantity).label("q"), func.count(func.distinct(inv.id)).label("n"))
        .join(item, item.product_id == P.id)
        .join(inv, join_valid(inv, item))
        .filter(getattr(inv, fk) == party_id)
    )
    if search and search.strip():
        grouped = grouped.filter(contains(P.name, search) | contains(P.sku, search))
    grouped = grouped.group_by(P.id, P.name, P.sku)
    total = db.query(func.count()).select_from(grouped.subquery()).scalar() or 0
    rows = grouped.order_by(func.sum(item.quantity).desc(), P.id).offset((pg - 1) * size).limit(size).all()
    items = [{"product_id": r[0], "product_name": r[1], "sku": r[2], "total_quantity": r[3] or 0, "invoices_count": r[4]} for r in rows]
    return items, total, pg, size


@router.get("/supplier-products/{supplier_id}")
def supplier_products(
    supplier_id: int,
    search: Optional[str] = None,
    page: int = 1,
    page_size: int = 10,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(auth.require_permission("suppliers.view")),
):
    """المنتجات اللي المورد ده وردهالنا وكمية كل واحد (بترقيم)."""
    supplier = db.query(models.Supplier).filter(models.Supplier.id == supplier_id).first()
    if not supplier:
        raise HTTPException(status_code=404, detail="المورد مش موجود")
    items, total, pg, size = _party_products(db, models.StockInInvoice, models.StockInItem, "supplier_id", supplier_id, search, page, page_size)
    return paged(items, total, pg, size, supplier={"id": supplier.id, "name": supplier.name}, products=items)


@router.get("/supplier-product-invoices/{supplier_id}/{product_id}")
def supplier_product_invoices(
    supplier_id: int,
    product_id: int,
    page: int = 1,
    page_size: int = 10,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(auth.require_permission("suppliers.view")),
):
    """فواتير الوارد من مورد معين لمنتج معين (بترقيم)."""
    pg, size = page_args(page, page_size, default=10)
    q = (
        db.query(models.StockInItem)
        .join(models.StockInInvoice, models.StockInInvoice.id == models.StockInItem.invoice_id)
        .filter(models.StockInInvoice.supplier_id == supplier_id, models.StockInItem.product_id == product_id)
    )
    total = q.count()
    rows = (
        q.options(joinedload(models.StockInItem.invoice).joinedload(models.StockInInvoice.received_by))
        .order_by(models.StockInInvoice.date.desc(), models.StockInItem.id.desc()).offset((pg - 1) * size).limit(size).all()
    )
    show_finance = permissions.has_permission(current_user, "finance.view")
    return paged([
        {"invoice_id": it.invoice_id, "date": it.invoice.date, "quantity": it.quantity,
         "unit_price": it.unit_price if show_finance else None,  # سعر الشراء: finance.view بس
         "user_name": format_user_display(it.invoice.received_by, it.invoice.received_by_username), "notes": it.invoice.notes}
        for it in rows
    ], total, pg, size)


@router.get("/recipient-products/{recipient_id}")
def recipient_products(
    recipient_id: int,
    search: Optional[str] = None,
    page: int = 1,
    page_size: int = 10,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(auth.require_permission("recipients.view")),
):
    """المنتجات اللي صدّرناها للجهة دي وكمية كل واحد (بترقيم)."""
    recipient = db.query(models.Recipient).filter(models.Recipient.id == recipient_id).first()
    if not recipient:
        raise HTTPException(status_code=404, detail="الجهة مش موجودة")
    items, total, pg, size = _party_products(db, models.StockOutInvoice, models.StockOutItem, "recipient_id", recipient_id, search, page, page_size)
    return paged(items, total, pg, size, recipient={"id": recipient.id, "name": recipient.name}, products=items)


@router.get("/recipient-product-invoices/{recipient_id}/{product_id}")
def recipient_product_invoices(
    recipient_id: int,
    product_id: int,
    page: int = 1,
    page_size: int = 10,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(auth.require_permission("recipients.view")),
):
    """فواتير الصادر لجهة معينة لمنتج معين (بترقيم)."""
    pg, size = page_args(page, page_size, default=10)
    q = (
        db.query(models.StockOutItem)
        .join(models.StockOutInvoice, models.StockOutInvoice.id == models.StockOutItem.invoice_id)
        .filter(models.StockOutInvoice.recipient_id == recipient_id, models.StockOutItem.product_id == product_id)
    )
    total = q.count()
    rows = (
        q.options(joinedload(models.StockOutItem.invoice).joinedload(models.StockOutInvoice.delivered_by))
        .order_by(models.StockOutInvoice.date.desc(), models.StockOutItem.id.desc()).offset((pg - 1) * size).limit(size).all()
    )
    return paged([
        {"invoice_id": it.invoice_id, "date": it.invoice.date, "quantity": it.quantity, "unit_price": it.unit_price,
         "user_name": format_user_display(it.invoice.delivered_by, it.invoice.delivered_by_username), "notes": it.invoice.notes}
        for it in rows
    ], total, pg, size)

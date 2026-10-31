import math
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import and_, case, func, literal_column, not_, or_, select, union_all
from sqlalchemy.orm import Session, joinedload, selectinload
from typing import List, Optional
from datetime import datetime, timedelta
from sqlalchemy.orm import object_session
from ..timezone_utils import now_cairo

from .. import models, schemas, auth, invoice_calc, permissions
from ..database import get_db
from ..display_utils import format_user_display
from ..location_utils import LocationIndex, lock_products, resolve_receiving_location, transfer_to_dict
from ..money import q3
from ..services import inventory
from ..query_helpers import contains, end_exclusive, invoice_totals, parse_dt

router = APIRouter(prefix="/api/stock", tags=["Stock Movements"])

# ---------- إعدادات الترقيم (Pagination) ----------
# الترقيم هنا بيحصل بالكامل في الباك اند: بنجيب من قاعدة البيانات بس الصفحة
# المطلوبة (LIMIT/OFFSET)، مش كل الفواتير مرة واحدة، عشان الفرونت إند
# مايعرضش غير جزء من البيانات فعليًا من غير ما يكون محملهم كلهم في المتصفح.
INVOICE_LIST_PAGE_SIZE_DEFAULT = 3   # "آخر 3 فواتير" - نفس اللي طلبه المستخدم
INVOICE_LIST_PAGE_SIZE_MAX = 50
LOG_PAGE_SIZE_DEFAULT = 10
LOG_PAGE_SIZE_MAX = 100


def _round_qty(value: float) -> float:
    """تقريب لـ 3 خانات عشرية عشان نتفادى أخطاء الفاصلة العائمة التراكمية (25 بيتحول 24.999999 مثلاً)."""
    return round(value, 3)


def _clamp_page(page: int) -> int:
    return max(1, page)


def _clamp_page_size(page_size: int, default: int, max_value: int) -> int:
    if page_size is None or page_size <= 0:
        return default
    return min(page_size, max_value)


def _in_invoice_to_out(inv: models.StockInInvoice, items_limit: Optional[int] = None, show_finance: bool = True) -> schemas.StockInInvoiceOut:
    all_items = inv.items
    total_value = sum((it.unit_price or 0) * it.quantity for it in all_items)
    shown_items = all_items[:items_limit] if items_limit is not None else all_items
    idx = LocationIndex(object_session(inv)) if any(it.location_id for it in shown_items) else None
    return schemas.StockInInvoiceOut(
        id=inv.id,
        supplier_id=inv.supplier_id,
        supplier_name=inv.supplier.name if inv.supplier else None,
        received_by_id=inv.received_by_id,
        received_by_name=format_user_display(inv.received_by, inv.received_by_username),
        date=inv.date,
        notes=inv.notes,
        items=[
            schemas.StockInItemOut(
                id=it.id, product_id=it.product_id,
                product_name=it.product.name if it.product else None,
                quantity=it.quantity, unit_price=it.unit_price if show_finance else None,  # سعر الشراء: finance.view بس
                location_id=it.location_id,
                location_code=idx.full_code(it.location_id) if (idx and it.location_id) else None,
            )
            for it in shown_items
        ],
        items_total=len(all_items),
        total_value=round(total_value, 2) if show_finance else None,
    )


def _out_invoice_to_out(inv: models.StockOutInvoice, items_limit: Optional[int] = None) -> schemas.StockOutInvoiceOut:
    all_items = inv.items
    total_value = sum((it.unit_price or 0) * it.quantity for it in all_items)
    shown_items = all_items[:items_limit] if items_limit is not None else all_items
    idx = LocationIndex(object_session(inv)) if any(it.location_id for it in shown_items) else None
    return schemas.StockOutInvoiceOut(
        id=inv.id,
        recipient_id=inv.recipient_id,
        recipient_name=inv.recipient.name if inv.recipient else None,
        delivered_by_id=inv.delivered_by_id,
        delivered_by_name=format_user_display(inv.delivered_by, inv.delivered_by_username),
        date=inv.date,
        notes=inv.notes,
        items=[
            schemas.StockOutItemOut(
                id=it.id, product_id=it.product_id,
                product_name=it.product.name if it.product else None,
                quantity=it.quantity, unit_price=it.unit_price,
                location_id=it.location_id,
                location_code=idx.full_code(it.location_id) if (idx and it.location_id) else None,
            )
            for it in shown_items
        ],
        items_total=len(all_items),
        total_value=round(total_value, 2),
        status=inv.status or "completed",
        discount_type=inv.discount_type, discount_value=inv.discount_value,
        tax_type=inv.tax_type, tax_value=inv.tax_value,
        **_out_totals(inv, all_items),
    )


def _out_totals(inv: models.StockOutInvoice, items) -> dict:
    """إجماليات العرض: المحفوظة وقت الإنشاء/التعديل، وللفواتير القديمة (قبل الخصم/الضريبة) بتتحسب من الأصناف من غير أي خصم."""
    if inv.grand_total is not None and inv.subtotal_amount is not None:
        return {"subtotal": inv.subtotal_amount, "discount_amount": inv.discount_amount or 0.0,
                "tax_amount": inv.tax_amount or 0.0, "grand_total": inv.grand_total}
    sub = invoice_calc.subtotal_of([(it.quantity, it.unit_price or 0) for it in items])
    return {"subtotal": sub, "discount_amount": 0.0, "tax_amount": 0.0, "grand_total": sub}


def _check_discount_permission(user: models.User, payload) -> None:
    """خصم أو ضريبة فعّالة (قيمة > 0) على الصادر محتاجة stock_out.discount."""
    has_discount = bool(payload.discount_type) and (payload.discount_value or 0) > 0
    has_tax = bool(payload.tax_type) and (payload.tax_value or 0) > 0
    if (has_discount or has_tax) and not permissions.has_permission(user, "stock_out.discount"):
        raise HTTPException(status_code=403, detail=f"ملكش صلاحية: {permissions.label_of('stock_out.discount')}")


def _check_items_basic(items):
    for item in items:
        if q3(item.quantity) <= 0:  # 0.0004 بيتقرّب لصفر - مرفوضة زيها زي الصفر
            raise HTTPException(status_code=400, detail="الكمية لازم تكون أكبر من صفر في كل الأصناف")


def _load_active_products(db: Session, items) -> dict:
    """بيقفل صفوف المنتجات (FOR UPDATE بترتيب ثابت) ويتأكد إنها موجودة ومفعّلة."""
    products = lock_products(db, [i.product_id for i in items])
    for item in items:
        product = products.get(item.product_id)
        if not product:
            raise HTTPException(status_code=404, detail=f"منتج برقم {item.product_id} مش موجود")
        if not product.is_active:
            raise HTTPException(status_code=400, detail=f"المنتج ({product.name}) غير مفعّل - فعّله الأول من صفحة المنتجات")
    return products


@router.post("/in", response_model=schemas.StockInInvoiceOut)
def add_stock_in(
    payload: schemas.StockInInvoiceCreate,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(auth.require_permission("stock_in.create")),
):
    """
    فاتورة وارد: كل صنف بيدخل مخزن (warehouse_id) فيتحط تلقائيًا في مكان الاستلام الافتراضي بتاعه،
    أو في مكان محدد (location_id). بعد ما الفاتورة تتقفل الموظف بينقل الصنف لمكانه النهائي (الرص)
    من صفحة النقل بين المواقع. مفيش بضاعة بتدخل من غير مكان.
    """
    if not payload.items:
        raise HTTPException(status_code=400, detail="لازم تضيف منتج واحد على الأقل في الفاتورة")
    _check_items_basic(payload.items)

    def _tx():
        idx = LocationIndex(db)
        # بنحدد مكان كل صنف الأول (أخطاء المخزن/المكان تبان قبل ما نلمس أي حاجة)
        targets = [resolve_receiving_location(idx, item.warehouse_id, item.location_id) for item in payload.items]
        _load_active_products(db, payload.items)  # قفل المنتجات بترتيب ثابت + التأكد إنها مفعّلة

        invoice = models.StockInInvoice(
            supplier_id=payload.supplier_id,
            received_by_id=current_user.id,
            received_by_username=current_user.username,
            notes=payload.notes,
            date=now_cairo(),
        )
        db.add(invoice)
        db.flush()  # عشان ناخد الـ id بتاع الفاتورة قبل ما نكمل

        for item, target in zip(payload.items, targets):
            db.add(models.StockInItem(
                invoice_id=invoice.id,
                product_id=item.product_id,
                quantity=float(q3(item.quantity)),
                unit_price=item.unit_price,
                location_id=target.id,
            ))
            inventory.add_stock(db, item.product_id, target.id, item.quantity)

        db.commit()
        db.refresh(invoice)
        return invoice

    return _in_invoice_to_out(inventory.with_retry(db, _tx), show_finance=permissions.has_permission(current_user, "finance.view"))


# ---------- فواتير الصادر: أدوات مشتركة بين الإنشاء والتعديل والمسح ----------
def _plan_out_lines(idx: LocationIndex, products: dict, items) -> dict:
    """
    بيجمّع المطلوب لكل (منتج + مكان) عشان لو نفس المنتج اتكرر في الفاتورة، وبيتأكد إن كل صنف له مكان سحب موجود.
    بيرجّع {(product_id, location_id): الكمية المطلوبة}. فحص كفاية الرصيد نفسه بيحصل ذرّيًا جوه
    services.inventory.remove_stock (مش هنا) - أي فحص هنا على قراءة عادية ممكن يبقى قديم على MySQL.
    """
    requested = {}
    for item in items:
        product = products[item.product_id]
        if not item.location_id:
            raise HTTPException(status_code=400, detail=f"حدد المكان اللي هتسحب منه ({product.name})")
        if item.location_id not in idx.locations:
            raise HTTPException(status_code=404, detail="الموقع المختار مش موجود")
        key = (item.product_id, item.location_id)
        requested[key] = requested.get(key, 0) + q3(item.quantity)
    return requested


def _apply_out_lines(db: Session, invoice, products: dict, items, requested: dict, prices: list) -> None:
    """بيضيف أسطر الفاتورة وبيخصم الكميات (مرة واحدة لكل منتج+مكان، بترتيب ثابت)."""
    for item, price in zip(items, prices):
        db.add(models.StockOutItem(
            invoice_id=invoice.id,
            product_id=item.product_id,
            quantity=float(q3(item.quantity)),
            unit_price=price,
            location_id=item.location_id,
        ))
    for (product_id, location_id), qty in sorted(requested.items()):
        inventory.remove_stock(db, product_id, location_id, qty)


def _revert_out_items(db: Session, idx: LocationIndex, invoice: models.StockOutInvoice, products: dict) -> None:
    """بيرجّع كميات أصناف فاتورة الصادر للمخزون (لنفس الأماكن اللي اتسحبت منها). المنتجات لازم تكون مقفولة قبل كده."""
    back = {}
    legacy = {}
    for it in invoice.items:
        if it.product_id not in products:
            raise HTTPException(status_code=409, detail="منتج في الفاتورة مش موجود - مينفعش نرجّع الكمية")
        if it.location_id is not None and it.location_id not in idx.locations:
            raise HTTPException(status_code=409, detail="المكان اللي اتسحبت منه البضاعة اتمسح - مينفعش نرجّع الكمية")
        if it.location_id is None:  # سطر قديم من غير مكان: بنرجّع الإجمالي بس
            legacy[it.product_id] = legacy.get(it.product_id, 0) + q3(it.quantity)
        else:
            key = (it.product_id, it.location_id)
            back[key] = back.get(key, 0) + q3(it.quantity)
    for (product_id, location_id), qty in sorted(back.items()):
        inventory.add_stock(db, product_id, location_id, qty)
    for product_id, qty in sorted(legacy.items()):
        inventory.adjust_product_total_only(db, product_id, qty)


def _check_recipient(db: Session, recipient_id: Optional[int]) -> None:
    if recipient_id and not db.query(models.Recipient.id).filter(models.Recipient.id == recipient_id).first():
        raise HTTPException(status_code=404, detail="الجهة المستلمة مش موجودة")


def _lock_held_out_invoice(db: Session, invoice_id: int) -> models.StockOutInvoice:
    """
    بيجيب فاتورة الصادر بقفل على صفها (FOR UPDATE) وبيتأكد إنها لسه معلّقة. القفل بيمنع إتمام ومسح/تعديل
    نفس الفاتورة في نفس اللحظة من جهازين (SQLite بتتجاهله لأن الكتابة فيها بتتسلسل أصلًا).
    """
    inv = db.query(models.StockOutInvoice).filter(models.StockOutInvoice.id == invoice_id).with_for_update().first()
    if not inv:
        raise HTTPException(status_code=404, detail="الفاتورة مش موجودة")
    if (inv.status or "completed") != "held":
        raise HTTPException(status_code=409, detail="الفاتورة دي مقفولة - مينفعش تتعدّل أو تتمسح")
    return inv


@router.post("/out", response_model=schemas.StockOutInvoiceOut)
def add_stock_out(
    payload: schemas.StockOutInvoiceCreate,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(auth.require_permission("stock_out.create")),
):
    """
    فاتورة صادر. سعر كل صنف بيتاخد من سعر المنتج المسجّل في النظام وقت الإنشاء (أي unit_price جاي من العميل بيتتجاهل).
    اختياري: status="held" لتعليق الفاتورة (الكمية بتتخصم عادي)، وخصم/ضريبة على إجمالي الفاتورة.
    """
    if not payload.items:
        raise HTTPException(status_code=400, detail="لازم تضيف منتج واحد على الأقل في الفاتورة")
    _check_items_basic(payload.items)
    _check_discount_permission(current_user, payload)
    _check_recipient(db, payload.recipient_id)

    def _tx():
        idx = LocationIndex(db)
        products = _load_active_products(db, payload.items)
        requested = _plan_out_lines(idx, products, payload.items)

        prices = [float(products[i.product_id].price or 0) for i in payload.items]
        totals = invoice_calc.compute_totals(
            [(i.quantity, pr) for i, pr in zip(payload.items, prices)],
            payload.discount_type, payload.discount_value, payload.tax_type, payload.tax_value,
        )

        invoice = models.StockOutInvoice(
            recipient_id=payload.recipient_id,
            delivered_by_id=current_user.id,
            delivered_by_username=current_user.username,
            notes=payload.notes,
            date=now_cairo(),
            status=payload.status,
            **totals,
        )
        db.add(invoice)
        db.flush()

        _apply_out_lines(db, invoice, products, payload.items, requested, prices)

        db.commit()
        db.refresh(invoice)
        return invoice

    return _out_invoice_to_out(inventory.with_retry(db, _tx))


@router.put("/out/{invoice_id}", response_model=schemas.StockOutInvoiceOut)
def update_held_stock_out(
    invoice_id: int,
    payload: schemas.StockOutInvoiceUpdate,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(auth.require_permission("stock_out.manage_held")),
):
    """
    تعديل فاتورة صادر معلّقة بس (المقفولة ما بتتعدلش). بنرجّع كميات الأصناف القديمة لأماكنها ثم نطبّق الأصناف الجديدة
    كأنها فاتورة جديدة في نفس العملية (لو حاجة فشلت كل حاجة بترجع زي ما كانت). الأسطر القديمة بتحتفظ بسعرها
    المسجّل، والأسطر الجديدة بتاخد سعر المنتج الحالي.
    """
    if not payload.items:
        raise HTTPException(status_code=400, detail="لازم تضيف منتج واحد على الأقل في الفاتورة")
    _check_items_basic(payload.items)
    _check_discount_permission(current_user, payload)
    _check_recipient(db, payload.recipient_id)

    def _tx():
        invoice = _lock_held_out_invoice(db, invoice_id)
        idx = LocationIndex(db)
        old_items = sorted(invoice.items, key=lambda it: it.id)
        old_prices = {it.product_id: it.unit_price for it in old_items if it.unit_price is not None}

        ids = {it.product_id for it in old_items} | {i.product_id for i in payload.items}
        products = lock_products(db, ids)
        old_ids = {it.product_id for it in old_items}
        for item in payload.items:
            product = products.get(item.product_id)
            if not product:
                raise HTTPException(status_code=404, detail=f"منتج برقم {item.product_id} مش موجود")
            if not product.is_active and item.product_id not in old_ids:
                raise HTTPException(status_code=400, detail=f"المنتج ({product.name}) غير مفعّل - فعّله الأول من صفحة المنتجات")

        prices = [old_prices.get(i.product_id, float(products[i.product_id].price or 0)) for i in payload.items]
        totals = invoice_calc.compute_totals(
            [(i.quantity, pr) for i, pr in zip(payload.items, prices)],
            payload.discount_type, payload.discount_value, payload.tax_type, payload.tax_value,
        )

        _revert_out_items(db, idx, invoice, products)
        invoice.items.clear()
        db.flush()
        requested = _plan_out_lines(idx, products, payload.items)
        _apply_out_lines(db, invoice, products, payload.items, requested, prices)
        invoice.recipient_id = payload.recipient_id
        invoice.notes = payload.notes
        for key, value in totals.items():
            setattr(invoice, key, value)
        db.commit()
        db.refresh(invoice)
        return invoice

    return _out_invoice_to_out(inventory.with_retry(db, _tx))


@router.post("/out/{invoice_id}/complete", response_model=schemas.StockOutInvoiceOut)
def complete_held_stock_out(
    invoice_id: int,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(auth.require_permission("stock_out.manage_held")),
):
    """إتمام فاتورة صادر معلّقة: بتتقفل نهائيًا (مفيش تعديل ولا مسح بعد كده)."""
    invoice = _lock_held_out_invoice(db, invoice_id)
    invoice.status = "completed"
    db.commit()
    db.refresh(invoice)
    return _out_invoice_to_out(invoice)


@router.delete("/out/{invoice_id}")
def delete_held_stock_out(
    invoice_id: int,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(auth.require_permission("stock_out.manage_held")),
):
    """مسح فاتورة صادر معلّقة بس: الكميات بترجع للمخزون ولنفس الأماكن اللي اتسحبت منها، والفاتورة بتتمسح نهائيًا."""
    def _tx():
        invoice = _lock_held_out_invoice(db, invoice_id)
        idx = LocationIndex(db)
        products = lock_products(db, {it.product_id for it in invoice.items})
        _revert_out_items(db, idx, invoice, products)
        db.delete(invoice)
        db.commit()

    inventory.with_retry(db, _tx)
    return {"message": "تم مسح الفاتورة المعلّقة وإرجاع الكميات للمخزون"}


@router.get("/out/{invoice_id}/edit-data")
def held_out_edit_data(
    invoice_id: int,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(auth.require_permission("stock_out.manage_held")),
):
    """
    كل اللي شاشة تعديل فاتورة معلّقة محتاجاه في ضربة واحدة (من غير طلب لكل صنف): الأسطر ببيانات منتجاتها
    وأماكن السحب المتاحة لكل منتج. رصيد كل مكان محسوب بعد إرجاع كميات الفاتورة نفسها (لأنها بتتعكس وقت الحفظ).
    """
    inv = db.query(models.StockOutInvoice).filter(models.StockOutInvoice.id == invoice_id).first()
    if not inv:
        raise HTTPException(status_code=404, detail="الفاتورة مش موجودة")
    if (inv.status or "completed") != "held":
        raise HTTPException(status_code=409, detail="الفاتورة دي مقفولة - مينفعش تتعدّل")
    items = sorted(inv.items, key=lambda it: it.id)
    pids = {it.product_id for it in items}
    idx = LocationIndex(db)

    products = {p.id: p for p in db.query(models.Product).filter(models.Product.id.in_(pids)).all()} if pids else {}
    balances = {}
    if pids:
        for pid, lid, qty in db.query(models.ProductLocation.product_id, models.ProductLocation.location_id,
                                      models.ProductLocation.quantity).filter(models.ProductLocation.product_id.in_(pids)).all():
            balances.setdefault(pid, {})[lid] = qty
    reserved_by_product = {}
    for it in items:
        reserved_by_product[it.product_id] = reserved_by_product.get(it.product_id, 0) + it.quantity
        if it.location_id is not None:
            slot = balances.setdefault(it.product_id, {})
            slot[it.location_id] = slot.get(it.location_id, 0) + it.quantity

    lines = []
    for it in items:
        p = products.get(it.product_id)
        lines.append({
            "product_id": it.product_id,
            "product_name": p.name if p else None,
            "sku": p.sku if p else None,
            "unit": p.unit if p else None,
            "product_price": p.price if p else None,
            "product_quantity": _round_qty((p.quantity if p else 0) + reserved_by_product.get(it.product_id, 0)),
            "quantity": it.quantity,
            "unit_price": it.unit_price,
            "location_id": it.location_id,
            "locations": [
                {"location_id": lid, "full_code": idx.full_code(lid), "quantity": _round_qty(q)}
                for lid, q in sorted(balances.get(it.product_id, {}).items()) if lid in idx.locations and q > 0
            ],
        })
    return {
        "id": inv.id, "status": inv.status, "recipient_id": inv.recipient_id,
        "recipient_name": inv.recipient.name if inv.recipient else None, "notes": inv.notes,
        "discount_type": inv.discount_type, "discount_value": inv.discount_value,
        "tax_type": inv.tax_type, "tax_value": inv.tax_value,
        "lines": lines,
    }


@router.get("/in")
def list_stock_in(
    page: int = 1,
    page_size: int = INVOICE_LIST_PAGE_SIZE_DEFAULT,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(auth.require_permission("invoices.view")),
):
    """
    ترقيم كامل من الباك اند: كل صفحة بتجيب عدد محدود من الفواتير بس (LIMIT/OFFSET
    على مستوى قاعدة البيانات). القايمة هنا بترجع ملخص الفاتورة بس (المورد،
    مين استلمها، التاريخ، الإجمالي) من غير أي أصناف - لعرض أصناف الفاتورة
    كاملة استخدم GET /api/stock/in/{id} (صفحة منفصلة في الفرونت إند).
    """
    page = _clamp_page(page)
    page_size = _clamp_page_size(page_size, INVOICE_LIST_PAGE_SIZE_DEFAULT, INVOICE_LIST_PAGE_SIZE_MAX)

    base_query = db.query(models.StockInInvoice)
    total = base_query.count()
    rows = (
        base_query.options(joinedload(models.StockInInvoice.supplier), joinedload(models.StockInInvoice.received_by), selectinload(models.StockInInvoice.items))
        .order_by(models.StockInInvoice.date.desc(), models.StockInInvoice.id.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
        .all()
    )
    return {
        "items": [_in_invoice_to_out(r, items_limit=0, show_finance=permissions.has_permission(current_user, "finance.view")) for r in rows],
        "total": total,
        "page": page,
        "page_size": page_size,
        "total_pages": max(1, math.ceil(total / page_size)),
    }


@router.get("/in/{invoice_id}", response_model=schemas.StockInInvoiceOut)
def get_stock_in_invoice(
    invoice_id: int,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(auth.require_permission("invoices.view")),
):
    """تفاصيل فاتورة وارد واحدة كاملة (كل الأصناف من غير أي حد أقصى)."""
    inv = db.query(models.StockInInvoice).filter(models.StockInInvoice.id == invoice_id).first()
    if not inv:
        raise HTTPException(status_code=404, detail="الفاتورة مش موجودة")
    return _in_invoice_to_out(inv, show_finance=permissions.has_permission(current_user, "finance.view"))


@router.get("/out")
def list_stock_out(
    page: int = 1,
    page_size: int = INVOICE_LIST_PAGE_SIZE_DEFAULT,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(auth.require_permission("invoices.view")),
):
    """نفس فكرة ترقيم /api/stock/in بالظبط لكن لفواتير الصادر - ملخص بس من غير أصناف."""
    page = _clamp_page(page)
    page_size = _clamp_page_size(page_size, INVOICE_LIST_PAGE_SIZE_DEFAULT, INVOICE_LIST_PAGE_SIZE_MAX)

    base_query = db.query(models.StockOutInvoice)
    total = base_query.count()
    rows = (
        base_query.options(joinedload(models.StockOutInvoice.recipient), joinedload(models.StockOutInvoice.delivered_by), selectinload(models.StockOutInvoice.items))
        .order_by(models.StockOutInvoice.date.desc(), models.StockOutInvoice.id.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
        .all()
    )
    return {
        "items": [_out_invoice_to_out(r, items_limit=0) for r in rows],
        "total": total,
        "page": page,
        "page_size": page_size,
        "total_pages": max(1, math.ceil(total / page_size)),
    }


@router.get("/out/{invoice_id}", response_model=schemas.StockOutInvoiceOut)
def get_stock_out_invoice(
    invoice_id: int,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(auth.require_permission("invoices.view")),
):
    """تفاصيل فاتورة صادر واحدة كاملة (كل الأصناف من غير أي حد أقصى)."""
    inv = db.query(models.StockOutInvoice).filter(models.StockOutInvoice.id == invoice_id).first()
    if not inv:
        raise HTTPException(status_code=404, detail="الفاتورة مش موجودة")
    return _out_invoice_to_out(inv)


# =====================================================================
#  السجل الموحّد (وارد + صادر + نقل) - ترقيم حقيقي في قاعدة البيانات
# =====================================================================
_KIND_RANK = {"in": 0, "out": 1, "transfer": 2}  # أولوية الترتيب لو (date, id) اتساووا بين نوعين (زي ترتيب النسخة القديمة)
_LIT_CHUNK = 1000  # أقصى عدد أرقام في IN واحدة (بنكتبها literals مش bind params عشان حد SQLite)


def _in_literals(column, ids):
    ids = sorted(int(i) for i in ids)
    if not ids:
        return literal_column("1 = 0")
    chunks = [ids[i:i + _LIT_CHUNK] for i in range(0, len(ids), _LIT_CHUNK)]
    return or_(*[column.in_([literal_column(str(i)) for i in ch]) for ch in chunks])


def _location_match(column, ids, all_ids):
    """column ∈ ids بدون bind params كتير؛ لو ids أغلبية المواقع بنعكس الشرط (NOT IN للباقي)."""
    if not ids:
        return literal_column("1 = 0")
    if len(ids) >= len(all_ids):
        return column.is_not(None)
    if len(ids) > len(all_ids) / 2:
        return and_(column.is_not(None), not_(_in_literals(column, set(all_ids) - set(ids))))
    return _in_literals(column, ids)


def _invoice_source(Inv, Party, party_fk, user_fk, user_col, start, end_excl, party, user):
    """
    شروط وجوينات فواتير الوارد/الصادر (نفس فلاتر النسخة القديمة بالظبط). بترجّع (joins, conditions).
    الـ party جوين عادي (inner) - فاتورة من غير جهة بتختفي لما نفلتر بالجهة. الـ user بيدوّر في النسخة المخزّنة من الاسم أو اسم الحساب.
    """
    conds, joins = [], []
    if start:
        conds.append(Inv.date >= start)
    if end_excl:
        conds.append(Inv.date < end_excl)
    if party and party.strip():
        joins.append((Party, Party.id == getattr(Inv, party_fk), False))
        conds.append(contains(Party.name, party))
    if user and user.strip():
        joins.append((models.User, models.User.id == getattr(Inv, user_fk), True))
        conds.append(or_(contains(getattr(Inv, user_col), user), contains(models.User.username, user)))
    return joins, conds


def _apply(q, joins, conds):
    for target, on, outer in joins:
        q = q.outerjoin(target, on) if outer else q.join(target, on)
    return q.filter(*conds) if conds else q


def _transfer_source(db: Session, idx: LocationIndex, start, end_excl, party, user):
    """
    شروط وجوينات حركات النقل - نفس سلوك query_transfers (فلترة Python) بس في SQL:
      party: النص موجود في الكود الكامل لـ "من" أو "إلى" أو في اسم المنتج (بدون حساسية لحالة الحروف).
             الأكواد الكاملة بتتبني في الذاكرة (شجرة المواقع صغيرة) فبنجيب أرقام المواقع المطابقة ونفلتر بيها.
      user:  النص موجود في نص العرض (اسم الحساب + "(الحساب موقوف)" أو النسخة المخزّنة + "(الحساب اتمسح)").
    """
    T = models.LocationTransfer
    conds, joins = [], []
    if start:
        conds.append(T.date >= start)
    if end_excl:
        conds.append(T.date < end_excl)
    if party:
        p = party.strip().lower()
        if p:  # نص فاضي = من غير فلتر (زي القديم)
            all_ids = list(idx.locations.keys())
            matched = {lid for lid in all_ids if p in (idx.full_code(lid) or "").lower()}
            joins.append((models.Product, models.Product.id == T.product_id, True))
            conds.append(or_(
                _location_match(T.from_location_id, matched, all_ids),
                _location_match(T.to_location_id, matched, all_ids),
                contains(models.Product.name, p),
            ))
    if user:
        u = user.strip().lower()
        U = models.User
        joins.append((U, U.id == T.moved_by_id, True))
        inactive = U.is_active.is_not(True)
        display = case(
            (U.id.is_not(None), case((inactive, U.username + " (الحساب موقوف)"), else_=U.username)),
            (and_(T.moved_by_username.is_not(None), T.moved_by_username != ""), T.moved_by_username + " (الحساب اتمسح)"),
            else_=None,
        )
        conds.append(contains(display, u))  # NULL (مفيش اسم نهائيًا) بيطلع برا زي القديم
    return joins, conds


@router.get("/log")
def stock_log(
    kind: Optional[str] = None,  # "in" / "out" / "transfer" / None (الكل)
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    party: Optional[str] = None,  # بحث باسم المورد (في الوارد) أو الجهة المستلمة (في الصادر)
    user: Optional[str] = None,  # بحث باسم المستخدم اللي سلّم أو استلم أو نقل
    page: int = 1,
    page_size: int = LOG_PAGE_SIZE_DEFAULT,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(auth.require_any_permission("invoices.view", "transfers.view")),
):
    """
    سجل موحّد وقابل للفلترة لكل فواتير الوارد والصادر وحركات النقل.

    الأداء: الترقيم حقيقي في قاعدة البيانات. بنعمل UNION ALL على (النوع، id، التاريخ) للأنواع التلاتة بنفس الفلاتر،
    ORDER BY date DESC, id DESC (ولو اتساووا: وارد ثم صادر ثم نقل زي الأول) و LIMIT/OFFSET، وكل نوع بنجيب منه أول
    (offset + page_size) صف بس قبل الدمج. الإجماليات (totals) و total بـ COUNT/SUM في SQL، وبعدين بنحمّل تفاصيل صفحة واحدة بس.
    كل صف ملخص فاتورة (من غير أصناف) - لعرض الأصناف استخدم GET /api/stock/in/{id} أو /out/{id}.
    """
    start = parse_dt(date_from, "تاريخ البداية")
    end_excl = end_exclusive(date_to)
    page = _clamp_page(page)
    page_size = _clamp_page_size(page_size, LOG_PAGE_SIZE_DEFAULT, LOG_PAGE_SIZE_MAX)

    # كل نوع بيظهر بصلاحيته: in/out ← invoices.view، transfer ← transfers.view. النوع غير المسموح بيتشال من النتيجة والإجماليات.
    can_invoices = permissions.has_permission(current_user, "invoices.view")
    can_transfers = permissions.has_permission(current_user, "transfers.view")
    show_finance = permissions.has_permission(current_user, "finance.view")
    want = {
        "in": can_invoices and kind in (None, "", "all", "in"),
        "out": can_invoices and kind in (None, "", "all", "out"),
        "transfer": can_transfers and kind in (None, "", "all", "transfer"),
    }
    window = page * page_size  # أقصى عدد صفوف محتاجينه من كل نوع (offset + page_size)

    counts = {"in": 0, "out": 0, "transfer": 0}
    values = {"in": 0.0, "out": 0.0}
    transfers_qty = 0.0
    branches = []  # كل فرع: select (rk, id, date) مرتّب ومحدود

    def add_branch(k, q, id_col, date_col):
        sub = (
            q.with_entities(literal_column(str(_KIND_RANK[k])).label("rk"), id_col.label("id"), date_col.label("date"))
            .order_by(date_col.desc(), id_col.desc()).limit(window).subquery()
        )
        branches.append(select(sub.c.rk, sub.c.id, sub.c.date))

    # ---- الوارد والصادر ----
    for k, Inv, Item, Party, party_fk, user_fk, user_col in (
        ("in", models.StockInInvoice, models.StockInItem, models.Supplier, "supplier_id", "received_by_id", "received_by_username"),
        ("out", models.StockOutInvoice, models.StockOutItem, models.Recipient, "recipient_id", "delivered_by_id", "delivered_by_username"),
    ):
        if not want[k]:
            continue
        joins, conds = _invoice_source(Inv, Party, party_fk, user_fk, user_col, start, end_excl, party, user)
        q = _apply(db.query(Inv), joins, conds)
        counts[k] = q.with_entities(func.count(Inv.id)).scalar() or 0
        if counts[k]:
            ids_sq = _apply(db.query(Inv.id), joins, conds).subquery()
            values[k] = float(
                db.query(func.coalesce(func.sum(Item.quantity * func.coalesce(Item.unit_price, 0)), 0))
                .filter(Item.invoice_id.in_(select(ids_sq.c.id)))
                .scalar() or 0
            )
            add_branch(k, q, Inv.id, Inv.date)

    # ---- حركات النقل بين المواقع (بتظهر في نفس السجل) ----
    idx = None
    if want["transfer"]:
        T = models.LocationTransfer
        idx = LocationIndex(db)
        joins, conds = _transfer_source(db, idx, start, end_excl, party, user)
        q = _apply(db.query(T), joins, conds)
        t_count, t_qty = q.with_entities(func.count(T.id), func.coalesce(func.sum(T.quantity), 0)).one()
        counts["transfer"], transfers_qty = int(t_count or 0), float(t_qty or 0)
        if counts["transfer"]:
            add_branch("transfer", q, T.id, T.date)

    total_filtered = sum(counts.values())

    # ---- الصفحة المطلوبة (UNION ALL + ORDER BY + LIMIT/OFFSET في الداتا بيز) ----
    page_rows = []
    if branches and (page - 1) * page_size < total_filtered:
        u = union_all(*branches).subquery() if len(branches) > 1 else branches[0].subquery()
        page_rows = db.execute(
            select(u.c.rk, u.c.id).order_by(u.c.date.desc(), u.c.id.desc(), u.c.rk.asc())
            .limit(page_size).offset((page - 1) * page_size)
        ).all()
    rank_kind = {v: k for k, v in _KIND_RANK.items()}
    page_entries = [(rank_kind[int(r[0])], int(r[1])) for r in page_rows]

    # ---- تفاصيل الصفحة الحالية بس ----
    ids = {k: [e[1] for e in page_entries if e[0] == k] for k in _KIND_RANK}
    details = {}
    if ids["in"]:
        Inv = models.StockInInvoice
        tot = invoice_totals(db, models.StockInItem, ids["in"])
        for inv in db.query(Inv).options(joinedload(Inv.supplier), joinedload(Inv.received_by)).filter(Inv.id.in_(ids["in"])).all():
            n, val = tot.get(inv.id, (0, 0.0))
            details[("in", inv.id)] = {
                "kind": "in", "invoice_id": inv.id, "date": inv.date, "party_role": "المورد",
                "party_name": inv.supplier.name if inv.supplier else None, "user_role": "استلمها",
                "user_name": format_user_display(inv.received_by, inv.received_by_username),
                "notes": inv.notes, "items": [], "items_total": n, "total_value": val if show_finance else None,
            }
    if ids["out"]:
        Inv = models.StockOutInvoice
        tot = invoice_totals(db, models.StockOutItem, ids["out"])
        for inv in db.query(Inv).options(joinedload(Inv.recipient), joinedload(Inv.delivered_by)).filter(Inv.id.in_(ids["out"])).all():
            n, val = tot.get(inv.id, (0, 0.0))
            details[("out", inv.id)] = {
                "kind": "out", "invoice_id": inv.id, "date": inv.date, "party_role": "الجهة المستلمة",
                "party_name": inv.recipient.name if inv.recipient else None, "user_role": "سلّمها",
                "user_name": format_user_display(inv.delivered_by, inv.delivered_by_username),
                "notes": inv.notes, "items": [], "items_total": n, "total_value": val,
                "status": inv.status or "completed",
            }
    if ids["transfer"]:
        T = models.LocationTransfer
        for tr in db.query(T).options(joinedload(T.product), joinedload(T.moved_by)).filter(T.id.in_(ids["transfer"])).all():
            t = transfer_to_dict(tr, idx)
            details[("transfer", t["id"])] = {
                "kind": "transfer", "invoice_id": t["id"], "date": t["date"], "party_role": "النقل",
                "party_name": f"{t['from_code']} ← {t['to_code']}", "user_role": "نقلها", "user_name": t["moved_by"],
                "notes": t["notes"], "product_name": t["product_name"], "sku": t["sku"], "quantity": t["quantity"],
                "from_code": t["from_code"], "to_code": t["to_code"], "items": [], "items_total": 1, "total_value": 0,
            }
    page_items = [details[e] for e in page_entries if e in details]

    return {
        "items": page_items,
        "total": total_filtered,
        "page": page,
        "page_size": page_size,
        "total_pages": max(1, math.ceil(total_filtered / page_size)) if total_filtered else 1,
        "totals": {
            "in_invoices_count": counts["in"],
            "in_total_value": round(values["in"], 2) if show_finance else None,  # قيمة الوارد = سعر شراء: finance.view بس
            "out_invoices_count": counts["out"],
            "out_total_value": round(values["out"], 2),
            "transfers_count": counts["transfer"],
            "transfers_quantity": round(transfers_qty, 3) if counts["transfer"] else 0,  # int لما مفيش حركات (نفس نوع النسخة القديمة)
        },
    }
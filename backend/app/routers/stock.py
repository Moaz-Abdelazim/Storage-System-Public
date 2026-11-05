import math
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Dict, List, Optional, Tuple

from fastapi import APIRouter, Body, Depends, HTTPException
from sqlalchemy import and_, case, func, literal_column, not_, or_, select, union_all
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, joinedload, object_session, selectinload

from .. import models, schemas, auth, invoice_calc, permissions
from ..database import get_db
from ..display_utils import format_user_display
from ..location_utils import LocationIndex, lock_products, resolve_receiving_location, transfer_to_dict
from ..money import q3
from ..query_helpers import contains, end_exclusive, invoice_totals, movement_invoice, parse_dt, valid_invoice
from ..services import inventory
from ..services.audit import audit
from ..timezone_utils import now_cairo

router = APIRouter(prefix="/api/stock", tags=["Stock Movements"])

# ---------- إعدادات الترقيم (Pagination) ----------
# الترقيم بيحصل بالكامل في الباك اند (LIMIT/OFFSET): بنجيب بس الصفحة المطلوبة.
INVOICE_LIST_PAGE_SIZE_DEFAULT = 3   # "آخر 3 مستندات"
INVOICE_LIST_PAGE_SIZE_MAX = 50
LOG_PAGE_SIZE_DEFAULT = 10
LOG_PAGE_SIZE_MAX = 100

OUT_STATUSES = ("quote", "reserved", "completed", "voided")
# صلاحية كل حالة عند إنشاء إذن صرف جديد
OUT_CREATE_PERM = {"completed": "stock_out.create", "quote": "stock_out.quote", "reserved": "stock_out.reserve"}


def _round_qty(value: float) -> float:
    return round(value, 3)


def _clamp_page(page: int) -> int:
    return max(1, page)


def _clamp_page_size(page_size: int, default: int, max_value: int) -> int:
    if page_size is None or page_size <= 0:
        return default
    return min(page_size, max_value)


def _need(user: models.User, key: str) -> None:
    if not permissions.has_permission(user, key):
        raise HTTPException(status_code=403, detail=f"ملكش صلاحية: {permissions.label_of(key)}")


# ---------- تحويل الموديلات لـ API ----------
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
        status=inv.status or "completed",
        voided_at=inv.voided_at,
        voided_by_name=format_user_display(inv.voided_by, inv.voided_by_username) if inv.voided_at else None,
        void_reason=inv.void_reason,
    )


def _out_totals(inv: models.StockOutInvoice, items) -> dict:
    """إجماليات العرض: subtotal (قبل الخصم) - discount = net، + tax = grand_total. المحفوظة وقت الإنشاء/التعديل."""
    if inv.grand_total is not None and inv.subtotal_amount is not None:
        sub, disc = inv.subtotal_amount, inv.discount_amount or 0.0
        return {"subtotal": sub, "discount_amount": disc, "net_amount": round(sub - disc, 2),
                "tax_amount": inv.tax_amount or 0.0, "grand_total": inv.grand_total}
    sub = invoice_calc.subtotal_of([(it.quantity, it.unit_price or 0) for it in items])
    return {"subtotal": sub, "discount_amount": 0.0, "net_amount": sub, "tax_amount": 0.0, "grand_total": sub}


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
        completed_at=inv.completed_at,
        voided_at=inv.voided_at,
        voided_by_name=format_user_display(inv.voided_by, inv.voided_by_username) if inv.voided_at else None,
        void_reason=inv.void_reason,
        copied_from_id=inv.copied_from_id,
        **_out_totals(inv, all_items),
    )


# ---------- أدوات مشتركة ----------
def _check_discount_permission(user: models.User, payload) -> None:
    """خصم أو ضريبة فعّالة (قيمة > 0) على إذن الصرف محتاجة stock_out.discount."""
    has_discount = bool(payload.discount_type) and (payload.discount_value or 0) > 0
    has_tax = bool(payload.tax_type) and (payload.tax_value or 0) > 0
    if has_discount or has_tax:
        _need(user, "stock_out.discount")


def _check_items_basic(items):
    for item in items:
        if q3(item.quantity) <= 0:  # 0.0004 بيتقرّب لصفر - مرفوضة زيها زي الصفر
            raise HTTPException(status_code=400, detail="الكمية لازم تكون أكبر من صفر في كل الأصناف")


def _load_products(db: Session, items, lock: bool = True) -> dict:
    """بيجيب المنتجات (وبيقفل صفوفها FOR UPDATE بترتيب ثابت لو lock) ويتأكد إنها موجودة ومفعّلة."""
    ids = sorted({i.product_id for i in items})
    if lock:
        products = lock_products(db, ids)
    else:
        products = {p.id: p for p in db.query(models.Product).filter(models.Product.id.in_(ids)).all()} if ids else {}
    for item in items:
        product = products.get(item.product_id)
        if not product:
            raise HTTPException(status_code=404, detail=f"منتج برقم {item.product_id} مش موجود")
        if not product.is_active:
            raise HTTPException(status_code=400, detail=f"المنتج ({product.name}) غير مفعّل - فعّله الأول من صفحة المنتجات")
    return products


def _check_recipient(db: Session, recipient_id: Optional[int]) -> None:
    if recipient_id and not db.query(models.Recipient.id).filter(models.Recipient.id == recipient_id).first():
        raise HTTPException(status_code=404, detail="الجهة المستلمة مش موجودة")


def _existing_by_key(db: Session, Model, key: Optional[str], user: models.User):
    """منع التكرار: لو المفتاح اتسجّل قبل كده لنفس المستخدم بنرجّع المستند الأصلي. مستخدم تاني بنفس المفتاح = تعارض."""
    if not key:
        return None
    inv = db.query(Model).filter(Model.idempotency_key == key).first()
    if inv is None:
        return None
    owner = getattr(inv, "received_by_id", None) if Model is models.StockInInvoice else getattr(inv, "delivered_by_id", None)
    if owner is not None and owner != user.id:
        raise HTTPException(status_code=409, detail="مفتاح الطلب مستخدم قبل كده")
    return inv


def _run_idempotent(db: Session, Model, key: Optional[str], user: models.User, tx):
    """
    بينفّذ tx (إنشاء المستند + حركته في transaction واحدة). لو نفس المفتاح اتبعت قبل كده: المستند الأصلي (200) من غير حركة جديدة.
    السباق (طلبين بنفس المفتاح في نفس اللحظة): الـ unique في الـ DB بيرفض التاني بـ IntegrityError فبنعمل rollback ونقرا الموجود.
    """
    existing = _existing_by_key(db, Model, key, user)
    if existing is not None:
        return existing, True
    try:
        return inventory.with_retry(db, tx), False
    except IntegrityError:
        db.rollback()
        existing = _existing_by_key(db, Model, key, user)
        if existing is None:
            raise
        return existing, True


def _plan_out_lines(idx: LocationIndex, products: dict, items, require_location: bool) -> Dict[Tuple[int, int], Decimal]:
    """
    بيجمّع المطلوب لكل (منتج + مكان) (لو نفس المنتج اتكرر)، وبيتأكد إن كل صنف له مكان سحب موجود (إلا عرض السعر: المكان اختياري).
    بيرجّع {(product_id, location_id): الكمية}.
    """
    requested: Dict[Tuple[int, int], Decimal] = {}
    for item in items:
        product = products[item.product_id]
        if not item.location_id:
            if require_location:
                raise HTTPException(status_code=400, detail=f"حدد المكان اللي هتسحب منه ({product.name})")
            continue
        if item.location_id not in idx.locations:
            raise HTTPException(status_code=404, detail="الموقع المختار مش موجود")
        key = (item.product_id, item.location_id)
        requested[key] = requested.get(key, Decimal(0)) + q3(item.quantity)
    return requested


def _reserved_of(invoice: models.StockOutInvoice) -> Dict[Tuple[int, int], Decimal]:
    out: Dict[Tuple[int, int], Decimal] = {}
    for it in invoice.items:
        if it.location_id is not None:
            key = (it.product_id, it.location_id)
            out[key] = out.get(key, Decimal(0)) + q3(it.quantity)
    return out


def _require_available(db: Session, requested: Dict[Tuple[int, int], Decimal]) -> None:
    """يرفض (400) لو أي (منتج، مكان) المتاح فيه أقل من المطلوب، برسالة بتذكر **كل** الأصناف المتأثرة."""
    bad = inventory.shortages(db, requested)
    if bad:
        raise HTTPException(status_code=400, detail=inventory.shortages_message(bad))


def _add_out_items(db: Session, invoice, items, prices: list) -> None:
    for item, price in zip(items, prices):
        db.add(models.StockOutItem(
            invoice_id=invoice.id, product_id=item.product_id, quantity=float(q3(item.quantity)),
            unit_price=price, location_id=item.location_id,
        ))


def _lock_out_invoice(db: Session, invoice_id: int, allowed: Tuple[str, ...], what: str) -> models.StockOutInvoice:
    """مستند الصادر بقفل على صفه (FOR UPDATE) + التأكد من حالته. القفل بيمنع حجزين/إتمامين لنفس المستند في نفس اللحظة."""
    inv = db.query(models.StockOutInvoice).filter(models.StockOutInvoice.id == invoice_id).with_for_update().first()
    if not inv:
        raise HTTPException(status_code=404, detail="المستند مش موجود")
    if (inv.status or "completed") not in allowed:
        raise HTTPException(status_code=409, detail=f"مينفعش {what}: حالة المستند الحالية ({_STATUS_LABEL.get(inv.status, inv.status)})")
    return inv


_STATUS_LABEL = {"quote": "عرض سعر", "reserved": "محجوزة", "completed": "تمت", "voided": "ملغاة"}


def _out_summary(inv: models.StockOutInvoice) -> dict:
    return {"status": inv.status, "items": len(inv.items), "subtotal": inv.subtotal_amount, "discount": inv.discount_amount,
            "tax": inv.tax_amount, "grand_total": inv.grand_total}


def _set_totals(invoice: models.StockOutInvoice, totals: dict) -> None:
    for key, value in totals.items():
        if hasattr(invoice, key) and key != "net_amount":
            setattr(invoice, key, value)


# =====================================================================
#  إذن إضافة (وارد): completed / voided
# =====================================================================
@router.post("/in", response_model=schemas.StockInInvoiceOut)
def add_stock_in(
    payload: schemas.StockInInvoiceCreate,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(auth.require_permission("stock_in.create")),
):
    """
    إذن إضافة: كل صنف بيدخل مخزن (warehouse_id) فيتحط تلقائيًا في مكان الاستلام الافتراضي بتاعه،
    أو في مكان محدد (location_id). بعد ما المستند يتقفل الموظف بينقل الصنف لمكانه النهائي (الرص) من صفحة النقل.
    مفيش بضاعة بتدخل من غير مكان. idempotency_key اختياري: نفس المفتاح = نفس المستند (200).
    """
    if not payload.items:
        raise HTTPException(status_code=400, detail="لازم تضيف منتج واحد على الأقل في الإذن")
    _check_items_basic(payload.items)
    _check_supplier(db, payload.supplier_id)

    def _tx():
        idx = LocationIndex(db)
        # بنحدد مكان كل صنف الأول (أخطاء المخزن/المكان تبان قبل ما نلمس أي حاجة)
        targets = [resolve_receiving_location(idx, item.warehouse_id, item.location_id) for item in payload.items]
        _load_products(db, payload.items)  # قفل المنتجات بترتيب ثابت + التأكد إنها مفعّلة

        invoice = models.StockInInvoice(
            supplier_id=payload.supplier_id,
            received_by_id=current_user.id,
            received_by_username=current_user.username,
            notes=payload.notes,
            date=now_cairo(),
            status="completed",
            idempotency_key=payload.idempotency_key,
        )
        db.add(invoice)
        db.flush()  # عشان ناخد الـ id (وعشان unique الـ idempotency يترفض هنا قبل أي حركة مخزون)

        for item, target in zip(payload.items, targets):
            db.add(models.StockInItem(
                invoice_id=invoice.id, product_id=item.product_id, quantity=float(q3(item.quantity)),
                unit_price=item.unit_price, location_id=target.id,
            ))
            inventory.add_stock(db, item.product_id, target.id, item.quantity)
        audit(db, current_user, "stock_in", invoice.id, "create",
              {"items": len(payload.items), "supplier_id": payload.supplier_id})
        db.commit()
        db.refresh(invoice)
        return invoice

    invoice, _replayed = _run_idempotent(db, models.StockInInvoice, payload.idempotency_key, current_user, _tx)
    return _in_invoice_to_out(invoice, show_finance=permissions.has_permission(current_user, "finance.view"))


def _check_supplier(db: Session, supplier_id: Optional[int]) -> None:
    if supplier_id and not db.query(models.Supplier.id).filter(models.Supplier.id == supplier_id).first():
        raise HTTPException(status_code=404, detail="المورد مش موجود")


@router.post("/in/{invoice_id}/void", response_model=schemas.StockInInvoiceOut)
def void_stock_in(
    invoice_id: int,
    payload: schemas.VoidRequest,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(auth.require_permission("invoices.void")),
):
    """
    إلغاء إذن إضافة (بسبب إلزامي): بيعكس الزيادة من نفس المواقع. لو هيسبب رصيد سالب (البضاعة اتصرفت/اتنقلت/اتحجزت)
    أو الموقع اتمسح ← 409 برسالة فيها **كل** الأصناف والمواقع المتأثرة (المتاح والمطلوب). المستند بيفضل ظاهر بعلامة "ملغاة".
    """
    def _tx():
        inv = db.query(models.StockInInvoice).filter(models.StockInInvoice.id == invoice_id).with_for_update().first()
        if not inv:
            raise HTTPException(status_code=404, detail="المستند مش موجود")
        if inv.status != "completed":
            raise HTTPException(status_code=409, detail="المستند ده ملغي بالفعل")
        idx = LocationIndex(db)
        back: Dict[Tuple[int, int], Decimal] = {}
        legacy: Dict[int, Decimal] = {}
        for it in sorted(inv.items, key=lambda x: x.id):
            if it.location_id is None:
                legacy[it.product_id] = legacy.get(it.product_id, Decimal(0)) + q3(it.quantity)
                continue
            if it.location_id not in idx.locations:
                raise HTTPException(status_code=409, detail="مينفعش نلغي الإذن: الموقع اللي اتخزنت فيه البضاعة اتمسح")
            key = (it.product_id, it.location_id)
            back[key] = back.get(key, Decimal(0)) + q3(it.quantity)
        lock_products(db, sorted({k[0] for k in back} | set(legacy)))
        bad = inventory.shortages(db, back)
        if bad:
            raise HTTPException(
                status_code=409,
                detail="مينفعش نلغي الإذن لأن الكميات دي اتصرفت أو اتنقلت أو اتحجزت: " + inventory.shortages_message(bad),
            )
        for (product_id, location_id), qty in sorted(back.items()):
            inventory.remove_stock(db, product_id, location_id, qty)
        for product_id, qty in sorted(legacy.items()):
            inventory.adjust_product_total_only(db, product_id, -qty)
        inv.status = "voided"
        inv.voided_at, inv.voided_by_id, inv.voided_by_username = now_cairo(), current_user.id, current_user.username
        inv.void_reason = payload.reason
        audit(db, current_user, "stock_in", inv.id, "void", {"reason": payload.reason, "items": len(inv.items)})
        db.commit()
        db.refresh(inv)
        return inv

    return _in_invoice_to_out(inventory.with_retry(db, _tx), show_finance=permissions.has_permission(current_user, "finance.view"))


# =====================================================================
#  إذن صرف: quote → reserved → completed (+ voided)
# =====================================================================
@router.post("/out", response_model=schemas.StockOutInvoiceOut)
def add_stock_out(
    payload: schemas.StockOutInvoiceCreate,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(auth.require_any_permission(*permissions.OUT_CREATE)),
):
    """
    إنشاء إذن صرف. السعر دايمًا من سعر المنتج وقت الإنشاء (أي unit_price من العميل بيتتجاهل).
      status=quote     عرض سعر: من غير أي تأثير على الرصيد أو المحجوز.
      status=reserved  حجز: بيقلل المتاح بس (الفعلي ما بيتغيرش) من الأماكن المحددة.
      status=completed صرف مباشر (الافتراضي): بينقص الفعلي.
    كل حالة ليها صلاحيتها. الخصم/الضريبة محتاجين stock_out.discount. idempotency_key اختياري.
    """
    if not payload.items:
        raise HTTPException(status_code=400, detail="لازم تضيف منتج واحد على الأقل في المستند")
    _need(current_user, OUT_CREATE_PERM[payload.status])
    _check_items_basic(payload.items)
    _check_discount_permission(current_user, payload)
    _check_recipient(db, payload.recipient_id)
    status = payload.status

    def _tx():
        idx = LocationIndex(db)
        products = _load_products(db, payload.items, lock=status != "quote")
        requested = _plan_out_lines(idx, products, payload.items, require_location=status != "quote")
        prices = [float(products[i.product_id].price or 0) for i in payload.items]
        totals = invoice_calc.compute_totals(
            [(i.quantity, pr) for i, pr in zip(payload.items, prices)],
            payload.discount_type, payload.discount_value, payload.tax_type, payload.tax_value,
        )
        if status != "quote":
            _require_available(db, requested)

        now = now_cairo()
        invoice = models.StockOutInvoice(
            recipient_id=payload.recipient_id, delivered_by_id=current_user.id, delivered_by_username=current_user.username,
            notes=payload.notes, date=now, created_at=now, status=status, idempotency_key=payload.idempotency_key,
            completed_at=now if status == "completed" else None,
        )
        _set_totals(invoice, totals)
        db.add(invoice)
        db.flush()
        _add_out_items(db, invoice, payload.items, prices)
        for (product_id, location_id), qty in sorted(requested.items()):
            if status == "reserved":
                inventory.reserve_stock(db, product_id, location_id, qty)
            elif status == "completed":
                inventory.remove_stock(db, product_id, location_id, qty)
        db.flush()
        db.refresh(invoice)
        audit(db, current_user, "stock_out", invoice.id, {"quote": "create_quote", "reserved": "create_reserved", "completed": "create"}[status],
              _out_summary(invoice))
        db.commit()
        db.refresh(invoice)
        return invoice

    invoice, _replayed = _run_idempotent(db, models.StockOutInvoice, payload.idempotency_key, current_user, _tx)
    return _out_invoice_to_out(invoice)


@router.put("/out/{invoice_id}", response_model=schemas.StockOutInvoiceOut)
def update_stock_out(
    invoice_id: int,
    payload: schemas.StockOutInvoiceUpdate,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(auth.require_any_permission("stock_out.quote", "stock_out.reserve")),
):
    """
    تعديل عرض سعر (stock_out.quote) أو حجز (stock_out.reserve). المستند التمّ أو الملغي مقفول.
    تعديل الحجز: بنفك الحجز القديم ثم نتحقق من التوافر ونحجز القيم الجديدة في transaction واحدة (لو فشل كل حاجة بترجع).
    الأسطر القديمة بتحتفظ بسعرها المسجّل، والأسطر الجديدة بتاخد سعر المنتج الحالي.
    """
    if not payload.items:
        raise HTTPException(status_code=400, detail="لازم تضيف منتج واحد على الأقل في المستند")
    _check_items_basic(payload.items)
    _check_discount_permission(current_user, payload)
    _check_recipient(db, payload.recipient_id)

    def _tx():
        invoice = _lock_out_invoice(db, invoice_id, ("quote", "reserved"), "تعديل المستند")
        is_reserved = invoice.status == "reserved"
        _need(current_user, "stock_out.reserve" if is_reserved else "stock_out.quote")
        idx = LocationIndex(db)
        old_items = sorted(invoice.items, key=lambda it: it.id)
        old_prices = {it.product_id: it.unit_price for it in old_items if it.unit_price is not None}
        old_ids = {it.product_id for it in old_items}

        ids = sorted(old_ids | {i.product_id for i in payload.items})
        products = lock_products(db, ids) if is_reserved else {
            p.id: p for p in db.query(models.Product).filter(models.Product.id.in_(ids)).all()}
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
        requested = _plan_out_lines(idx, products, payload.items, require_location=is_reserved)
        before = _out_summary(invoice)
        if is_reserved:
            for (product_id, location_id), qty in sorted(_reserved_of(invoice).items()):
                if location_id not in idx.locations:
                    raise HTTPException(status_code=409, detail="مكان الحجز القديم اتمسح - فك الحجز وأنشئ حجز جديد")
                inventory.release_reservation(db, product_id, location_id, qty)
            _require_available(db, requested)
        invoice.items.clear()
        db.flush()
        _add_out_items(db, invoice, payload.items, prices)
        if is_reserved:
            for (product_id, location_id), qty in sorted(requested.items()):
                inventory.reserve_stock(db, product_id, location_id, qty)
        invoice.recipient_id = payload.recipient_id
        invoice.notes = payload.notes
        _set_totals(invoice, totals)
        db.flush()
        db.refresh(invoice)
        audit(db, current_user, "stock_out", invoice.id, "update_reserved" if is_reserved else "update_quote",
              {"before": before, "after": _out_summary(invoice)})
        db.commit()
        db.refresh(invoice)
        return invoice

    return _out_invoice_to_out(inventory.with_retry(db, _tx))


@router.post("/out/{invoice_id}/reserve", response_model=schemas.StockOutInvoiceOut)
def reserve_quote(
    invoice_id: int,
    payload: Optional[schemas.ReserveRequest] = Body(default=None),
    db: Session = Depends(get_db),
    current_user: models.User = Depends(auth.require_permission("stock_out.reserve")),
):
    """تحويل عرض سعر لحجز: بنتحقق من المتاح في كل موقع لكل صنف ونحجز (المتاح بس بيقل، الفعلي ثابت). اختياري: تحديد أماكن السحب."""
    def _tx():
        invoice = _lock_out_invoice(db, invoice_id, ("quote",), "حجز المستند")
        by_id = {it.id: it for it in invoice.items}
        for ov in (payload.locations if payload else []):
            if ov.item_id not in by_id:
                raise HTTPException(status_code=400, detail="سطر غير موجود في المستند")
            by_id[ov.item_id].location_id = ov.location_id
        idx = LocationIndex(db)
        items = sorted(invoice.items, key=lambda it: it.id)
        products = _load_products(db, items)
        requested = _plan_out_lines(idx, products, items, require_location=True)
        _require_available(db, requested)
        for (product_id, location_id), qty in sorted(requested.items()):
            inventory.reserve_stock(db, product_id, location_id, qty)
        invoice.status = "reserved"
        audit(db, current_user, "stock_out", invoice.id, "reserve", _out_summary(invoice))
        db.commit()
        db.refresh(invoice)
        return invoice

    return _out_invoice_to_out(inventory.with_retry(db, _tx))


@router.post("/out/{invoice_id}/release", response_model=schemas.StockOutInvoiceOut)
def release_reservation(
    invoice_id: int,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(auth.require_permission("stock_out.reserve")),
):
    """فك الحجز: الكميات بترجع متاحة والمستند بيرجع عرض سعر (لسه قابل للتعديل أو الحجز تاني)."""
    def _tx():
        invoice = _lock_out_invoice(db, invoice_id, ("reserved",), "فك الحجز")
        lock_products(db, sorted({it.product_id for it in invoice.items}))
        for (product_id, location_id), qty in sorted(_reserved_of(invoice).items()):
            inventory.release_reservation(db, product_id, location_id, qty)
        invoice.status = "quote"
        audit(db, current_user, "stock_out", invoice.id, "release", _out_summary(invoice))
        db.commit()
        db.refresh(invoice)
        return invoice

    return _out_invoice_to_out(inventory.with_retry(db, _tx))


@router.post("/out/{invoice_id}/complete", response_model=schemas.StockOutInvoiceOut)
def complete_reservation(
    invoice_id: int,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(auth.require_permission("stock_out.complete")),
):
    """
    إتمام الحجز: الكميات المحجوزة بتتحوّل لصرف فعلي من نفس المواقع (الفعلي بينقص والحجز بينتهي في نفس الـ UPDATE).
    لو الموقع اتمسح أو الرصيد/الحجز اتغيّر بشكل يمنع التنفيذ ← 409 ومفيش حاجة بتتغيّر. تاريخ المستند بيتحدّث لوقت الصرف الفعلي.
    """
    def _tx():
        invoice = _lock_out_invoice(db, invoice_id, ("reserved",), "إتمام المستند")
        idx = LocationIndex(db)
        reserved = _reserved_of(invoice)
        if any(it.location_id is None for it in invoice.items):
            raise HTTPException(status_code=409, detail="الحجز فيه سطر من غير مكان - فك الحجز وراجعه")
        for (_, location_id) in reserved:
            if location_id not in idx.locations:
                raise HTTPException(status_code=409, detail="مكان الحجز اتمسح - مينفعش نكمل الصرف. فك الحجز وأنشئ حجز جديد")
        lock_products(db, sorted({it.product_id for it in invoice.items}))
        for (product_id, location_id), qty in sorted(reserved.items()):
            inventory.fulfill_reservation(db, product_id, location_id, qty)
        now = now_cairo()
        invoice.status, invoice.completed_at, invoice.date = "completed", now, now
        audit(db, current_user, "stock_out", invoice.id, "complete", _out_summary(invoice))
        db.commit()
        db.refresh(invoice)
        return invoice

    return _out_invoice_to_out(inventory.with_retry(db, _tx))


def _copy_as_quote(db: Session, source: models.StockOutInvoice, user: models.User) -> models.StockOutInvoice:
    """نسخة جديدة كعرض سعر (من غير أي حركة مخزون): نفس الأصناف/الجهة/الملاحظات، بأسعار المنتج الحالية ومن غير خصم/ضريبة."""
    idx = LocationIndex(db)
    products = {p.id: p for p in db.query(models.Product).filter(
        models.Product.id.in_({it.product_id for it in source.items})).all()}
    items = sorted(source.items, key=lambda it: it.id)
    prices = [float(products[it.product_id].price or 0) if it.product_id in products else float(it.unit_price or 0) for it in items]
    totals = invoice_calc.compute_totals([(it.quantity, pr) for it, pr in zip(items, prices)])
    now = now_cairo()
    copy = models.StockOutInvoice(
        recipient_id=source.recipient_id, delivered_by_id=user.id, delivered_by_username=user.username,
        notes=source.notes, date=now, created_at=now, status="quote", copied_from_id=source.id,
    )
    _set_totals(copy, totals)
    db.add(copy)
    db.flush()
    for it, pr in zip(items, prices):
        db.add(models.StockOutItem(
            invoice_id=copy.id, product_id=it.product_id, quantity=it.quantity, unit_price=pr,
            location_id=it.location_id if it.location_id in idx.locations else None,
        ))
    db.flush()
    db.refresh(copy)
    return copy


@router.post("/out/{invoice_id}/void", response_model=schemas.StockOutInvoiceOut)
def void_stock_out(
    invoice_id: int,
    payload: schemas.VoidRequest,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(auth.require_any_permission("invoices.void", "stock_out.quote", "stock_out.reserve")),
):
    """
    إلغاء إذن صرف (بسبب إلزامي ≥ 3 حروف). المستند مش بيتمسح: بيتعلّم "ملغاة" مع من/إمتى/ليه.
      عرض سعر: من غير حركة.   محجوزة: فك الحجز.   تمت: إرجاع الكميات لنفس المواقع (الموقع اتمسح ← 409).
    إلغاء المستند التمّ محتاج invoices.void. recreate=true: بينشئ في نفس الـ transaction نسخة جديدة كعرض سعر (stock_out.quote).
    """
    if payload.recreate:
        _need(current_user, "stock_out.quote")

    def _tx():
        invoice = _lock_out_invoice(db, invoice_id, ("quote", "reserved", "completed"), "إلغاء المستند")
        status = invoice.status
        can_void = permissions.has_permission(current_user, "invoices.void")
        if status == "completed" and not can_void:
            raise HTTPException(status_code=403, detail=f"ملكش صلاحية: {permissions.label_of('invoices.void')}")
        if status == "reserved" and not (can_void or permissions.has_permission(current_user, "stock_out.reserve")):
            raise HTTPException(status_code=403, detail=f"ملكش صلاحية: {permissions.label_of('stock_out.reserve')}")
        if status == "quote" and not (can_void or permissions.has_permission(current_user, "stock_out.quote")):
            raise HTTPException(status_code=403, detail=f"ملكش صلاحية: {permissions.label_of('stock_out.quote')}")
        idx = LocationIndex(db)
        if status in ("reserved", "completed"):
            lines = _reserved_of(invoice)
            legacy: Dict[int, Decimal] = {}
            for it in invoice.items:
                if it.location_id is None:
                    legacy[it.product_id] = legacy.get(it.product_id, Decimal(0)) + q3(it.quantity)
            for (_, location_id) in lines:
                if location_id not in idx.locations:
                    raise HTTPException(status_code=409, detail="مينفعش نلغي المستند: المكان اللي اتسحبت منه البضاعة اتمسح")
            lock_products(db, sorted({it.product_id for it in invoice.items}))
            if status == "reserved":
                for (product_id, location_id), qty in sorted(lines.items()):
                    inventory.release_reservation(db, product_id, location_id, qty)
            else:
                for (product_id, location_id), qty in sorted(lines.items()):
                    inventory.add_stock(db, product_id, location_id, qty)
                for product_id, qty in sorted(legacy.items()):
                    inventory.adjust_product_total_only(db, product_id, qty)
        invoice.status = "voided"
        invoice.voided_at, invoice.voided_by_id, invoice.voided_by_username = now_cairo(), current_user.id, current_user.username
        invoice.void_reason = payload.reason
        recreated = _copy_as_quote(db, invoice, current_user) if payload.recreate else None
        audit(db, current_user, "stock_out", invoice.id, "void",
              {"from_status": status, "reason": payload.reason, "recreated_id": recreated.id if recreated else None})
        if recreated is not None:
            audit(db, current_user, "stock_out", recreated.id, "create_quote", {"copied_from": invoice.id})
        db.commit()
        db.refresh(invoice)
        return invoice, (recreated.id if recreated else None)

    invoice, recreated_id = inventory.with_retry(db, _tx)
    out = _out_invoice_to_out(invoice)
    out.recreated_invoice_id = recreated_id
    return out


@router.post("/out/{invoice_id}/recreate", response_model=schemas.StockOutInvoiceOut)
def recreate_voided_out(
    invoice_id: int,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(auth.require_permission("invoices.void", "stock_out.quote")),
):
    """نسخة جديدة كعرض سعر (مش حركة مخزون) من إذن صرف ملغي."""
    inv = db.query(models.StockOutInvoice).filter(models.StockOutInvoice.id == invoice_id).first()
    if not inv:
        raise HTTPException(status_code=404, detail="المستند مش موجود")
    if inv.status != "voided":
        raise HTTPException(status_code=409, detail="إعادة الإنشاء متاحة للمستندات الملغاة بس")
    copy = _copy_as_quote(db, inv, current_user)
    audit(db, current_user, "stock_out", copy.id, "create_quote", {"copied_from": inv.id})
    db.commit()
    db.refresh(copy)
    return _out_invoice_to_out(copy)


@router.get("/out/{invoice_id}/edit-data")
def out_edit_data(
    invoice_id: int,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(auth.require_any_permission("stock_out.quote", "stock_out.reserve")),
):
    """
    كل اللي شاشة تعديل عرض سعر/حجز محتاجاه في ضربة واحدة: الأسطر ببيانات منتجاتها وأماكن السحب المتاحة لكل منتج.
    المتاح = الفعلي - المحجوز (ولو المستند محجوز، حجزه هو نفسه بيتحسب متاح لأنه بيتفك وقت الحفظ).
    """
    inv = db.query(models.StockOutInvoice).filter(models.StockOutInvoice.id == invoice_id).first()
    if not inv:
        raise HTTPException(status_code=404, detail="المستند مش موجود")
    if inv.status not in ("quote", "reserved"):
        raise HTTPException(status_code=409, detail="المستند ده مقفول - مينفعش يتعدّل")
    items = sorted(inv.items, key=lambda it: it.id)
    pids = {it.product_id for it in items}
    idx = LocationIndex(db)

    products = {p.id: p for p in db.query(models.Product).filter(models.Product.id.in_(pids)).all()} if pids else {}
    PL = models.ProductLocation
    avail: Dict[int, Dict[int, float]] = {}
    reserved_total: Dict[int, float] = {}
    if pids:
        for pid, lid, qty, res in db.query(PL.product_id, PL.location_id, PL.quantity, PL.reserved).filter(PL.product_id.in_(pids)).all():
            avail.setdefault(pid, {})[lid] = qty - (res or 0)
            reserved_total[pid] = reserved_total.get(pid, 0) + (res or 0)
    own: Dict[int, float] = {}
    if inv.status == "reserved":
        for it in items:
            own[it.product_id] = own.get(it.product_id, 0) + it.quantity
            if it.location_id is not None:
                slot = avail.setdefault(it.product_id, {})
                slot[it.location_id] = slot.get(it.location_id, 0) + it.quantity

    lines = []
    for it in items:
        p = products.get(it.product_id)
        lines.append({
            "item_id": it.id,
            "product_id": it.product_id,
            "product_name": p.name if p else None,
            "sku": p.sku if p else None,
            "unit": p.unit if p else None,
            "product_price": p.price if p else None,
            "product_quantity": _round_qty((p.quantity if p else 0)),
            "product_available": _round_qty((p.quantity if p else 0) - reserved_total.get(it.product_id, 0) + own.get(it.product_id, 0)),
            "quantity": it.quantity,
            "unit_price": it.unit_price,
            "location_id": it.location_id,
            "locations": [
                {"location_id": lid, "full_code": idx.full_code(lid), "quantity": _round_qty(q)}
                for lid, q in sorted(avail.get(it.product_id, {}).items()) if lid in idx.locations and q > 0
            ],
        })
    return {
        "id": inv.id, "status": inv.status, "recipient_id": inv.recipient_id,
        "recipient_name": inv.recipient.name if inv.recipient else None, "notes": inv.notes,
        "discount_type": inv.discount_type, "discount_value": inv.discount_value,
        "tax_type": inv.tax_type, "tax_value": inv.tax_value,
        "lines": lines,
    }


# =====================================================================
#  قوائم المستندات (كل الحالات بعلامة حالة - مش حركة مخزون فعلية)
# =====================================================================
@router.get("/in")
def list_stock_in(
    page: int = 1,
    page_size: int = INVOICE_LIST_PAGE_SIZE_DEFAULT,
    status: Optional[str] = None,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(auth.require_permission("invoices.view")),
):
    """ملخص إذونات الإضافة (من غير أصناف) بترقيم من الـ DB. الأصناف: GET /api/stock/in/{id}."""
    page = _clamp_page(page)
    page_size = _clamp_page_size(page_size, INVOICE_LIST_PAGE_SIZE_DEFAULT, INVOICE_LIST_PAGE_SIZE_MAX)
    base_query = db.query(models.StockInInvoice)
    if status in ("completed", "voided"):
        base_query = base_query.filter(models.StockInInvoice.status == status)
    total = base_query.count()
    rows = (
        base_query.options(joinedload(models.StockInInvoice.supplier), joinedload(models.StockInInvoice.received_by),
                           joinedload(models.StockInInvoice.voided_by), selectinload(models.StockInInvoice.items))
        .order_by(models.StockInInvoice.date.desc(), models.StockInInvoice.id.desc())
        .offset((page - 1) * page_size).limit(page_size).all()
    )
    return {
        "items": [_in_invoice_to_out(r, items_limit=0, show_finance=permissions.has_permission(current_user, "finance.view")) for r in rows],
        "total": total, "page": page, "page_size": page_size, "total_pages": max(1, math.ceil(total / page_size)),
    }


@router.get("/in/{invoice_id}", response_model=schemas.StockInInvoiceOut)
def get_stock_in_invoice(
    invoice_id: int,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(auth.require_permission("invoices.view")),
):
    inv = db.query(models.StockInInvoice).filter(models.StockInInvoice.id == invoice_id).first()
    if not inv:
        raise HTTPException(status_code=404, detail="المستند مش موجود")
    return _in_invoice_to_out(inv, show_finance=permissions.has_permission(current_user, "finance.view"))


@router.get("/out")
def list_stock_out(
    page: int = 1,
    page_size: int = INVOICE_LIST_PAGE_SIZE_DEFAULT,
    status: Optional[str] = None,  # quote / reserved / completed / voided
    db: Session = Depends(get_db),
    current_user: models.User = Depends(auth.require_permission("invoices.view")),
):
    """ملخص إذونات الصرف بكل حالاتها (عرض سعر/محجوزة/تمت/ملغاة) من غير أصناف."""
    page = _clamp_page(page)
    page_size = _clamp_page_size(page_size, INVOICE_LIST_PAGE_SIZE_DEFAULT, INVOICE_LIST_PAGE_SIZE_MAX)
    base_query = db.query(models.StockOutInvoice)
    if status in OUT_STATUSES:
        base_query = base_query.filter(models.StockOutInvoice.status == status)
    total = base_query.count()
    rows = (
        base_query.options(joinedload(models.StockOutInvoice.recipient), joinedload(models.StockOutInvoice.delivered_by),
                           joinedload(models.StockOutInvoice.voided_by), selectinload(models.StockOutInvoice.items))
        .order_by(models.StockOutInvoice.date.desc(), models.StockOutInvoice.id.desc())
        .offset((page - 1) * page_size).limit(page_size).all()
    )
    return {
        "items": [_out_invoice_to_out(r, items_limit=0) for r in rows],
        "total": total, "page": page, "page_size": page_size, "total_pages": max(1, math.ceil(total / page_size)),
    }


@router.get("/out/{invoice_id}", response_model=schemas.StockOutInvoiceOut)
def get_stock_out_invoice(
    invoice_id: int,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(auth.require_permission("invoices.view")),
):
    inv = db.query(models.StockOutInvoice).filter(models.StockOutInvoice.id == invoice_id).first()
    if not inv:
        raise HTTPException(status_code=404, detail="المستند مش موجود")
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
    include_voided: int = 0,  # 1 = إظهار المستندات الملغاة (بعلامة) في الصفوف؛ الإجماليات بتستبعدها دايمًا
    page: int = 1,
    page_size: int = LOG_PAGE_SIZE_DEFAULT,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(auth.require_any_permission("invoices.view", "transfers.view")),
):
    """
    سجل الحركة الفعلية (إذونات إضافة وصرف **مكتملة** + حركات نقل). عروض الأسعار والحجوزات مش حركة مخزون فعلية فمبتظهرش هنا
    (بتظهر في قائمة المستندات بعلامة حالة). include_voided=1 بيظهر الملغي بعلامة، ومبيدخلش في الإجماليات.

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

    counts = {"in": 0, "out": 0, "transfer": 0}  # عدد المستندات الصالحة (للإجماليات)
    row_counts = {"in": 0, "out": 0, "transfer": 0}  # عدد الصفوف المعروضة (للترقيم، بيشمل الملغي لو include_voided)
    values = {"in": 0.0, "out": 0.0}
    out_discount = 0.0
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
        q_rows = _apply(db.query(Inv), joins, conds + [movement_invoice(Inv, bool(include_voided))])
        row_counts[k] = q_rows.with_entities(func.count(Inv.id)).scalar() or 0
        q_valid = _apply(db.query(Inv), joins, conds + [valid_invoice(Inv)])
        if k == "in":
            counts[k] = q_valid.with_entities(func.count(Inv.id)).scalar() or 0
            if counts[k]:
                ids_sq = _apply(db.query(Inv.id), joins, conds + [valid_invoice(Inv)]).subquery()
                values[k] = float(
                    db.query(func.coalesce(func.sum(Item.quantity * func.coalesce(Item.unit_price, 0)), 0))
                    .filter(Item.invoice_id.in_(select(ids_sq.c.id)))
                    .scalar() or 0
                )
        else:
            n, gross, disc = q_valid.with_entities(
                func.count(Inv.id), func.coalesce(func.sum(Inv.subtotal_amount), 0), func.coalesce(func.sum(Inv.discount_amount), 0)
            ).one()
            counts[k], values[k], out_discount = int(n or 0), float(gross or 0), float(disc or 0)
        if row_counts[k]:
            add_branch(k, q_rows, Inv.id, Inv.date)

    # ---- حركات النقل بين المواقع (بتظهر في نفس السجل) ----
    idx = None
    if want["transfer"]:
        T = models.LocationTransfer
        idx = LocationIndex(db)
        joins, conds = _transfer_source(db, idx, start, end_excl, party, user)
        q = _apply(db.query(T), joins, conds)
        t_count, t_qty = q.with_entities(func.count(T.id), func.coalesce(func.sum(T.quantity), 0)).one()
        counts["transfer"] = row_counts["transfer"] = int(t_count or 0)
        transfers_qty = float(t_qty or 0)
        if counts["transfer"]:
            add_branch("transfer", q, T.id, T.date)

    total_filtered = sum(row_counts.values())

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
                "status": inv.status or "completed", "void_reason": inv.void_reason,
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
                "notes": inv.notes, "items": [], "items_total": n,
                "total_value": inv.subtotal_amount if inv.subtotal_amount is not None else val,  # قبل الخصم
                "discount_amount": inv.discount_amount or 0.0,
                "net_amount": round((inv.subtotal_amount if inv.subtotal_amount is not None else val) - (inv.discount_amount or 0.0), 2),
                "tax_amount": inv.tax_amount or 0.0, "grand_total": inv.grand_total,
                "status": inv.status or "completed", "void_reason": inv.void_reason,
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
            "out_total_value": round(values["out"], 2),  # إجمالي قيمة الأصناف قبل الخصم (مش "مبيعات")
            "out_discount_total": round(out_discount, 2),
            "out_net_value": round(values["out"] - out_discount, 2),  # صافي القيمة بعد الخصم
            "transfers_count": counts["transfer"],
            "transfers_quantity": round(transfers_qty, 3) if counts["transfer"] else 0,  # int لما مفيش حركات (نفس نوع النسخة القديمة)
        },
    }
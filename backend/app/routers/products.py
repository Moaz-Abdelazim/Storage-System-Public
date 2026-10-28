from datetime import datetime
from ..timezone_utils import now_cairo

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func
from sqlalchemy.orm import Session, selectinload, joinedload
from typing import List, Optional

from .. import models, schemas, auth
from ..database import get_db
from ..location_utils import LocationIndex, validate_location_rows, lock_products, require_leaf_location
from ..display_utils import format_user_display
from ..query_helpers import clamp_page, clamp_page_size, contains, total_pages, page_args, paged

router = APIRouter(prefix="/api/products", tags=["Products"])


def _round_qty(value: float) -> float:
    """تقريب لـ 3 خانات عشرية عشان نتفادى أخطاء الفاصلة العائمة التراكمية."""
    return round(value, 3)


def db_of(p):
    from sqlalchemy.orm import object_session
    return object_session(p)


def _to_out(p: models.Product, idx: Optional[LocationIndex] = None, scope_ids=None) -> schemas.ProductOut:
    """idx: فهرس المواقع (بيتحمّل مرة واحدة للقايمة كلها). scope_ids: نطاق فلتر الموقع لو فيه."""
    if idx is None:
        idx = LocationIndex(db_of(p))
    locs = sorted((pl for pl in p.locations if pl.quantity > 0), key=lambda pl: idx.full_code(pl.location_id))
    assigned = _round_qty(sum(pl.quantity for pl in locs))
    return schemas.ProductOut(
        id=p.id,
        sku=p.sku,
        name=p.name,
        category_id=p.category_id,
        category_name=p.category.name if p.category else None,
        brand_id=p.brand_id,
        brand_name=p.brand.name if p.brand else None,
        unit=p.unit,
        price=p.price,
        quantity=p.quantity,
        min_quantity=p.min_quantity,
        updated_at=p.updated_at,
        is_active=bool(p.is_active) if p.is_active is not None else True,
        locations=[
            schemas.ProductLocationOut(location_id=pl.location_id, full_code=idx.full_code(pl.location_id), quantity=pl.quantity)
            for pl in locs
        ],
        unassigned_quantity=max(0.0, _round_qty((p.quantity or 0) - assigned)),
        scoped_quantity=_round_qty(sum(pl.quantity for pl in locs if pl.location_id in scope_ids)) if scope_ids is not None else None,
    )


_SORTS = {
    "name": models.Product.name,
    "sku": models.Product.sku,
    "quantity": models.Product.quantity,
    "price": models.Product.price,
    "updated": models.Product.updated_at,
}


@router.get("")
def list_products(
    search: Optional[str] = None,
    category_id: Optional[int] = None,
    brand_id: Optional[int] = None,
    low_stock_only: bool = False,
    stock: Optional[str] = None,  # low (تحت الحد الأدنى أو نفد) / out (نفد) / ok (فوق الحد الأدنى)
    warehouse_id: Optional[int] = None,
    location_id: Optional[int] = None,
    status: str = "active",  # active (الافتراضي) / inactive / all
    page: Optional[int] = None,  # من غير page = القايمة كلها (للتوافق مع الاستخدامات القديمة)
    page_size: int = 25,
    sort: str = "name",
    order: str = "asc",
    db: Session = Depends(get_db),
    current_user: models.User = Depends(auth.get_current_user),
):
    """
    قايمة المنتجات. مع page/page_size الترقيم والترتيب بيحصلوا في قاعدة البيانات
    (بيرجّع {items, total, page, page_size, total_pages})، ومن غيرهم بيرجّع القايمة كلها زي الأول.
    """
    idx = LocationIndex(db)
    scope = idx.scope_ids(warehouse_id, location_id)  # None = من غير فلتر موقع
    P = models.Product
    query = db.query(P).options(selectinload(P.locations), joinedload(P.category), joinedload(P.brand))
    if status == "active":
        query = query.filter(P.is_active.is_(True))
    elif status == "inactive":
        query = query.filter(P.is_active.is_(False))
    elif status != "all":
        raise HTTPException(status_code=400, detail="قيمة الحالة لازم تكون active أو inactive أو all")
    if scope is not None:
        # المنتجات اللي ليها كمية فعلية في الموقع المختار أو أي موقع تحته
        in_scope = db.query(models.ProductLocation.product_id).filter(
            models.ProductLocation.location_id.in_(scope), models.ProductLocation.quantity > 0
        )
        query = query.filter(P.id.in_(in_scope))
    if search and search.strip():
        query = query.filter(contains(P.name, search) | contains(P.sku, search))
    if category_id:
        query = query.filter(P.category_id == category_id)
    if brand_id:
        query = query.filter(P.brand_id == brand_id)
    if low_stock_only or stock == "low":
        query = query.filter(P.quantity <= P.min_quantity)
    elif stock == "out":
        query = query.filter(P.quantity <= 0)
    elif stock == "ok":
        query = query.filter(P.quantity > P.min_quantity)
    elif stock not in (None, ""):
        raise HTTPException(status_code=400, detail="قيمة المخزون لازم تكون low أو out أو ok")

    sort_col = _SORTS.get(sort, P.name)
    ordering = sort_col.desc() if order == "desc" else sort_col.asc()
    query = query.order_by(ordering, P.id)

    if page is None:
        return [_to_out(p, idx, scope) for p in query.all()]

    page = clamp_page(page)
    page_size = clamp_page_size(page_size, 25, 200)
    total = query.count()
    rows = query.offset((page - 1) * page_size).limit(page_size).all()
    return {
        "items": [_to_out(p, idx, scope) for p in rows],
        "total": total,
        "page": page,
        "page_size": page_size,
        "total_pages": total_pages(total, page_size),
    }


@router.get("/lookup")
def lookup_products(
    q: str = "",
    limit: int = 20,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(auth.get_current_user),
):
    """بحث سريع وخفيف (للـ autocomplete في الفواتير والنقل): منتجات مفعّلة بس، وأقصى عدد محدود."""
    limit = max(1, min(limit, 50))
    P = models.Product
    query = db.query(P).filter(P.is_active.is_(True))
    if q and q.strip():
        query = query.filter(contains(P.name, q) | contains(P.sku, q))
    rows = query.order_by(P.name).limit(limit).all()
    return [
        {"id": p.id, "sku": p.sku, "name": p.name, "unit": p.unit, "price": p.price, "quantity": p.quantity,
         "min_quantity": p.min_quantity}
        for p in rows
    ]


@router.get("/{product_id}/movements")
def product_movements_paged(
    product_id: int,
    kind: str = "in",  # in (وارد) / out (صادر)
    page: int = 1,
    page_size: int = 10,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(auth.get_current_user),
):
    """حركات الوارد أو الصادر لمنتج واحد - بترقيم من قاعدة البيانات (بدل تحميل التاريخ كله)."""
    if kind not in ("in", "out"):
        raise HTTPException(status_code=400, detail="kind لازم تكون in أو out")
    if not db.query(models.Product.id).filter(models.Product.id == product_id).first():
        raise HTTPException(status_code=404, detail="المنتج مش موجود")
    pg, size = page_args(page, page_size, default=10)
    if kind == "in":
        Item, Inv, party_rel, user_rel = models.StockInItem, models.StockInInvoice, models.StockInInvoice.supplier, models.StockInInvoice.received_by
    else:
        Item, Inv, party_rel, user_rel = models.StockOutItem, models.StockOutInvoice, models.StockOutInvoice.recipient, models.StockOutInvoice.delivered_by
    q = db.query(Item).join(Inv, Inv.id == Item.invoice_id).filter(Item.product_id == product_id)
    total = q.count()
    total_qty = db.query(func.coalesce(func.sum(Item.quantity), 0)).filter(Item.product_id == product_id).scalar() or 0
    rows = (
        q.options(joinedload(Item.invoice).joinedload(party_rel), joinedload(Item.invoice).joinedload(user_rel))
        .order_by(Inv.date.desc(), Item.id.desc()).offset((pg - 1) * size).limit(size).all()
    )
    items = []
    for it in rows:
        inv = it.invoice
        party = inv.supplier if kind == "in" else inv.recipient
        user = inv.received_by if kind == "in" else inv.delivered_by
        uname = inv.received_by_username if kind == "in" else inv.delivered_by_username
        items.append({
            "invoice_id": it.invoice_id, "date": inv.date, "quantity": it.quantity, "unit_price": it.unit_price,
            "party_name": party.name if party else None, "user_name": format_user_display(user, uname),
        })
    return paged(items, total, pg, size, total_quantity=round(float(total_qty), 3), kind=kind)


@router.get("/{product_id}", response_model=schemas.ProductOut)
def get_product(
    product_id: int,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(auth.get_current_user),
):
    product = db.query(models.Product).options(selectinload(models.Product.locations)).filter(models.Product.id == product_id).first()
    if not product:
        raise HTTPException(status_code=404, detail="المنتج مش موجود")
    return _to_out(product, LocationIndex(db))


@router.post("", response_model=schemas.ProductOut)
def create_product(
    payload: schemas.ProductCreate,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(auth.get_current_user),
):
    sku = payload.sku.strip() if payload.sku else None
    if sku and db.query(models.Product).filter(models.Product.sku == sku).first():
        raise HTTPException(status_code=400, detail="كود المنتج (SKU) موجود بالفعل")
    if payload.category_id and not db.query(models.Category).filter(models.Category.id == payload.category_id).first():
        raise HTTPException(status_code=400, detail="التصنيف المختار مش موجود")
    if payload.brand_id and not db.query(models.Brand).filter(models.Brand.id == payload.brand_id).first():
        raise HTTPException(status_code=400, detail="الماركة المختارة مش موجودة")
    idx = LocationIndex(db)
    loc_rows = validate_location_rows(idx, payload.locations)
    if (payload.quantity or 0) > 0 and not loc_rows:
        raise HTTPException(status_code=400, detail="أي كمية لازم يتحدد لها مكان تخزين. أضف مكان التخزين (أو سيب الكمية صفر وتدخل البضاعة من فاتورة وارد).")
    data = payload.model_dump(exclude={"locations"})
    data["sku"] = sku
    # الكمية الإجمالية = مجموع كميات الأماكن دايمًا (مفيش بضاعة من غير مكان)
    data["quantity"] = _round_qty(sum(q for _, q in loc_rows))
    product = models.Product(**data)
    db.add(product)
    db.flush()
    for loc_id, qty in loc_rows:
        db.add(models.ProductLocation(product_id=product.id, location_id=loc_id, quantity=qty))
    db.commit()
    db.refresh(product)
    return _to_out(product, idx)


@router.put("/{product_id}", response_model=schemas.ProductOut)
def update_product(
    product_id: int,
    payload: schemas.ProductUpdate,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(auth.get_current_user),
):
    product = db.query(models.Product).filter(models.Product.id == product_id).first()
    if not product:
        raise HTTPException(status_code=404, detail="المنتج مش موجود")
    data = payload.model_dump(exclude_unset=True)
    if "category_id" in data and data["category_id"] and not db.query(models.Category).filter(models.Category.id == data["category_id"]).first():
        raise HTTPException(status_code=400, detail="التصنيف المختار مش موجود")
    if "brand_id" in data and data["brand_id"] and not db.query(models.Brand).filter(models.Brand.id == data["brand_id"]).first():
        raise HTTPException(status_code=400, detail="الماركة المختارة مش موجودة")
    for field, value in data.items():
        setattr(product, field, value)
    db.commit()
    db.refresh(product)
    return _to_out(product, LocationIndex(db))


@router.put("/{product_id}/locations", response_model=schemas.ProductOut)
def set_product_locations(
    product_id: int,
    payload: schemas.ProductLocationsSet,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(auth.get_current_user),
):
    """
    بيضبط توزيع كمية المنتج على أماكن التخزين (بيستبدل القايمة كلها)، وإجمالي المنتج = مجموع الأماكن دايمًا.
    لو الإجمالي اتغير بيتسجّل في سجل تعديلات الكمية. (لنقل كمية من مكان لمكان من غير ما الإجمالي يتغير
    استخدم /api/locations/transfer)
    """
    product = lock_products(db, [product_id]).get(product_id)
    if not product:
        raise HTTPException(status_code=404, detail="المنتج مش موجود")
    idx = LocationIndex(db)
    wanted = dict(validate_location_rows(idx, payload.locations))

    existing = {pl.location_id: pl for pl in product.locations}
    new_total = _round_qty(sum(wanted.values()))

    for loc_id, pl in existing.items():
        if loc_id in wanted:
            pl.quantity = wanted[loc_id]
        else:
            db.delete(pl)
    for loc_id, qty in wanted.items():
        if loc_id not in existing:
            db.add(models.ProductLocation(product_id=product.id, location_id=loc_id, quantity=qty))

    old_total = _round_qty(product.quantity)
    if new_total != old_total:
        db.add(models.QuantityAdjustment(
            product_id=product.id, old_quantity=old_total, new_quantity=new_total,
            difference=_round_qty(new_total - old_total),
            changed_by_id=current_user.id, changed_by_username=current_user.username,
            reason=payload.reason or "تعديل توزيع الكمية على أماكن التخزين", date=now_cairo(),
        ))
        product.quantity = new_total
    db.commit()
    db.refresh(product)
    return _to_out(product, idx)


@router.post("/{product_id}/adjust-quantity", response_model=schemas.ProductOut)
def adjust_quantity(
    product_id: int,
    payload: schemas.QuantityAdjustmentCreate,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(auth.get_current_user),
):
    """
    تصحيح كمية منتج في مكان معيّن (تصحيح جرد) - بيتسجل في سجل منفصل (quantity_adjustments)
    مش ظاهر في الموقع، بس قابل للتحميل Excel من التقارير. إجمالي المنتج بيتحسب من مجموع الأماكن.
    """
    product = lock_products(db, [product_id]).get(product_id)
    if not product:
        raise HTTPException(status_code=404, detail="المنتج مش موجود")
    idx = LocationIndex(db)
    require_leaf_location(idx, payload.location_id)
    new_at_loc = _round_qty(payload.new_quantity)

    pl = next((x for x in product.locations if x.location_id == payload.location_id), None)
    old_at_loc = pl.quantity if pl else 0.0
    old_total = _round_qty(product.quantity)
    new_total = _round_qty(old_total - old_at_loc + new_at_loc)

    if pl is None:
        if new_at_loc > 0:
            db.add(models.ProductLocation(product_id=product.id, location_id=payload.location_id, quantity=new_at_loc))
    elif new_at_loc > 0:
        pl.quantity = new_at_loc
    else:
        db.delete(pl)

    reason = f"[{idx.full_code(payload.location_id)}] " + (payload.reason or "")
    db.add(models.QuantityAdjustment(
        product_id=product.id, old_quantity=old_total, new_quantity=new_total,
        difference=_round_qty(new_total - old_total),
        changed_by_id=current_user.id, changed_by_username=current_user.username,
        reason=reason.strip(), date=now_cairo(),
    ))
    product.quantity = new_total
    db.commit()
    db.refresh(product)
    return _to_out(product, idx)


@router.patch("/{product_id}/active", response_model=schemas.ProductOut)
def set_product_active(
    product_id: int,
    payload: schemas.ProductActiveSet,
    db: Session = Depends(get_db),
    admin: models.User = Depends(auth.require_admin),
):
    """
    بديل الحذف: تفعيل / تعطيل المنتج (للأدمن). المنتج المعطّل بيختفي من قايمة المنتجات الافتراضية
    ومن اختيار الأصناف في الوارد/الصادر/النقل، وكل سجلّه (فواتير، حركات، أماكن) بيفضل محفوظ.
    """
    product = db.query(models.Product).filter(models.Product.id == product_id).first()
    if not product:
        raise HTTPException(status_code=404, detail="المنتج مش موجود")
    product.is_active = payload.is_active
    db.commit()
    db.refresh(product)
    return _to_out(product, LocationIndex(db))

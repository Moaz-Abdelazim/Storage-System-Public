import math
from collections import defaultdict
from datetime import datetime, timedelta
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import distinct, func, select
from sqlalchemy.orm import Session

from .. import models, schemas, auth, permissions
from ..database import get_db
from ..location_utils import (
    LocationIndex, normalize_code, require_leaf_location, query_transfers, transfer_to_dict, lock_products,
)
from ..timezone_utils import now_cairo
from ..money import q3
from ..services import inventory
from ..query_helpers import contains, page_args, paged

router = APIRouter(prefix="/api/locations", tags=["Locations"])


def _round_qty(v: float) -> float:
    return round(v, 3)


def _clean_name(value: Optional[str], label: str) -> str:
    name = (value or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail=f"اكتب {label}")
    if len(name) > 100:
        raise HTTPException(status_code=400, detail=f"{label} طويل جدًا")
    return name


def _location_stock(db: Session, location_id: int) -> float:
    return db.query(func.coalesce(func.sum(models.ProductLocation.quantity), 0.0)).filter(
        models.ProductLocation.location_id == location_id, models.ProductLocation.quantity > 0
    ).scalar() or 0.0


def _location_has_history(db: Session, location_id: int) -> bool:
    L = models.LocationTransfer
    if db.query(L.id).filter((L.from_location_id == location_id) | (L.to_location_id == location_id)).first():
        return True
    if db.query(models.StockInItem.id).filter(models.StockInItem.location_id == location_id).first():
        return True
    return db.query(models.StockOutItem.id).filter(models.StockOutItem.location_id == location_id).first() is not None


def _page(rows: list, page: int, page_size: int, default: int = 10, max_size: int = 100) -> dict:
    page = max(1, page)
    page_size = default if not page_size or page_size <= 0 else min(page_size, max_size)
    start = (page - 1) * page_size
    total = len(rows)
    return {
        "items": rows[start:start + page_size], "total": total, "page": page, "page_size": page_size,
        "total_pages": max(1, math.ceil(total / page_size)) if total else 1,
    }


# =====================================================================
#  Warehouses
# =====================================================================
@router.get("/warehouses")
def list_warehouses(db: Session = Depends(get_db), current_user: models.User = Depends(auth.require_any_permission("locations.view", "stock_in.create", "stock_out.create", "transfers.create", "transfers.view", "products.create", "products.adjust", "stocktake.manage"))):
    idx = LocationIndex(db)
    return [idx.warehouse_dict(w) for w in idx.warehouses_sorted()]


@router.post("/warehouses")
def create_warehouse(
    payload: schemas.WarehouseCreate,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(auth.require_permission("locations.manage")),
):
    name = _clean_name(payload.name, "اسم المخزن")
    code = normalize_code(payload.code, "كود المخزن")
    if db.query(models.Warehouse).filter(models.Warehouse.code == code).first():
        raise HTTPException(status_code=400, detail="كود المخزن ده مستخدم بالفعل")
    wh = models.Warehouse(name=name, code=code)
    db.add(wh)
    db.commit()
    db.refresh(wh)
    return {"id": wh.id, "code": wh.code, "name": wh.name, "default_location_id": None, "default_location_code": None}


@router.put("/warehouses/{warehouse_id}/default-location")
def set_default_location(
    warehouse_id: int,
    payload: schemas.DefaultLocationSet,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(auth.require_permission("locations.manage")),
):
    """تحديد (أو إلغاء) مكان الاستلام الافتراضي للمخزن. اختياري - مخزن من غير مكان افتراضي بيطلب مكان محدد وقت الاستلام."""
    wh = db.query(models.Warehouse).filter(models.Warehouse.id == warehouse_id).first()
    if not wh:
        raise HTTPException(status_code=404, detail="المخزن مش موجود")
    if payload.location_id is None:
        wh.default_location_id = None
    else:
        idx = LocationIndex(db)
        loc = idx.locations.get(payload.location_id)
        if loc is None or loc.warehouse_id != warehouse_id:
            raise HTTPException(status_code=400, detail="المكان ده مش تابع للمخزن المختار")
        require_leaf_location(idx, loc.id)  # لازم يكون مكان نهائي (مالوش مواقع تحته)
        wh.default_location_id = loc.id
    db.commit()
    return LocationIndex(db).warehouse_dict(wh)


@router.put("/warehouses/{warehouse_id}")
def update_warehouse(
    warehouse_id: int,
    payload: schemas.WarehouseUpdate,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(auth.require_permission("locations.manage")),
):
    wh = db.query(models.Warehouse).filter(models.Warehouse.id == warehouse_id).first()
    if not wh:
        raise HTTPException(status_code=404, detail="المخزن مش موجود")
    if payload.name is not None:
        wh.name = _clean_name(payload.name, "اسم المخزن")
    if payload.code is not None:
        code = normalize_code(payload.code, "كود المخزن")
        if db.query(models.Warehouse).filter(models.Warehouse.code == code, models.Warehouse.id != warehouse_id).first():
            raise HTTPException(status_code=400, detail="كود المخزن ده مستخدم بالفعل")
        wh.code = code
    db.commit()
    return LocationIndex(db).warehouse_dict(wh)


@router.delete("/warehouses/{warehouse_id}")
def delete_warehouse(
    warehouse_id: int,
    db: Session = Depends(get_db),
    admin: models.User = Depends(auth.require_permission("locations.delete")),
):
    wh = db.query(models.Warehouse).filter(models.Warehouse.id == warehouse_id).first()
    if not wh:
        raise HTTPException(status_code=404, detail="المخزن مش موجود")
    if db.query(models.Location.id).filter(models.Location.warehouse_id == warehouse_id).first():
        raise HTTPException(status_code=400, detail="المخزن ده فيه مواقع - احذف المواقع اللي جواه الأول (ولازم تكون فاضية ومن غير حركات مسجلة)")
    db.delete(wh)
    db.commit()
    return {"message": "تم حذف المخزن"}


# =====================================================================
#  Tree + children (للـ Tree View وللقوائم المتدرجة)
# =====================================================================
@router.get("/tree")
def tree(db: Session = Depends(get_db), current_user: models.User = Depends(auth.require_any_permission("locations.view", "stock_in.create", "stock_out.create", "transfers.create", "transfers.view", "products.create", "products.adjust", "stocktake.manage"))):
    idx = LocationIndex(db)
    default_ids = idx.default_location_ids()
    qty_by_loc = defaultdict(float)
    prods_by_loc = defaultdict(set)
    for loc_id, prod_id, qty in db.query(
        models.ProductLocation.location_id, models.ProductLocation.product_id, models.ProductLocation.quantity
    ).filter(models.ProductLocation.quantity > 0).all():
        qty_by_loc[loc_id] += qty
        prods_by_loc[loc_id].add(prod_id)

    def build(loc):
        kids = [build(c) for c in idx.children_of(loc.warehouse_id, loc.id)]
        products = set(prods_by_loc.get(loc.id, set()))
        qty = qty_by_loc.get(loc.id, 0.0)
        for k in kids:
            qty += k["quantity"]
            products |= k["_products"]
        return {
            "kind": "location", "id": loc.id, "warehouse_id": loc.warehouse_id, "parent_id": loc.parent_id,
            "code": loc.code, "name": loc.name, "type": loc.type, "full_code": idx.full_code(loc.id),
            "is_leaf": not kids, "quantity": _round_qty(qty), "products_count": len(products),
            "is_default_receiving": loc.id in default_ids,
            "children": kids, "_products": products,
        }

    def strip(node):
        node.pop("_products", None)
        for c in node["children"]:
            strip(c)

    result = []
    for wh in idx.warehouses_sorted():
        kids = [build(l) for l in idx.children_of(wh.id, None)]
        products = set()
        qty = 0.0
        for k in kids:
            qty += k["quantity"]
            products |= k["_products"]
            strip(k)
        result.append({
            "kind": "warehouse", "id": wh.id, "warehouse_id": wh.id, "parent_id": None, "code": wh.code,
            "name": wh.name, "type": None, "full_code": wh.code, "is_leaf": not kids,
            "quantity": _round_qty(qty), "products_count": len(products), "children": kids,
            "default_location_id": idx.warehouse_dict(wh)["default_location_id"],
            "default_location_code": idx.warehouse_dict(wh)["default_location_code"],
        })
    return result


@router.get("/children")
def children(
    warehouse_id: Optional[int] = None,
    parent_id: Optional[int] = None,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(auth.require_any_permission("locations.view", "stock_in.create", "stock_out.create", "transfers.create", "transfers.view", "products.create", "products.adjust", "stocktake.manage")),
):
    idx = LocationIndex(db)
    if parent_id:
        parent = idx.locations.get(parent_id)
        if not parent:
            raise HTTPException(status_code=404, detail="الموقع مش موجود")
        items = idx.children_of(parent.warehouse_id, parent.id)
    elif warehouse_id:
        if warehouse_id not in idx.warehouses:
            raise HTTPException(status_code=404, detail="المخزن مش موجود")
        items = idx.children_of(warehouse_id, None)
    else:
        raise HTTPException(status_code=400, detail="لازم تحدد المخزن أو الموقع الأب")
    return [
        {"id": l.id, "code": l.code, "name": l.name, "type": l.type, "full_code": idx.full_code(l.id),
         "has_children": bool(idx.children_of(l.warehouse_id, l.id)),
         "is_default_receiving": l.id in idx.default_location_ids()}
        for l in items
    ]


@router.get("/index")
def location_index(db: Session = Depends(get_db), current_user: models.User = Depends(auth.require_any_permission("locations.view", "stock_in.create", "stock_out.create", "transfers.create", "transfers.view", "products.create", "products.adjust", "stocktake.manage"))):
    """
    خريطة مسطّحة {الكود الكامل: [المستويات بأسمائها]} - الواجهة بتستخدمها للـ pop-up اللي بيظهر
    لما الماوس يقف على أي كود مكان (من غير طلب لكل كود).
    """
    idx = LocationIndex(db)
    out = {}
    for wh in idx.warehouses_sorted():
        out[wh.code] = [{"code": wh.code, "name": wh.name, "type": "مخزن"}]
    for loc_id in idx.locations:
        out[idx.full_code(loc_id)] = [
            {"code": p["code"], "name": p["name"], "type": p["type"] if p["kind"] == "location" else "مخزن"}
            for p in idx.path(loc_id)
        ]
    return out


# =====================================================================
#  Locations CRUD
# =====================================================================
def _sibling_exists(db: Session, warehouse_id: int, parent_id: Optional[int], code: str, exclude_id: Optional[int] = None) -> bool:
    q = db.query(models.Location.id).filter(models.Location.warehouse_id == warehouse_id, models.Location.code == code)
    q = q.filter(models.Location.parent_id.is_(None)) if parent_id is None else q.filter(models.Location.parent_id == parent_id)
    if exclude_id:
        q = q.filter(models.Location.id != exclude_id)
    return q.first() is not None


@router.post("")
def create_location(
    payload: schemas.LocationCreate,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(auth.require_permission("locations.manage")),
):
    name = _clean_name(payload.name, "اسم الموقع")
    code = normalize_code(payload.code, "كود الموقع")
    loc_type = (payload.type or "").strip() or None

    if payload.parent_id:
        parent = db.query(models.Location).filter(models.Location.id == payload.parent_id).first()
        if not parent:
            raise HTTPException(status_code=404, detail="الموقع الأب مش موجود")
        warehouse_id = parent.warehouse_id
        if payload.warehouse_id and payload.warehouse_id != warehouse_id:
            raise HTTPException(status_code=400, detail="المخزن المحدد مش هو مخزن الموقع الأب")
        if parent.id in LocationIndex(db).default_location_ids():
            raise HTTPException(
                status_code=400,
                detail="ده مكان الاستلام الافتراضي للمخزن، ومينفعش يتقسم لمواقع فرعية. غيّر مكان الاستلام الافتراضي الأول.",
            )
        # الموقع اللي فيه بضاعة ما ينفعش يتقسم - البضاعة بتتخزن في المواقع النهائية بس
        if _location_stock(db, parent.id) > 0:
            raise HTTPException(
                status_code=400,
                detail=f"الموقع ({LocationIndex(db).full_code(parent.id)}) فيه بضاعة. انقل البضاعة لموقع تاني الأول قبل ما تضيف مواقع فرعية تحته",
            )
        db.query(models.ProductLocation).filter(
            models.ProductLocation.location_id == parent.id, models.ProductLocation.quantity <= 0
        ).delete()
    else:
        if not payload.warehouse_id or not db.query(models.Warehouse).filter(models.Warehouse.id == payload.warehouse_id).first():
            raise HTTPException(status_code=400, detail="اختار المخزن أو الموقع الأب")
        warehouse_id = payload.warehouse_id

    if _sibling_exists(db, warehouse_id, payload.parent_id, code):
        raise HTTPException(status_code=400, detail="الكود ده مستخدم بالفعل في نفس المستوى")

    loc = models.Location(warehouse_id=warehouse_id, parent_id=payload.parent_id, name=name, code=code, type=loc_type)
    db.add(loc)
    db.commit()
    db.refresh(loc)
    return {"id": loc.id, "full_code": LocationIndex(db).full_code(loc.id)}


@router.put("/{location_id}")
def update_location(
    location_id: int,
    payload: schemas.LocationUpdate,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(auth.require_permission("locations.manage")),
):
    loc = db.query(models.Location).filter(models.Location.id == location_id).first()
    if not loc:
        raise HTTPException(status_code=404, detail="الموقع مش موجود")
    data = payload.model_dump(exclude_unset=True)
    if "name" in data:
        loc.name = _clean_name(data["name"], "اسم الموقع")
    if "type" in data:
        loc.type = (data["type"] or "").strip() or None
    if "code" in data:
        code = normalize_code(data["code"], "كود الموقع")
        if _sibling_exists(db, loc.warehouse_id, loc.parent_id, code, exclude_id=loc.id):
            raise HTTPException(status_code=400, detail="الكود ده مستخدم بالفعل في نفس المستوى")
        loc.code = code
    db.commit()
    return {"id": loc.id, "full_code": LocationIndex(db).full_code(loc.id)}


@router.delete("/{location_id}")
def delete_location(
    location_id: int,
    db: Session = Depends(get_db),
    admin: models.User = Depends(auth.require_permission("locations.delete")),
):
    idx = LocationIndex(db)
    loc = idx.locations.get(location_id)
    if not loc:
        raise HTTPException(status_code=404, detail="الموقع مش موجود")
    code = idx.full_code(location_id)
    if idx.children_of(loc.warehouse_id, loc.id):
        raise HTTPException(status_code=400, detail=f"({code}) له مواقع فرعية - احذفها الأول")
    if location_id in idx.default_location_ids():
        raise HTTPException(status_code=400, detail=f"({code}) هو مكان الاستلام الافتراضي للمخزن - غيّر المكان الافتراضي الأول")
    if _location_stock(db, location_id) > 0:
        raise HTTPException(status_code=400, detail=f"({code}) فيه بضاعة - انقلها لموقع تاني الأول")
    if _location_has_history(db, location_id):
        raise HTTPException(
            status_code=400,
            detail=f"({code}) اتسجل عليه حركات (نقل أو وارد/صادر) ومينفعش يتحذف عشان محفوظ في سجل الحركات. تقدر تعدّل اسمه بدل الحذف.",
        )
    db.query(models.ProductLocation).filter(models.ProductLocation.location_id == location_id).delete()
    db.delete(loc)
    db.commit()
    return {"message": "تم حذف الموقع"}


# =====================================================================
#  البحث عن Location بالكود الكامل
# =====================================================================
@router.get("/resolve")
def resolve_location(
    code: str,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(auth.require_any_permission("locations.view", "stock_in.create", "stock_out.create", "transfers.create", "transfers.view", "products.create", "products.adjust", "stocktake.manage")),
):
    """
    البحث عن مخزن/موقع بالكود الكامل وعرض البضاعة اللي فيه (وفي كل اللي تحته).
    الإجماليات (total_quantity / products_count) بـ SUM و COUNT(DISTINCT) في SQL، وقايمة المنتجات
    مرتّبة بالاسم و LIMIT 200 في الاستعلام نفسه - من غير تحميل كل صفوف النطاق في الذاكرة.
    """
    idx = LocationIndex(db)
    res = idx.resolve(code)
    if res["error"]:
        raise HTTPException(status_code=404, detail=res["error"])
    wh, loc = res["warehouse"], res["location"]
    PL, P = models.ProductLocation, models.Product
    if loc is None:  # مخزن كامل: بنفلتر بالمخزن في SQL (مش قايمة أرقام كبيرة)
        scope_cond = PL.location_id.in_(select(models.Location.id).where(models.Location.warehouse_id == wh.id))
    else:
        scope_cond = PL.location_id.in_(idx.scope_ids(wh.id, loc.id))
    base = db.query(PL).join(P, P.id == PL.product_id).filter(scope_cond, PL.quantity > 0)
    total_qty, products_count = base.with_entities(
        func.coalesce(func.sum(PL.quantity), 0), func.count(distinct(PL.product_id))
    ).one()
    # الاسم أولًا (زي الأول)، وبعده id لتثبيت الترتيب لو في أسماء متكررة
    rows = base.with_entities(PL, P).order_by(P.name, P.id, PL.id).limit(200).all()
    return {
        "full_code": idx.full_code(loc.id) if loc else wh.code,
        "warehouse_id": wh.id,
        "location_id": loc.id if loc else None,
        "is_leaf": bool(loc and idx.is_leaf(loc.id)),
        "path": idx.path(loc.id if loc else None, wh.id),
        "total_quantity": _round_qty(float(total_qty or 0)),
        "products_count": int(products_count or 0),
        "products": [
            {"product_id": p.id, "name": p.name, "sku": p.sku, "unit": p.unit, "quantity": pl.quantity,
             "location_code": idx.full_code(pl.location_id)}
            for pl, p in rows
        ],
    }


# =====================================================================
#  نقل منتج بين مكانين
# =====================================================================
@router.post("/transfer")
def transfer_product(
    payload: schemas.TransferCreate,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(auth.require_permission("transfers.create")),
):
    qty = q3(payload.quantity)
    if qty <= 0:
        raise HTTPException(status_code=400, detail="الكمية لازم تكون أكبر من صفر")
    if payload.from_location_id == payload.to_location_id:
        raise HTTPException(status_code=400, detail="مكان النقل منه ومكان النقل إليه لازم يكونوا مختلفين")
    product = lock_products(db, [payload.product_id]).get(payload.product_id)
    if not product:
        raise HTTPException(status_code=404, detail="المنتج مش موجود")
    if not product.is_active:
        raise HTTPException(status_code=400, detail=f"المنتج ({product.name}) غير مفعّل - فعّله الأول من صفحة المنتجات")

    idx = LocationIndex(db)
    from_loc = idx.locations.get(payload.from_location_id)
    if not from_loc:
        raise HTTPException(status_code=404, detail="الموقع المنقول منه مش موجود")
    require_leaf_location(idx, payload.to_location_id)

    # الخصم من المصدر بشرط الكفاية (UPDATE ذرّي) والإضافة للوجهة (upsert ذرّي). الإجمالي ما بيتغيرش.
    # لو الرصيد مش كفاية services.inventory بيرمي InsufficientStock (400 برسالة عربي من main.py).
    def _tx():
        inventory.transfer_stock(db, product.id, from_loc.id, payload.to_location_id, qty)
        t = models.LocationTransfer(
            product_id=product.id,
            from_location_id=from_loc.id,
            to_location_id=payload.to_location_id,
            quantity=float(qty),
            moved_by_id=current_user.id,
            moved_by_username=current_user.username,
            notes=(payload.notes or "").strip() or None,
            date=now_cairo(),
        )
        db.add(t)
        db.commit()
        db.refresh(t)
        return t

    t = inventory.with_retry(db, _tx)
    return transfer_to_dict(t, idx)


@router.get("/putaway")
def putaway_list(
    page: int = 1,
    page_size: int = 10,
    search: Optional[str] = None,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(auth.require_any_permission("transfers.create", "transfers.view", "locations.view")),
):
    """
    الأصناف اللي لسه في أماكن الاستلام الافتراضية (مستنية الرص) - بترقيم من قاعدة البيانات.
    + عدد (وأول صفحة من) المنتجات اللي كميتها الإجمالية أكبر من الموزّع على أماكن: بيانات قديمة لازم تتوزّع.
    """
    PL, P = models.ProductLocation, models.Product
    pg, size = page_args(page, page_size, default=10)
    idx = LocationIndex(db)
    default_ids = idx.default_location_ids()
    pending, pending_total = [], 0
    if default_ids:
        q = (
            db.query(PL, P).join(P, P.id == PL.product_id)
            .filter(PL.location_id.in_(default_ids), PL.quantity > 0, P.is_active.is_(True))
        )
        if search and search.strip():
            q = q.filter(contains(P.name, search) | contains(P.sku, search))
        pending_total = q.count()
        rows = q.order_by(P.name, PL.id).offset((pg - 1) * size).limit(size).all()
        pending = [
            {"product_id": p.id, "name": p.name, "sku": p.sku, "unit": p.unit, "location_id": pl.location_id,
             "location_code": idx.full_code(pl.location_id), "quantity": pl.quantity}
            for pl, p in rows
        ]

    assigned_sq = db.query(PL.product_id.label("pid"), func.sum(PL.quantity).label("a")).group_by(PL.product_id).subquery()
    gap = P.quantity - func.coalesce(assigned_sq.c.a, 0)
    uq = (
        db.query(P, gap.label("gap")).outerjoin(assigned_sq, assigned_sq.c.pid == P.id)
        .filter(P.quantity > 0, gap > 0.0005)
    )
    unallocated_total = uq.count()
    unallocated = [
        {"product_id": p.id, "name": p.name, "sku": p.sku, "unit": p.unit, "quantity": _round_qty(g)}
        for p, g in uq.order_by(gap.desc(), P.id).limit(10).all()
    ]
    return paged(pending, pending_total, pg, size, pending=pending, unallocated=unallocated, unallocated_total=unallocated_total)


def _end_exclusive(date_to: Optional[str]) -> Optional[datetime]:
    if not date_to:
        return None
    end = datetime.fromisoformat(date_to)
    return end + timedelta(days=1) if len(date_to) <= 10 else end


@router.get("/transfers")
def list_transfers(
    product_id: Optional[int] = None,
    search: Optional[str] = None,
    warehouse_id: Optional[int] = None,
    location_id: Optional[int] = None,
    user: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    page: int = 1,
    page_size: int = 10,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(auth.require_permission("transfers.view")),
):
    """تقرير حركات النقل (من أو إلى أي مكان جوه الموقع/المخزن المختار، وبالترتيب الأحدث أولاً)."""
    idx = LocationIndex(db)
    rows = query_transfers(
        db, idx, product_id=product_id, search=search, scope_ids=idx.scope_ids(warehouse_id, location_id),
        user=user, date_from=datetime.fromisoformat(date_from) if date_from else None,
        date_to_exclusive=_end_exclusive(date_to),
    )
    result = _page(rows, page, page_size)
    result["totals"] = {"count": len(rows), "quantity": _round_qty(sum(r["quantity"] for r in rows))}
    return result


# =====================================================================
#  تقرير المخزون حسب المكان
# =====================================================================
@router.get("/reports/stock")
def stock_by_location(
    warehouse_id: Optional[int] = None,
    location_id: Optional[int] = None,
    search: Optional[str] = None,
    page: int = 1,
    page_size: int = 10,
    breakdown_page: int = 1,
    breakdown_page_size: int = 8,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(auth.require_any_permission("locations.view", "stock_in.create", "stock_out.create", "transfers.create", "transfers.view", "products.create", "products.adjust", "stocktake.manage")),
):
    """
    إجمالي الكمية (والقيمة لو أدمن) جوه مخزن أو أي موقع (بكل اللي تحته)، مع تفصيل حسب المستوى اللي تحته مباشرة،
    وقايمة المنتجات الموجودة فيه. من غير فلتر: كل المخازن.
    الأداء: كله بـ SQL مجمّع - المنتجات والتفصيل (breakdown) كلاهما بترقيم، ومفيش تحميل لكل صفوف المخزون في الذاكرة.
    """
    PL, P = models.ProductLocation, models.Product
    idx = LocationIndex(db)
    scope = idx.scope_ids(warehouse_id, location_id)
    show_value = permissions.has_permission(current_user, "finance.view")  # قيمة المخزون للأدمن بس (زي باقي النظام)
    pg, size = page_args(page, page_size, default=10, max_value=100)
    bpg, bsize = page_args(breakdown_page, breakdown_page_size, default=8, max_value=50)

    def scoped(q):
        q = q.filter(PL.quantity > 0)
        return q.filter(PL.location_id.in_(scope)) if scope is not None else q

    totals_row = scoped(
        db.query(func.coalesce(func.sum(PL.quantity), 0), func.coalesce(func.sum(PL.quantity * P.price), 0),
                 func.count(func.distinct(PL.product_id))).select_from(PL).join(P, P.id == PL.product_id)
    ).first()

    # ---- التفصيل: تجميع بالموقع في SQL (صف لكل موقع فيه رصيد) ثم دمجه بالمستوى المطلوب ----
    selected_depth = len(idx.chain(location_id)) - 1 if location_id else None

    def bucket(loc_id: int):
        chain = idx.chain(loc_id)
        if scope is None:
            wh = idx.warehouses[chain[0].warehouse_id]
            return wh.code, f"{wh.code} — {wh.name}"
        node = chain[0] if selected_depth is None else (chain[selected_depth + 1] if len(chain) > selected_depth + 1 else chain[selected_depth])
        return idx.full_code(node.id), f"{idx.full_code(node.id)} — {node.name}"

    per_loc = scoped(
        db.query(PL.location_id, func.sum(PL.quantity), func.sum(PL.quantity * P.price)).select_from(PL).join(P, P.id == PL.product_id)
    ).group_by(PL.location_id).all()
    buckets = {}
    for loc_id, qty, val in per_loc:
        key, label = bucket(loc_id)
        b = buckets.setdefault(key, {"key": key, "label": label, "quantity": 0.0, "value": 0.0, "loc_ids": []})
        b["quantity"] += float(qty or 0)
        b["value"] += float(val or 0)
        b["loc_ids"].append(loc_id)
    ordered = sorted(buckets.values(), key=lambda b: -b["quantity"])
    page_buckets = ordered[(bpg - 1) * bsize: bpg * bsize]
    breakdown = []
    for b in page_buckets:
        distinct_products = db.query(func.count(func.distinct(PL.product_id))).filter(PL.quantity > 0, PL.location_id.in_(b["loc_ids"])).scalar() or 0
        breakdown.append({"key": b["key"], "label": b["label"], "quantity": _round_qty(b["quantity"]),
                          "value": round(b["value"], 2) if show_value else None, "products_count": distinct_products})

    # ---- المنتجات: تجميع بالصنف + ترقيم في SQL ----
    pq = scoped(
        db.query(P.id, P.name, P.sku, P.unit, func.sum(PL.quantity), func.sum(PL.quantity * P.price)).select_from(PL).join(P, P.id == PL.product_id)
    )
    if search and search.strip():
        pq = pq.filter(contains(P.name, search) | contains(P.sku, search))
    pq = pq.group_by(P.id, P.name, P.sku, P.unit)
    total = db.query(func.count()).select_from(pq.subquery()).scalar() or 0
    rows = pq.order_by(P.name, P.id).offset((pg - 1) * size).limit(size).all()
    pids = [r[0] for r in rows]
    locs_of = defaultdict(list)
    if pids:
        for pid, lid, qty in scoped(db.query(PL.product_id, PL.location_id, PL.quantity).filter(PL.product_id.in_(pids))).all():
            locs_of[pid].append({"full_code": idx.full_code(lid), "quantity": _round_qty(qty)})
    items = []
    for pid, name, sku, unit, qty, val in rows:
        ls = sorted(locs_of.get(pid, []), key=lambda x: x["full_code"])
        items.append({"product_id": pid, "name": name, "sku": sku, "unit": unit, "quantity": _round_qty(qty),
                      "value": round(float(val or 0), 2) if show_value else None,
                      "locations": [f"{l['full_code']}: {l['quantity']}" for l in ls], "location_items": ls})

    if location_id:
        label = idx.full_code(location_id)
    elif warehouse_id:
        label = idx.warehouses[warehouse_id].code
    else:
        label = "كل المخازن"
    return paged(items, total, pg, size,
                 scope_label=label,
                 totals={"quantity": _round_qty(totals_row[0]), "value": round(float(totals_row[1] or 0), 2) if show_value else None,
                         "products_count": totals_row[2] or 0},
                 breakdown=breakdown, breakdown_total=len(ordered), breakdown_page=bpg, breakdown_page_size=bsize,
                 breakdown_total_pages=max(1, math.ceil(len(ordered) / bsize)) if ordered else 1)
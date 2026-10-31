from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func
from sqlalchemy.orm import Session
from typing import List

from .. import models, schemas, auth
from ..database import get_db
from ..query_helpers import contains, page_args, paged
from typing import Optional

router = APIRouter(prefix="/api/brands", tags=["Brands"])


@router.get("", response_model=List[schemas.BrandOut])
def list_brands(db: Session = Depends(get_db), current_user: models.User = Depends(auth.get_current_user)):
    return db.query(models.Brand).order_by(models.Brand.name).all()


@router.get("/stats")
def brand_stats(
    search: Optional[str] = None,
    page: Optional[int] = None,  # من غير page = القايمة كلها
    page_size: int = 15,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(auth.require_any_permission("products.view", "catalog.manage")),
):
    """مع عدد المنتجات المفعّلة وإجمالي الكمية لكل واحد. مع page: ترقيم وبحث من قاعدة البيانات."""
    M, P = models.Brand, models.Product
    q = (
        db.query(M.id, M.name, M.created_at, func.count(P.id), func.coalesce(func.sum(P.quantity), 0))
        .outerjoin(P, (P.brand_id == M.id) & (P.is_active.is_(True)))
    )
    if search and search.strip():
        q = q.filter(contains(M.name, search))
    q = q.group_by(M.id, M.name, M.created_at).order_by(M.name)
    to_row = lambda r: {"id": r[0], "name": r[1], "created_at": r[2], "products_count": r[3], "total_quantity": round(float(r[4] or 0), 3)}
    if page is None:
        return [to_row(r) for r in q.all()]
    pg, size = page_args(page, page_size)
    total = db.query(func.count(M.id)).scalar() if not (search and search.strip()) else db.query(func.count(M.id)).filter(contains(M.name, search)).scalar()
    classified = db.query(func.count(P.id)).filter(P.is_active.is_(True), P.brand_id.isnot(None)).scalar() or 0
    rows = q.offset((pg - 1) * size).limit(size).all()
    return paged([to_row(r) for r in rows], total or 0, pg, size, classified_products=classified)


@router.post("", response_model=schemas.BrandOut)
def create_brand(
    payload: schemas.BrandCreate,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(auth.require_permission("catalog.manage")),
):
    name = payload.name.strip()
    if not name:
        raise HTTPException(status_code=400, detail="اكتب اسم الماركة")
    if db.query(models.Brand).filter(models.Brand.name == name).first():
        raise HTTPException(status_code=400, detail="الماركة دي موجودة بالفعل")
    brand = models.Brand(name=name)
    db.add(brand)
    db.commit()
    db.refresh(brand)
    return brand


@router.put("/{brand_id}", response_model=schemas.BrandOut)
def update_brand(
    brand_id: int,
    payload: schemas.BrandUpdate,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(auth.require_permission("catalog.manage")),
):
    brand = db.query(models.Brand).filter(models.Brand.id == brand_id).first()
    if not brand:
        raise HTTPException(status_code=404, detail="الماركة مش موجودة")
    name = payload.name.strip()
    if not name:
        raise HTTPException(status_code=400, detail="اكتب اسم الماركة")
    duplicate = db.query(models.Brand).filter(models.Brand.name == name, models.Brand.id != brand_id).first()
    if duplicate:
        raise HTTPException(status_code=400, detail="في ماركة تانية بنفس الاسم")
    brand.name = name
    db.commit()
    db.refresh(brand)
    return brand


@router.delete("/{brand_id}")
def delete_brand(
    brand_id: int,
    db: Session = Depends(get_db),
    admin: models.User = Depends(auth.require_permission("catalog.delete")),
):
    brand = db.query(models.Brand).filter(models.Brand.id == brand_id).first()
    if not brand:
        raise HTTPException(status_code=404, detail="الماركة مش موجودة")
    db.query(models.Product).filter(models.Product.brand_id == brand_id).update({"brand_id": None})
    db.delete(brand)
    db.commit()
    return {"message": "تم حذف الماركة"}

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func
from sqlalchemy.orm import Session
from typing import List

from .. import models, schemas, auth
from ..database import get_db
from ..query_helpers import contains, page_args, paged
from typing import Optional

router = APIRouter(prefix="/api/categories", tags=["Categories"])


@router.get("", response_model=List[schemas.CategoryOut])
def list_categories(db: Session = Depends(get_db), current_user: models.User = Depends(auth.get_current_user)):
    return db.query(models.Category).order_by(models.Category.name).all()


@router.get("/stats")
def category_stats(
    search: Optional[str] = None,
    page: Optional[int] = None,  # من غير page = القايمة كلها
    page_size: int = 15,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(auth.require_any_permission("products.view", "catalog.manage")),
):
    """مع عدد المنتجات المفعّلة وإجمالي الكمية لكل واحد. مع page: ترقيم وبحث من قاعدة البيانات."""
    M, P = models.Category, models.Product
    q = (
        db.query(M.id, M.name, M.created_at, func.count(P.id), func.coalesce(func.sum(P.quantity), 0))
        .outerjoin(P, (P.category_id == M.id) & (P.is_active.is_(True)))
    )
    if search and search.strip():
        q = q.filter(contains(M.name, search))
    q = q.group_by(M.id, M.name, M.created_at).order_by(M.name)
    to_row = lambda r: {"id": r[0], "name": r[1], "created_at": r[2], "products_count": r[3], "total_quantity": round(float(r[4] or 0), 3)}
    if page is None:
        return [to_row(r) for r in q.all()]
    pg, size = page_args(page, page_size)
    total = db.query(func.count(M.id)).scalar() if not (search and search.strip()) else db.query(func.count(M.id)).filter(contains(M.name, search)).scalar()
    classified = db.query(func.count(P.id)).filter(P.is_active.is_(True), P.category_id.isnot(None)).scalar() or 0
    rows = q.offset((pg - 1) * size).limit(size).all()
    return paged([to_row(r) for r in rows], total or 0, pg, size, classified_products=classified)


@router.post("", response_model=schemas.CategoryOut)
def create_category(
    payload: schemas.CategoryCreate,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(auth.require_permission("catalog.manage")),
):
    name = payload.name.strip()
    if not name:
        raise HTTPException(status_code=400, detail="اكتب اسم التصنيف")
    if db.query(models.Category).filter(models.Category.name == name).first():
        raise HTTPException(status_code=400, detail="التصنيف ده موجود بالفعل")
    category = models.Category(name=name)
    db.add(category)
    db.commit()
    db.refresh(category)
    return category


@router.put("/{category_id}", response_model=schemas.CategoryOut)
def update_category(
    category_id: int,
    payload: schemas.CategoryUpdate,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(auth.require_permission("catalog.manage")),
):
    category = db.query(models.Category).filter(models.Category.id == category_id).first()
    if not category:
        raise HTTPException(status_code=404, detail="التصنيف مش موجود")
    name = payload.name.strip()
    if not name:
        raise HTTPException(status_code=400, detail="اكتب اسم التصنيف")
    duplicate = db.query(models.Category).filter(models.Category.name == name, models.Category.id != category_id).first()
    if duplicate:
        raise HTTPException(status_code=400, detail="في تصنيف تاني بنفس الاسم")
    category.name = name
    db.commit()
    db.refresh(category)
    return category


@router.delete("/{category_id}")
def delete_category(
    category_id: int,
    db: Session = Depends(get_db),
    admin: models.User = Depends(auth.require_permission("catalog.delete")),
):
    category = db.query(models.Category).filter(models.Category.id == category_id).first()
    if not category:
        raise HTTPException(status_code=404, detail="التصنيف مش موجود")
    # فك ربط المنتجات من التصنيف ده بدل ما نمنع الحذف
    db.query(models.Product).filter(models.Product.category_id == category_id).update({"category_id": None})
    db.delete(category)
    db.commit()
    return {"message": "تم حذف التصنيف"}

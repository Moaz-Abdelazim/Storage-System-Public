from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from typing import List, Optional

from .. import models, schemas, auth
from ..database import get_db
from ..query_helpers import contains, page_args, paged

router = APIRouter(prefix="/api/recipients", tags=["Recipients"])


def _aggregates(db: Session, ids):
    """عدد الفواتير وإجمالي الكمية للجهات دي بس (مش لكل الجهات)."""
    if not ids:
        return {}
    rows = (
        db.query(models.StockOutInvoice.recipient_id, func.count(func.distinct(models.StockOutInvoice.id)), func.sum(models.StockOutItem.quantity))
        .join(models.StockOutItem, models.StockOutItem.invoice_id == models.StockOutInvoice.id)
        .filter(models.StockOutInvoice.recipient_id.in_(ids))
        .group_by(models.StockOutInvoice.recipient_id)
        .all()
    )
    return {r[0]: {"invoices_count": r[1], "total_quantity": r[2] or 0} for r in rows}


def _out(m, agg):
    a = agg.get(m.id, {"invoices_count": 0, "total_quantity": 0})
    return schemas.RecipientOut(
        id=m.id, name=m.name, phone=m.phone, address=m.address, notes=m.notes,
        created_at=m.created_at, invoices_count=a["invoices_count"], total_quantity=a["total_quantity"],
    )


@router.get("")
def list_recipients(
    search: Optional[str] = None,
    page: Optional[int] = None,  # من غير page = القايمة كلها (للتوافق)
    page_size: int = 15,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(auth.get_current_user),
):
    """جهات الاستلام - مع page/page_size الترقيم والبحث بيحصلوا في قاعدة البيانات والتجميع بيتحسب للصفحة دي بس."""
    M = models.Recipient
    q = db.query(M)
    if search and search.strip():
        q = q.filter(contains(M.name, search) | contains(M.phone, search) | contains(M.address, search))
    q = q.order_by(M.name, M.id)
    if page is None:
        rows = q.all()
        agg = _aggregates(db, [m.id for m in rows])
        return [_out(m, agg) for m in rows]
    pg, size = page_args(page, page_size)
    total = q.count()
    rows = q.offset((pg - 1) * size).limit(size).all()
    agg = _aggregates(db, [m.id for m in rows])
    total_invoices = db.query(func.count(models.StockOutInvoice.id)).filter(models.StockOutInvoice.recipient_id.isnot(None)).scalar() or 0
    return paged([_out(m, agg) for m in rows], total, pg, size, total_invoices=total_invoices)


@router.get("/lookup")
def lookup_recipients(
    q: str = "",
    limit: int = 20,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(auth.get_current_user),
):
    """بحث سريع وخفيف للقوايم المنسدلة (أقصى 50 نتيجة)."""
    M = models.Recipient
    query = db.query(M)
    if q and q.strip():
        query = query.filter(contains(M.name, q) | contains(M.phone, q))
    return [{"id": m.id, "name": m.name, "phone": m.phone} for m in query.order_by(M.name).limit(max(1, min(limit, 50))).all()]


@router.post("", response_model=schemas.RecipientOut)
def create_recipient(
    payload: schemas.RecipientCreate,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(auth.get_current_user),
):
    recipient = models.Recipient(**payload.model_dump())
    db.add(recipient)
    db.commit()
    db.refresh(recipient)
    return schemas.RecipientOut(
        id=recipient.id, name=recipient.name, phone=recipient.phone, address=recipient.address,
        notes=recipient.notes, created_at=recipient.created_at, invoices_count=0, total_quantity=0,
    )


def _aggregate_one(db: Session, entity_id: int):
    row = (
        db.query(func.count(func.distinct(models.StockOutInvoice.id)), func.sum(models.StockOutItem.quantity))
        .join(models.StockOutItem, models.StockOutItem.invoice_id == models.StockOutInvoice.id)
        .filter(models.StockOutInvoice.recipient_id == entity_id)
        .first()
    )
    return (row[0] or 0, row[1] or 0) if row else (0, 0)


@router.put("/{recipient_id}", response_model=schemas.RecipientOut)
def update_recipient(
    recipient_id: int,
    payload: schemas.RecipientCreate,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(auth.get_current_user),
):
    entity = db.query(models.Recipient).filter(models.Recipient.id == recipient_id).first()
    if not entity:
        raise HTTPException(status_code=404, detail="الجهة مش موجودة")
    data = payload.model_dump()
    name = (data.get("name") or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="اكتب الاسم")
    entity.name = name
    for field in ("phone", "address", "notes"):
        if field in data:
            setattr(entity, field, (data[field] or "").strip() or None)
    db.commit()
    db.refresh(entity)
    invoices_count, total_quantity = _aggregate_one(db, entity.id)
    return schemas.RecipientOut(
        id=entity.id, name=entity.name, phone=entity.phone, address=entity.address, notes=entity.notes,
        created_at=entity.created_at, invoices_count=invoices_count, total_quantity=total_quantity,
    )


@router.delete("/{recipient_id}")
def delete_recipient(
    recipient_id: int,
    db: Session = Depends(get_db),
    admin: models.User = Depends(auth.require_admin),
):
    recipient = db.query(models.Recipient).filter(models.Recipient.id == recipient_id).first()
    if not recipient:
        raise HTTPException(status_code=404, detail="الجهة مش موجودة")
    try:
        db.delete(recipient)
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(status_code=400, detail="الجهة دي مرتبطة بفواتير صادر سابقة، مينفعش تتحذف.")
    return {"message": "تم الحذف"}

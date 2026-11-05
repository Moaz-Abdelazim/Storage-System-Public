"""عرض سجل التدقيق (للأدمن بس): بحث بالكيان/الفعل/المستخدم + ترقيم من الـ DB."""
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from .. import auth, models
from ..database import get_db
from ..query_helpers import end_exclusive, page_args, paged, parse_dt

router = APIRouter(prefix="/api/audit-log", tags=["Audit"])


@router.get("")
def list_audit(
    entity_type: Optional[str] = None,
    entity_id: Optional[str] = None,
    action: Optional[str] = None,
    user: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    page: int = 1,
    page_size: int = 25,
    db: Session = Depends(get_db),
    admin: models.User = Depends(auth.require_admin),
):
    A = models.AuditLog
    q = db.query(A)
    if entity_type:
        q = q.filter(A.entity_type == entity_type)
    if entity_id:
        q = q.filter(A.entity_id == str(entity_id))
    if action:
        q = q.filter(A.action == action)
    if user:
        q = q.filter(A.username == user)
    start, end = parse_dt(date_from, "تاريخ البداية"), end_exclusive(date_to)
    if start:
        q = q.filter(A.at >= start)
    if end:
        q = q.filter(A.at < end)
    pg, size = page_args(page, page_size, default=25)
    total = q.count()
    rows = q.order_by(A.at.desc(), A.id.desc()).offset((pg - 1) * size).limit(size).all()
    items = [{"id": r.id, "at": r.at, "user_id": r.user_id, "username": r.username, "ip": r.ip, "entity_type": r.entity_type,
              "entity_id": r.entity_id, "action": r.action, "summary": r.summary} for r in rows]
    return paged(items, total, pg, size)

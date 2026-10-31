"""منطق الأدوار المشترك بين الـ API والـ CLI."""
from sqlalchemy import func
from sqlalchemy.orm import Session

from .. import models

SYSTEM_ROLE_NAME = "مدير النظام"
SYSTEM_ROLE_DESCRIPTION = "كل الصلاحيات (دور أساسي - مينفعش يتعدّل ولا يتمسح)"


def ensure_system_role(db: Session) -> models.Role:
    """بيرجّع دور مدير النظام (is_system) وبينشئه لو مش موجود. بيعمل commit لو أنشأه."""
    role = db.query(models.Role).filter(models.Role.is_system.is_(True)).first()
    if role is None:
        role = models.Role(name=SYSTEM_ROLE_NAME, description=SYSTEM_ROLE_DESCRIPTION, is_system=True, is_active=True)
        db.add(role)
        db.commit()
        db.refresh(role)
    return role


def role_name_exists(db: Session, name: str, exclude_id: int = None) -> bool:
    """فحص الاسم بدون حساسية لحالة الحروف (ومن غير اعتماد على collation الـ DB)."""
    q = db.query(models.Role.id).filter(func.lower(models.Role.name) == name.strip().lower())
    if exclude_id is not None:
        q = q.filter(models.Role.id != exclude_id)
    return q.first() is not None

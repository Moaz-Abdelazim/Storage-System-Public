"""إدارة الأدوار والصلاحيات - للأدمن (دور is_system) فقط. مش صلاحية قابلة للتفويض عشان مفيش تصعيد صلاحيات."""
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func
from sqlalchemy.orm import Session, selectinload

from .. import auth, models, permissions, schemas
from ..database import get_db
from ..services.roles import role_name_exists
from ..timezone_utils import now_cairo

router = APIRouter(prefix="/api/roles", tags=["Roles"])

_SYSTEM_MSG = "دور مدير النظام أساسي ومينفعش يتعدّل أو يتعطّل أو يتمسح"


def _role_out(role: models.Role, users_count: int) -> dict:
    perms = sorted(permissions.ALL_KEYS) if role.is_system else sorted(rp.permission for rp in role.permissions)
    return {
        "id": role.id, "name": role.name, "description": role.description, "is_active": bool(role.is_active),
        "is_system": bool(role.is_system), "users_count": users_count, "permissions": perms,
        "created_at": role.created_at, "updated_at": role.updated_at,
    }


def _users_count(db: Session, role_id: int) -> int:
    return db.query(func.count(models.User.id)).filter(models.User.role_id == role_id).scalar() or 0


def _get_role(db: Session, role_id: int) -> models.Role:
    role = db.query(models.Role).options(selectinload(models.Role.permissions)).filter(models.Role.id == role_id).first()
    if role is None:
        raise HTTPException(status_code=404, detail="الدور مش موجود")
    return role


def _validated(payload: schemas.RoleIn) -> list:
    try:
        return permissions.validate_permission_set(payload.permissions)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.get("/permissions")
def list_permissions(admin: models.User = Depends(auth.require_admin)):
    """كتالوج الصلاحيات مجمّع بالـ group (مع requires لكل صلاحية)."""
    return {"groups": permissions.catalog_grouped()}


@router.get("")
def list_roles(db: Session = Depends(get_db), admin: models.User = Depends(auth.require_admin)):
    counts = dict(db.query(models.User.role_id, func.count(models.User.id)).group_by(models.User.role_id).all())
    roles = db.query(models.Role).options(selectinload(models.Role.permissions)).order_by(models.Role.is_system.desc(), models.Role.name).all()
    return [_role_out(r, counts.get(r.id, 0)) for r in roles]


@router.post("")
def create_role(payload: schemas.RoleIn, db: Session = Depends(get_db), admin: models.User = Depends(auth.require_admin)):
    keys = _validated(payload)
    if role_name_exists(db, payload.name):
        raise HTTPException(status_code=400, detail="في دور بنفس الاسم ده بالفعل")
    role = models.Role(name=payload.name, description=payload.description, is_active=True, is_system=False)
    role.permissions = [models.RolePermission(permission=k) for k in keys]
    db.add(role)
    db.commit()
    db.refresh(role)
    return _role_out(role, 0)


@router.put("/{role_id}")
def update_role(role_id: int, payload: schemas.RoleIn, db: Session = Depends(get_db), admin: models.User = Depends(auth.require_admin)):
    role = _get_role(db, role_id)
    if role.is_system:
        raise HTTPException(status_code=400, detail=_SYSTEM_MSG)
    keys = _validated(payload)
    if role_name_exists(db, payload.name, exclude_id=role.id):
        raise HTTPException(status_code=400, detail="في دور بنفس الاسم ده بالفعل")
    role.name = payload.name
    role.description = payload.description
    role.permissions = [models.RolePermission(permission=k) for k in keys]  # الصلاحيات بتتحمّل كل request فبتسري فورًا
    role.updated_at = now_cairo()
    db.commit()
    db.refresh(role)
    return _role_out(role, _users_count(db, role.id))


@router.patch("/{role_id}/active")
def set_role_active(role_id: int, payload: schemas.RoleActiveSet, db: Session = Depends(get_db), admin: models.User = Depends(auth.require_admin)):
    role = _get_role(db, role_id)
    if role.is_system:
        raise HTTPException(status_code=400, detail=_SYSTEM_MSG)
    role.is_active = payload.is_active
    role.updated_at = now_cairo()
    if not payload.is_active:
        # كل مستخدمي الدور بيفقدوا الدخول، وجلساتهم المفتوحة بتتقفل فورًا
        user_ids = [uid for (uid,) in db.query(models.User.id).filter(models.User.role_id == role.id).all()]
        if user_ids:
            db.query(models.LoginLog).filter(
                models.LoginLog.user_id.in_(user_ids), models.LoginLog.logout_time.is_(None)
            ).update({"logout_time": now_cairo(), "closed_by_id": admin.id}, synchronize_session=False)
    db.commit()
    db.refresh(role)
    return _role_out(role, _users_count(db, role.id))


@router.delete("/{role_id}")
def delete_role(role_id: int, db: Session = Depends(get_db), admin: models.User = Depends(auth.require_admin)):
    role = _get_role(db, role_id)
    if role.is_system:
        raise HTTPException(status_code=400, detail=_SYSTEM_MSG)
    count = _users_count(db, role.id)
    if count:
        raise HTTPException(status_code=409, detail=f"مينفعش تمسح الدور ده: عليه {count} مستخدم. غيّر دورهم الأول.")
    db.delete(role)
    db.commit()
    return {"message": "تم حذف الدور"}

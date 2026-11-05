"""
الإعداد الأول (First-run): متاح **بس** لو مفيش أي مستخدم في النظام. بينشئ: دور مدير النظام + أول أدمن + (اختياري) بيانات الشركة
+ أول مخزن (+ مكان استلام افتراضي اختياري). عملية واحدة atomic، ومحمية من السباق (علامة setup_completed فريدة) ومن التخمين (rate limit لكل IP).
"""
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field, field_validator
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .. import auth, models, schemas
from .. import rate_limit
from ..database import get_db
from ..location_utils import normalize_code
from ..security import client_ip
from ..services import app_settings
from ..services.audit import audit
from ..services.roles import ensure_system_role

router = APIRouter(prefix="/api/setup", tags=["Setup"])
_DONE = "الإعداد الأول اتعمل بالفعل"


class SetupIn(BaseModel):
    username: str
    password: str
    full_name: Optional[str] = Field(default=None, max_length=100)
    company_name: Optional[str] = Field(default=None, max_length=150)
    warehouse_name: str = Field(min_length=1, max_length=100)
    warehouse_code: str = Field(min_length=1, max_length=30)
    create_receiving_location: bool = True  # مكان استلام افتراضي للمخزن (RCV)

    @field_validator("username")
    @classmethod
    def _un(cls, v):
        return schemas._clean_username(v)

    @field_validator("password")
    @classmethod
    def _pw(cls, v):
        return schemas._check_password_strength(v)


@router.get("/status")
def setup_status(db: Session = Depends(get_db)):
    """عام (من غير login): هل النظام محتاج إعداد أول؟ مفيش أي بيانات تانية بتتسرّب."""
    has_users = db.query(models.User.id).first() is not None
    return {"needs_setup": not has_users}


@router.post("", status_code=201)
def run_setup(request: Request, payload: SetupIn, db: Session = Depends(get_db)):
    rate_limit.hit(f"setup:{client_ip(request)}", 10, 15 * 60)
    if db.query(models.User.id).first() is not None:
        raise HTTPException(status_code=409, detail=_DONE)
    wh_name = (payload.warehouse_name or "").strip()
    if not wh_name:
        raise HTTPException(status_code=400, detail="اكتب اسم المخزن")
    wh_code = normalize_code(payload.warehouse_code, "كود المخزن")
    try:
        role = ensure_system_role(db)
    except IntegrityError:
        # طلب إعداد تاني أنشأ دور مدير النظام في نفس اللحظة (unique على الاسم): نقراه بدل ما نرجّع 500
        db.rollback()
        role = ensure_system_role(db)
    try:
        # علامة فريدة: لو طلبين جُم في نفس اللحظة التاني بيتكسر على الـ PK (MySQL بيستنى الأول يخلص) فمفيش أدمنين اتنين
        db.add(models.AppSetting(key=app_settings.SETUP_DONE_KEY, value="1"))
        db.flush()
        if db.query(models.User.id).first() is not None:
            raise HTTPException(status_code=409, detail=_DONE)
        user = models.User(
            username=payload.username, full_name=(payload.full_name or "").strip() or "مدير النظام",
            password_hash=auth.get_password_hash(payload.password), role_id=role.id,
        )
        db.add(user)
        name = (payload.company_name or "").strip()
        if name:
            db.add(models.CompanySettings(id=1, name=name))
        wh = models.Warehouse(name=wh_name, code=wh_code)
        db.add(wh)
        db.flush()
        if payload.create_receiving_location:
            loc = models.Location(warehouse_id=wh.id, parent_id=None, name="مكان الاستلام", code="RCV", type="receiving")
            db.add(loc)
            db.flush()
            wh.default_location_id = loc.id
        audit(db, user, "setup", "first_run", "setup", {"admin": user.username, "warehouse": wh.code})
        db.commit()
    except HTTPException:
        db.rollback()
        raise
    except IntegrityError:
        db.rollback()
        raise HTTPException(status_code=409, detail=_DONE)
    return {"ok": True, "username": payload.username}

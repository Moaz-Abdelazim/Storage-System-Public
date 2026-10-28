"""
بيانات الشركة (الاسم + رقمين تليفون + اللوجو) - بتتخزن في جدول company_settings (صف واحد id=1) بما فيها اللوجو.

الصلاحيات: أي مستخدم مسجّل يقدر يقرأ (محتاجينها للطباعة)، والكتابة (تعديل البيانات/رفع/مسح اللوجو) للأدمن بس.
مفيش أي حاجة هنا متاحة من غير login.

اللوجو: PNG / JPEG / WebP بس (بنتحقق من محتوى الملف الحقيقي مش الامتداد)، حد أقصى 5 ميجا (MAX_LOGO_MB)، والـ SVG مرفوض (خطر XSS).
بيتقدّم من endpoint بـ Content-Type صح و ETag + Cache-Control عشان ما يتحمّلش مع كل فاتورة.
"""
import hashlib
import os
import re
from typing import Optional

from fastapi import APIRouter, Depends, File, HTTPException, Request, Response, UploadFile
from pydantic import BaseModel, Field, field_validator
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .. import models, auth
from ..database import get_db

router = APIRouter(prefix="/api/company", tags=["Company"])

def _logo_limit_mb() -> float:
    try:
        return max(0.1, min(float(os.getenv("MAX_LOGO_MB", "5")), 10))
    except ValueError:
        return 5.0


# الافتراضي 5 ميجا. ملحوظة: Vercel serverless بيرفض أي طلب أكبر من ~4.5 ميجا قبل ما يوصل للكود،
# عشان كده صفحة الإعدادات بتصغّر الصور الكبيرة تلقائيًا قبل الرفع. على VPS الحد ده مش موجود.
MAX_LOGO_BYTES = int(_logo_limit_mb() * 1024 * 1024)
MAX_LOGO_LABEL = f"{_logo_limit_mb():g} ميجا"
_PHONE_RE = re.compile(r"^[0-9+\-()\s]{3,30}$")


def sniff_image_mime(data: bytes) -> Optional[str]:
    """نوع الصورة من محتوى الملف الحقيقي (magic bytes). بيرجّع None لأي حاجة مش PNG/JPEG/WebP (SVG مثلًا)."""
    if data.startswith(b"\x89PNG\r\n\x1a\n") and data[12:16] == b"IHDR":
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if len(data) > 16 and data[:4] == b"RIFF" and data[8:12] == b"WEBP" and data[12:15] == b"VP8":
        return "image/webp"
    return None


class CompanyIn(BaseModel):
    name: Optional[str] = Field(default=None, max_length=150)
    phone1: Optional[str] = Field(default=None, max_length=30)
    phone2: Optional[str] = Field(default=None, max_length=30)

    @field_validator("name", "phone1", "phone2", mode="before")
    @classmethod
    def _strip(cls, v):
        if v is None:
            return None
        v = str(v).strip()
        return v or None

    @field_validator("phone1", "phone2")
    @classmethod
    def _phone(cls, v):
        if v is not None and not _PHONE_RE.match(v):
            raise ValueError("رقم التليفون لازم يتكون من أرقام ومسافات و + و - و () بس")
        return v


def _get_row(db: Session) -> models.CompanySettings:
    row = db.get(models.CompanySettings, 1)
    if row is None:
        try:
            row = models.CompanySettings(id=1)
            db.add(row)
            db.flush()
        except IntegrityError:  # أدمنين عملوا أول حفظ في نفس اللحظة
            db.rollback()
            row = db.get(models.CompanySettings, 1)
    return row


def _public(db: Session) -> dict:
    """بيانات الشركة من غير ما نحمّل اللوجو نفسه (أعمدة خفيفة بس)."""
    C = models.CompanySettings
    r = db.query(C.name, C.phone1, C.phone2, C.logo_etag).filter(C.id == 1).first()
    if not r:
        return {"name": None, "phone1": None, "phone2": None, "has_logo": False, "logo_version": None}
    return {"name": r.name, "phone1": r.phone1, "phone2": r.phone2, "has_logo": bool(r.logo_etag), "logo_version": r.logo_etag}


@router.get("")
def get_company(db: Session = Depends(get_db), current_user: models.User = Depends(auth.get_current_user)):
    return _public(db)


@router.put("")
def update_company(payload: CompanyIn, db: Session = Depends(get_db), admin: models.User = Depends(auth.require_admin)):
    row = _get_row(db)
    row.name, row.phone1, row.phone2 = payload.name, payload.phone1, payload.phone2
    db.commit()
    return _public(db)


@router.post("/logo")
def upload_logo(
    request: Request,
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
    admin: models.User = Depends(auth.require_admin),
):
    length = request.headers.get("content-length")
    if length and length.isdigit() and int(length) > MAX_LOGO_BYTES + 64 * 1024:
        raise HTTPException(status_code=413, detail=f"حجم اللوجو أكبر من {MAX_LOGO_LABEL}")
    data = file.file.read(MAX_LOGO_BYTES + 1)
    if not data:
        raise HTTPException(status_code=400, detail="الملف فاضي")
    if len(data) > MAX_LOGO_BYTES:
        raise HTTPException(status_code=413, detail=f"حجم اللوجو أكبر من {MAX_LOGO_LABEL}")
    mime = sniff_image_mime(data)
    if mime is None:
        raise HTTPException(status_code=400, detail="الملف مش صورة صالحة - المسموح PNG أو JPEG أو WebP بس")
    row = _get_row(db)
    row.logo, row.logo_mime, row.logo_etag = data, mime, hashlib.sha1(data).hexdigest()
    db.commit()
    return _public(db)


@router.delete("/logo")
def delete_logo(db: Session = Depends(get_db), admin: models.User = Depends(auth.require_admin)):
    row = _get_row(db)
    row.logo = row.logo_mime = row.logo_etag = None
    db.commit()
    return _public(db)


@router.get("/logo")
def get_logo(request: Request, db: Session = Depends(get_db), current_user: models.User = Depends(auth.get_current_user)):
    C = models.CompanySettings
    meta = db.query(C.logo_etag, C.logo_mime).filter(C.id == 1).first()
    if not meta or not meta.logo_etag:
        raise HTTPException(status_code=404, detail="مفيش لوجو")
    etag = f'"{meta.logo_etag}"'
    headers = {"ETag": etag, "Cache-Control": "private, max-age=86400", "Vary": "Cookie", "X-Content-Type-Options": "nosniff"}
    sent = [t.strip().removeprefix("W/") for t in (request.headers.get("if-none-match") or "").split(",")]
    if etag in sent:
        return Response(status_code=304, headers=headers)
    blob = db.query(C.logo).filter(C.id == 1).scalar()
    if not blob:
        raise HTTPException(status_code=404, detail="مفيش لوجو")
    return Response(content=bytes(blob), media_type=meta.logo_mime or "application/octet-stream", headers=headers)

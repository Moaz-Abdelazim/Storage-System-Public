"""إعدادات التطبيق (key/value) في جدول app_settings + التحقق من PUBLIC_BASE_URL (HTTPS فقط، من غير path/query)."""
import os
import re
from typing import Optional
from urllib.parse import urlparse

from fastapi import HTTPException
from sqlalchemy.orm import Session

from .. import models
from ..timezone_utils import now_cairo

PUBLIC_URL_KEY = "public_base_url"
SETUP_DONE_KEY = "setup_completed"


def normalize_public_base_url(raw: Optional[str]) -> str:
    """
    بيرجّع https://domain[:port] (من غير / في الآخر) أو "" لو فاضي. بيرمي 400 لو:
    مش https، من غير دومين، فيه مسار أو query أو fragment أو بيانات دخول (user@) أو مسافات.
    """
    text = (raw or "").strip()
    if not text:
        return ""
    if len(text) > 255 or re.search(r"\s", text):
        raise HTTPException(status_code=400, detail="العنوان العام غير صالح")
    try:
        u = urlparse(text.rstrip("/"))
        port = u.port  # بيرمي ValueError لو البورت غلط
    except ValueError:
        raise HTTPException(status_code=400, detail="العنوان العام غير صالح")
    if u.scheme != "https":
        raise HTTPException(status_code=400, detail="العنوان العام لازم يبدأ بـ https://")
    if not u.hostname or u.username or u.password or u.path not in ("", "/") or u.query or u.fragment or u.params:
        raise HTTPException(status_code=400, detail="العنوان العام لازم يكون دومين بس بدون مسار أو بارامترات (مثال: https://stock.example.com)")
    host = u.hostname.lower()
    return f"https://{host}:{port}" if port else f"https://{host}"


def get_setting(db: Session, key: str) -> Optional[str]:
    row = db.get(models.AppSetting, key)
    return row.value if row else None


def set_setting(db: Session, key: str, value: Optional[str], user: Optional[models.User] = None) -> None:
    """بيكتب/بيمسح (value فاضي) من غير commit."""
    row = db.get(models.AppSetting, key)
    if not value:
        if row is not None:
            db.delete(row)
        return
    if row is None:
        db.add(models.AppSetting(key=key, value=value, updated_by_id=getattr(user, "id", None),
                                 updated_by_username=getattr(user, "username", None), updated_at=now_cairo()))
    else:
        row.value, row.updated_by_id, row.updated_by_username = value, getattr(user, "id", None), getattr(user, "username", None)


def _env_public_url() -> str:
    try:
        return normalize_public_base_url(os.getenv("PUBLIC_BASE_URL", ""))
    except HTTPException:
        print("⚠ PUBLIC_BASE_URL غير صالح (لازم https://domain من غير مسار) - اتتجاهل")
        return ""


def effective_public_base_url(db: Session) -> tuple:
    """(القيمة، المصدر): الإعداد المحفوظ من الشاشة له الأولوية، وبعدين متغير البيئة. المصدر: db / env / none."""
    stored = get_setting(db, PUBLIC_URL_KEY)
    if stored:
        return stored, "db"
    env = _env_public_url()
    return (env, "env") if env else ("", "none")

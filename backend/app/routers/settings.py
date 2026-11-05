"""إعدادات التطبيق العامة. حاليًا: العنوان العام (PUBLIC_BASE_URL) اللي بيتثبّت في الـ QR بتاع الطباعة. الصلاحية: settings.company."""
from typing import Optional

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from .. import auth, models
from ..database import get_db
from ..services import app_settings
from ..services.audit import audit

router = APIRouter(prefix="/api/settings", tags=["Settings"])


class PublicUrlIn(BaseModel):
    public_base_url: Optional[str] = Field(default=None, max_length=255)


def _view(db: Session) -> dict:
    value, source = app_settings.effective_public_base_url(db)
    return {"public_base_url": value, "source": source}


@router.get("/public-url")
def get_public_url(db: Session = Depends(get_db), current_user: models.User = Depends(auth.require_permission("settings.company"))):
    return _view(db)


@router.put("/public-url")
def put_public_url(
    payload: PublicUrlIn,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(auth.require_permission("settings.company")),
):
    """HTTPS فقط ومن غير path أو query. قيمة فاضية = مسح الإعداد (يرجع لمتغير البيئة أو لدومين الصفحة)."""
    new = app_settings.normalize_public_base_url(payload.public_base_url)
    old = app_settings.get_setting(db, app_settings.PUBLIC_URL_KEY) or ""
    app_settings.set_setting(db, app_settings.PUBLIC_URL_KEY, new, current_user)
    audit(db, current_user, "settings", app_settings.PUBLIC_URL_KEY, "update", {"before": old, "after": new})
    db.commit()
    return _view(db)

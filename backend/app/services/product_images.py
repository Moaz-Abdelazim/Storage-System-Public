"""
دورة حياة صورة المنتج: رفع/تحديث/حذف، مع ضمان إن مفيش ملفات يتيمة ومفيش صورة قديمة بتتمسح قبل ما الجديدة تنجح.

الترتيب في replace_product_image:
  1) معالجة الصورة في الذاكرة (لو فشلت: ImageError ومفيش أي أثر).
  2) حفظ الملفين (main + thumb) تحت key جديد.   لو فشل: بنمسح اللي اتكتب ونرمي الخطأ.
  3) تحديث products.image_key + سجل تدقيق وcommit.   لو فشل: بنمسح الملفين الجداد (القديمة لسه سليمة ومربوطة).
  4) بعد نجاح الـ commit بس: مسح ملفات الصورة القديمة (best effort).
"""
import logging
import uuid
from typing import Optional

from sqlalchemy.orm import Session

from .. import models
from .audit import audit
from .images import MAIN_NAME, THUMB_NAME, process_image
from .media_store import get_store

_log = logging.getLogger("app.media")


def replace_product_image(db: Session, product: models.Product, data: bytes, user: Optional[models.User], source: str = "upload") -> str:
    """بيرجّع الـ key الجديد. ImageError لو الصورة مرفوضة. أي استثناء تاني = الصورة القديمة (لو فيه) لسه شغالة."""
    processed = process_image(data)
    store = get_store()
    key = uuid.uuid4().hex
    try:
        store.put(key, MAIN_NAME, processed.main)
        store.put(key, THUMB_NAME, processed.thumb)
    except Exception:
        store.delete(key)
        raise
    old_key = product.image_key
    product.image_key = key
    audit(db, user, "product", product.id, "image_set", {"source": source, "replaced": bool(old_key)})
    try:
        db.commit()
    except Exception:
        db.rollback()
        store.delete(key)
        raise
    if old_key:
        store.delete(old_key)
    return key


def remove_product_image(db: Session, product: models.Product, user: Optional[models.User]) -> bool:
    """بيشيل صورة المنتج. False لو مفيش صورة أصلًا."""
    old_key = product.image_key
    if not old_key:
        return False
    product.image_key = None
    audit(db, user, "product", product.id, "image_remove", None)
    db.commit()
    get_store().delete(old_key)
    return True

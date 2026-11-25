"""
صور المنتجات: عرض (main / thumb) ورفع/تحديث وحذف.

- العرض بيعدّي على الصلاحيات (مفيش static مفتوح): نفس صلاحيات قراءة المنتجات. الرد بـ ETag؛ ولو الطلب فيه v=<key> الحالي
  (الواجهة بتبعته) بيتكاش سنة كاملة (private) لأن أي صورة جديدة = key جديد = URL جديد.
- الرفع/الحذف: products.edit. الصورة بتتعالج في السيرفر (تحقق من المحتوى، EXIF، ضغط، WebP، Thumbnail) وتتخزن بطريقة آمنة (services/product_images.py).
"""
from typing import Optional

from fastapi import APIRouter, Depends, File, HTTPException, Request, Response, UploadFile
from sqlalchemy.orm import Session

from .. import auth, models, permissions
from ..database import get_db
from ..rate_limit import limit_per_user
from ..services import product_images
from ..services.images import MAIN_NAME, MAX_IMAGE_BYTES, THUMB_NAME, ImageError
from ..services.media_store import get_store
from .products import _image_url

router = APIRouter(prefix="/api/products", tags=["Product Images"])

_SIZES = {"main": MAIN_NAME, "thumb": THUMB_NAME}
_YEAR = 365 * 24 * 3600


@router.get("/{product_id}/image")
def get_product_image(
    product_id: int,
    request: Request,
    size: str = "main",
    v: Optional[str] = None,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(auth.require_any_permission(*permissions.PRODUCT_READ)),
):
    if size not in _SIZES:
        raise HTTPException(status_code=400, detail="size لازم تكون main أو thumb")
    row = db.query(models.Product.image_key).filter(models.Product.id == product_id).first()
    if row is None:
        raise HTTPException(status_code=404, detail="المنتج مش موجود")
    key = row[0]
    if not key:
        raise HTTPException(status_code=404, detail="المنتج ملوش صورة")
    etag = f'"{key}-{size}"'
    cache = f"private, max-age={_YEAR}, immutable" if v == key else "private, no-cache"
    headers = {"ETag": etag, "Cache-Control": cache}
    if request.headers.get("if-none-match") == etag:
        return Response(status_code=304, headers=headers)
    data = get_store().read(key, _SIZES[size])
    if data is None:
        raise HTTPException(status_code=404, detail="ملف الصورة مش موجود")
    return Response(content=data, media_type="image/webp", headers=headers)


@router.put("/{product_id}/image")
def upload_product_image(
    product_id: int,
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
    current_user: models.User = Depends(auth.require_permission("products.edit")),
    _limit: None = Depends(limit_per_user("product_image", 300, 60 * 60)),
):
    """رفع/تحديث صورة المنتج. بيقبل JPEG/PNG/WebP بحسب المحتوى الفعلي، ويرجّع روابط الصورة الأساسية والـ Thumbnail."""
    if file.size is not None and file.size > MAX_IMAGE_BYTES:
        raise HTTPException(status_code=413, detail=f"الصورة كبيرة جدًا (الحد الأقصى {MAX_IMAGE_BYTES / 1024 / 1024:g} ميجا)")
    data = file.file.read(MAX_IMAGE_BYTES + 1)  # def عادي: المعالجة CPU/IO blocking فمتتعملش على الـ event loop
    product = db.query(models.Product).filter(models.Product.id == product_id).first()
    if not product:
        raise HTTPException(status_code=404, detail="المنتج مش موجود")
    try:
        product_images.replace_product_image(db, product, data, current_user)
    except ImageError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    db.refresh(product)
    return {"has_image": True, "image_url": _image_url(product, "main"), "thumb_url": _image_url(product, "thumb")}


@router.delete("/{product_id}/image")
def delete_product_image(
    product_id: int,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(auth.require_permission("products.edit")),
):
    product = db.query(models.Product).filter(models.Product.id == product_id).first()
    if not product:
        raise HTTPException(status_code=404, detail="المنتج مش موجود")
    removed = product_images.remove_product_image(db, product, current_user)
    return {"removed": removed, "has_image": False}

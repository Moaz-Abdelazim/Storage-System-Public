"""
رفع صور المنتجات بالجملة (ZIP أو صور متعددة بأسماء = SKU/باركود) + شاشة المطابقة.
المنطق كله في services/image_batches.py؛ هنا HTTP + صلاحيات (products.edit) + ملكية الدفعة (المستخدم اللي رفعها بس).
"""
from typing import List, Optional

from fastapi import APIRouter, Depends, File, HTTPException, Response, UploadFile
from pydantic import BaseModel
from sqlalchemy.orm import Session

from .. import auth, models
from ..database import get_db
from ..rate_limit import limit_per_user
from ..services import image_batches
from ..services.image_batches import BatchError
from ..services.images import THUMB_NAME
from ..services.media_store import get_store

router = APIRouter(prefix="/api/product-images", tags=["Product Image Batches"])

MAX_UPLOAD_FILE_BYTES = 60 * 1024 * 1024   # ZIP واحد (الحد الكلي للطلب في main.py)


class AssignBody(BaseModel):
    product_id: Optional[int] = None       # None = شيل الاختيار


class ApplyBody(BaseModel):
    replace_existing: bool = False


def _load(token: str, user: models.User) -> dict:
    try:
        return image_batches.load_batch(token, user)
    except BatchError as exc:
        raise HTTPException(status_code=404, detail=str(exc))


@router.post("/batches")
def create_batch(
    files: List[UploadFile] = File(...),
    db: Session = Depends(get_db),
    current_user: models.User = Depends(auth.require_permission("products.edit")),
    _limit: None = Depends(limit_per_user("image_batch", 30, 60 * 60)),
):
    """ارفع ZIP واحد أو عدة صور. كل صورة بتتعالج وبتتطابق باسمها مع SKU/الباركود، وبيرجّع ملخص + تفاصيل كل صورة."""
    uploads = []
    for f in files:
        data = f.file.read(MAX_UPLOAD_FILE_BYTES + 1)
        if len(data) > MAX_UPLOAD_FILE_BYTES:
            raise HTTPException(status_code=413, detail="الملف كبير جدًا")
        uploads.append((f.filename or "", data))
    try:
        meta = image_batches.create_batch(db, current_user, uploads)
    except BatchError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return image_batches.serialize(db, meta)


@router.get("/batches/{token}")
def get_batch(token: str, db: Session = Depends(get_db), current_user: models.User = Depends(auth.require_permission("products.edit"))):
    return image_batches.serialize(db, _load(token, current_user))


@router.get("/batches/{token}/items/{item_id}/thumb")
def batch_item_thumb(token: str, item_id: int, current_user: models.User = Depends(auth.require_permission("products.edit"))):
    meta = _load(token, current_user)
    item = next((i for i in meta["items"] if i["id"] == item_id), None)
    data = get_store().read(item["key"], THUMB_NAME) if item and item["key"] else None
    if data is None:
        raise HTTPException(status_code=404, detail="الصورة مش موجودة")
    return Response(content=data, media_type="image/webp", headers={"Cache-Control": "private, max-age=3600"})


@router.put("/batches/{token}/items/{item_id}")
def assign_item(token: str, item_id: int, body: AssignBody, db: Session = Depends(get_db), current_user: models.User = Depends(auth.require_permission("products.edit"))):
    meta = _load(token, current_user)
    try:
        image_batches.assign(db, meta, item_id, body.product_id)
    except BatchError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return image_batches.serialize(db, meta)


@router.post("/batches/{token}/apply")
def apply_batch(token: str, body: ApplyBody, db: Session = Depends(get_db), current_user: models.User = Depends(auth.require_permission("products.edit"))):
    """بيربط الصور المطابقة بمنتجاتها وبينهي الدفعة (الصور غير المطابقة بتتمسح)."""
    meta = _load(token, current_user)
    return image_batches.apply(db, current_user, meta, body.replace_existing)


@router.delete("/batches/{token}")
def discard_batch(token: str, current_user: models.User = Depends(auth.require_permission("products.edit"))):
    meta = _load(token, current_user)
    image_batches.discard(meta)
    return {"discarded": True}

"""
رفع صور المنتجات بالجملة: ZIP (أو صور متعددة) أسماء ملفاتها = SKU أو باركود، مع شاشة مطابقة للي ما اتطابقش.

التدفق (من غير جدول جديد في الـ DB):
  1) create_batch: بنفك الـ ZIP (بحدود أمان)، وكل صورة بتتعالج فورًا (تحقق من المحتوى + WebP + Thumbnail) وتتخزن تحت key جديد
     "جاهز" لكن مش مربوط بأي منتج. ملف meta.json (في staging/<token>) بيسجّل كل صورة وحالتها ومنتجها.
  2) assign: المستخدم يختار منتج لصورة (أو يشيل الاختيار). المنتج الواحد ليه صورة مطابقة واحدة بس في الدفعة.
  3) apply: الصور المطابقة بتتربط بمنتجاتها (commit لكل منتج)، المنتج اللي ليه صورة بيتخطى إلا لو replace_existing. الباقي بيتمسح.
  4) discard / purge_expired: مسح الدفعة وصورها اللي ما اتربطتش (الدفعات القديمة > 24 ساعة بتتمسح تلقائيًا).

المطابقة: اسم الملف من غير الامتداد == SKU أو الباركود (من غير حساسية لحالة الحروف). لو ما لقيناش وفي آخره رقم ترقيم (_1 أو -2 أو (3))
بنجرب من غيره. اسم بيطابق أكتر من منتج = مبهم (ambiguous) والمستخدم يختار. أكتر من صورة لنفس المنتج: الأولى (أبجديًا) بتتطابق والباقي duplicate.
"""
import io
import logging
import posixpath
import re
import time
import uuid
import zipfile
from typing import Dict, List, Optional, Tuple

from sqlalchemy.orm import Session

from .. import models
from .audit import audit
from .images import MAIN_NAME, MAX_IMAGE_BYTES, THUMB_NAME, ImageError, process_image
from .media_store import get_store

_log = logging.getLogger("app.media")

MAX_BATCH_IMAGES = 300             # عدد الصور في الدفعة الواحدة (المعالجة متزامنة داخل الطلب: سقف يحافظ على زمن الرد)
MAX_ZIP_ENTRIES = 3000
MAX_ZIP_TOTAL_BYTES = 400 * 1024 * 1024
BATCH_TTL_SECONDS = 24 * 3600
_JUNK_NAMES = {".ds_store", "thumbs.db", "desktop.ini"}
_SUFFIX_RE = re.compile(r"(?:[\s_\-]+\d+|\s*\(\d+\))$")

STATUSES = ("matched", "unmatched", "ambiguous", "duplicate", "invalid")


class BatchError(ValueError):
    """مشكلة في الدفعة نفسها (مش في صورة واحدة): رسالة عربي للمستخدم."""


# ------------------------------------------------------------------ تجميع الملفات
def _fix_zip_name(info: zipfile.ZipInfo) -> str:
    name = info.filename
    if not (info.flag_bits & 0x800):  # مش UTF-8: بايثون بيفك cp437، فنجرّب نرجّعها UTF-8 (أسماء عربي من ويندوز/ماك)
        try:
            name = name.encode("cp437").decode("utf-8")
        except (UnicodeEncodeError, UnicodeDecodeError):
            pass
    return name


def _is_junk(path: str) -> bool:
    parts = [p for p in path.replace("\\", "/").split("/") if p]
    base = parts[-1].lower() if parts else ""
    return not parts or "__macosx" in (x.lower() for x in parts) or base in _JUNK_NAMES or base.startswith("._")


def collect_files(uploads: List[Tuple[str, bytes]]) -> List[Tuple[str, Optional[bytes], Optional[str]]]:
    """[(filename, bytes | None, error | None)]. ZIP بيتفك (من غير ما نكتب على disk) وبيتتجاهل ملفات النظام."""
    out: List[Tuple[str, Optional[bytes], Optional[str]]] = []
    for filename, data in uploads:
        if (filename or "").lower().endswith(".zip") or data[:4] == b"PK\x03\x04":
            try:
                zf = zipfile.ZipFile(io.BytesIO(data))
            except zipfile.BadZipFile:
                raise BatchError(f"الملف ({filename}) مش ZIP سليم")
            infos = [i for i in zf.infolist() if not i.is_dir()]
            if len(infos) > MAX_ZIP_ENTRIES or sum(i.file_size for i in infos) > MAX_ZIP_TOTAL_BYTES:
                raise BatchError("ملف الـ ZIP كبير أو غير طبيعي")
            for info in infos:
                name = _fix_zip_name(info)
                if _is_junk(name) or ".." in name.replace("\\", "/").split("/"):
                    continue
                base = posixpath.basename(name.replace("\\", "/"))
                if info.file_size > MAX_IMAGE_BYTES:
                    out.append((base, None, f"الصورة كبيرة جدًا (الحد الأقصى {MAX_IMAGE_BYTES / 1024 / 1024:g} ميجا)"))
                    continue
                with zf.open(info) as f:
                    blob = f.read(MAX_IMAGE_BYTES + 1)  # قراءة محدودة: مبنصدّقش حجم الهيدر
                out.append((base, blob, None) if len(blob) <= MAX_IMAGE_BYTES else (base, None, "الصورة كبيرة جدًا"))
        else:
            if _is_junk(filename or ""):
                continue
            out.append((posixpath.basename((filename or "").replace("\\", "/")), data, None))
    return out


# ------------------------------------------------------------------ المطابقة
def _norm(s: str) -> str:
    return " ".join((s or "").strip().casefold().split())


def _build_maps(db: Session) -> Tuple[Dict[str, List[int]], Dict[str, List[int]]]:
    sku_map: Dict[str, List[int]] = {}
    barcode_map: Dict[str, List[int]] = {}
    for pid, sku, barcode in db.query(models.Product.id, models.Product.sku, models.Product.barcode).all():
        if sku:
            sku_map.setdefault(_norm(sku), []).append(pid)
        if barcode:
            barcode_map.setdefault(_norm(barcode), []).append(pid)
    return sku_map, barcode_map


def match_name(filename: str, sku_map, barcode_map) -> Tuple[str, List[int], Optional[str]]:
    """(status, product_ids, how). status: matched | ambiguous | unmatched."""
    stem = _norm(posixpath.splitext(filename)[0])
    candidates = [stem]
    stripped = _SUFFIX_RE.sub("", stem).strip()
    if stripped and stripped != stem:
        candidates.append(stripped)
    for i, cand in enumerate(candidates):
        ids = sorted(set(sku_map.get(cand, [])) | set(barcode_map.get(cand, [])))
        if len(ids) == 1:
            how = "sku" if cand in sku_map else "barcode"
            return "matched", ids, how + ("_suffix" if i else "")
        if len(ids) > 1:
            return "ambiguous", ids, None
    return "unmatched", [], None


def _dedupe(items: List[dict]) -> None:
    """منتج واحد = صورة مطابقة واحدة: الأولى أبجديًا matched والباقي duplicate."""
    seen: Dict[int, int] = {}
    for it in sorted(items, key=lambda i: i["filename"].lower()):
        if it["status"] == "matched" and it["product_id"] is not None:
            if it["product_id"] in seen:
                it["status"] = "duplicate"
            else:
                seen[it["product_id"]] = it["id"]


# ------------------------------------------------------------------ العمليات
def purge_expired() -> int:
    """بيمسح الدفعات القديمة (وصورها). بيتنادى عند إنشاء أي دفعة جديدة."""
    store, n = get_store(), 0
    for token, mtime in store.staging_list():
        if time.time() - mtime > BATCH_TTL_SECONDS:
            meta = store.staging_read(token)
            for it in (meta or {}).get("items", []):
                if it.get("key"):
                    store.delete(it["key"])
            store.staging_delete(token)
            n += 1
    return n


def create_batch(db: Session, user: models.User, uploads: List[Tuple[str, bytes]]) -> dict:
    files = collect_files(uploads)
    if not files:
        raise BatchError("مفيش صور في الملفات اللي اترفعت")
    if len(files) > MAX_BATCH_IMAGES:
        raise BatchError(f"عدد الصور كبير ({len(files)}). الحد الأقصى {MAX_BATCH_IMAGES} صورة في المرة الواحدة، قسّمها على دفعات")
    purge_expired()
    store = get_store()
    sku_map, barcode_map = _build_maps(db)
    token = uuid.uuid4().hex
    items: List[dict] = []
    try:
        for n, (name, blob, err) in enumerate(sorted(files, key=lambda f: f[0].lower()), start=1):
            item = {"id": n, "filename": name, "key": None, "status": "invalid", "error": err, "product_id": None, "candidates": [], "how": None}
            if err is None:
                try:
                    processed = process_image(blob)
                    key = uuid.uuid4().hex
                    try:
                        store.put(key, MAIN_NAME, processed.main)
                        store.put(key, THUMB_NAME, processed.thumb)
                    except Exception:
                        store.delete(key)
                        raise
                    item["key"] = key
                    status, ids, how = match_name(name, sku_map, barcode_map)
                    item.update(status=status, how=how, product_id=ids[0] if status == "matched" else None,
                                candidates=ids if status == "ambiguous" else [])
                except ImageError as exc:
                    item["error"] = str(exc)
                except Exception:
                    _log.exception("image batch: storing %s failed", name)
                    item["error"] = "تعذّر حفظ الصورة"
            items.append(item)
        _dedupe(items)
        meta = {"token": token, "user_id": user.id, "created": time.time(), "items": items}
        store.staging_write(token, meta)
    except Exception:
        for it in items:
            if it.get("key"):
                store.delete(it["key"])
        store.staging_delete(token)
        raise
    return meta


def load_batch(token: str, user: models.User) -> dict:
    try:
        meta = get_store().staging_read(token)
    except ValueError:
        meta = None
    if meta is None or meta.get("user_id") != user.id:
        raise BatchError("الدفعة مش موجودة أو انتهت (الدفعات بتتمسح بعد 24 ساعة)")
    return meta


def _save(meta: dict) -> None:
    get_store().staging_write(meta["token"], meta)


def serialize(db: Session, meta: dict) -> dict:
    items = meta["items"]
    ids = {i for it in items for i in ([it["product_id"]] if it["product_id"] else []) + it.get("candidates", [])}
    prods = {}
    if ids:
        for pid, name, sku, barcode, image_key in db.query(models.Product.id, models.Product.name, models.Product.sku, models.Product.barcode, models.Product.image_key).filter(models.Product.id.in_(ids)).all():
            prods[pid] = {"id": pid, "name": name, "sku": sku, "barcode": barcode, "has_image": bool(image_key)}
    token = meta["token"]
    out = []
    for it in items:
        out.append({
            "id": it["id"], "filename": it["filename"], "status": it["status"], "error": it["error"], "how": it["how"],
            "product": prods.get(it["product_id"]) if it["product_id"] else None,
            "candidates": [prods[c] for c in it.get("candidates", []) if c in prods],
            "thumb_url": f"/api/product-images/batches/{token}/items/{it['id']}/thumb" if it["key"] else None,
        })
    counts = {s: sum(1 for it in items if it["status"] == s) for s in STATUSES}
    return {"token": token, "total": len(items), "counts": counts, "items": out}


def assign(db: Session, meta: dict, item_id: int, product_id: Optional[int]) -> None:
    item = next((i for i in meta["items"] if i["id"] == item_id), None)
    if item is None:
        raise BatchError("الصورة مش موجودة في الدفعة")
    if item["key"] is None:
        raise BatchError("الصورة دي فيها مشكلة ومينفعش تتربط")
    if product_id is None:
        item.update(status="unmatched", product_id=None, how=None)
    else:
        if db.query(models.Product.id).filter(models.Product.id == product_id).first() is None:
            raise BatchError("المنتج مش موجود")
        for other in meta["items"]:  # المنتج الواحد = صورة واحدة: اللي كانت متطابقة معاه قبل كده بتبقى duplicate
            if other is not item and other["product_id"] == product_id and other["status"] == "matched":
                other["status"] = "duplicate"
        item.update(status="matched", product_id=product_id, how="manual", candidates=[])
    _save(meta)


def apply(db: Session, user: models.User, meta: dict, replace_existing: bool) -> dict:
    store = get_store()
    applied = skipped = 0
    failed: List[dict] = []
    attached: set = set()
    for it in meta["items"]:
        if it["status"] != "matched" or not it["key"] or not it["product_id"]:
            continue
        product = db.get(models.Product, it["product_id"])
        if product is None:
            failed.append({"filename": it["filename"], "message": "المنتج اتحذف"})
            continue
        if product.image_key and not replace_existing:
            skipped += 1
            continue
        old = product.image_key
        product.image_key = it["key"]
        audit(db, user, "product", product.id, "image_set", {"source": "batch", "replaced": bool(old)})
        try:
            db.commit()
        except Exception:
            db.rollback()
            _log.exception("image batch: apply %s failed", it["filename"])
            failed.append({"filename": it["filename"], "message": "تعذّر حفظ الصورة"})
            continue
        attached.add(it["key"])
        applied += 1
        if old:
            store.delete(old)
    left = sum(1 for it in meta["items"] if it["status"] in ("unmatched", "ambiguous", "duplicate"))
    invalid = sum(1 for it in meta["items"] if it["status"] == "invalid")
    for it in meta["items"]:           # كل صورة ما اتربطتش تتمسح (الدفعة بتنتهي بالتطبيق)
        if it["key"] and it["key"] not in attached:
            store.delete(it["key"])
    store.staging_delete(meta["token"])
    return {"applied": applied, "skipped_existing": skipped, "failed": failed, "unmatched_discarded": left, "invalid": invalid}


def discard(meta: dict) -> None:
    store = get_store()
    for it in meta["items"]:
        if it["key"]:
            store.delete(it["key"])
    store.staging_delete(meta["token"])

"""رفع صور بالجملة (ZIP / صور متعددة بأسماء SKU أو باركود) + المطابقة اليدوية + التطبيق."""
import io
import os
import time
import zipfile

import pytest
from PIL import Image

from app import models
from app.services import image_batches
from app.services.media_store import get_store

from .helpers import make_image_bytes, make_product

B = "/api/product-images/batches"


def _zip(files):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for name, data in files.items():
            z.writestr(name, data)
    return buf.getvalue()


def _png(color=(200, 20, 20), size=(60, 40)):
    return make_image_bytes("PNG", size, color)


def _upload(c, files=None, zip_files=None, **kw):
    parts = []
    if zip_files is not None:
        parts.append(("files", ("images.zip", _zip(zip_files), "application/zip")))
    for name, data in (files or {}).items():
        parts.append(("files", (name, data, "image/png")))
    return c.post(B, files=parts, **kw)


def _items(batch):
    return {i["filename"]: i for i in batch["items"]}


def _prod(c, name, sku=None, barcode=None):
    extra = {}
    if sku:
        extra["sku"] = sku
    if barcode:
        extra["barcode"] = barcode
    return make_product(c, name, **extra)


def _center(data):
    im = Image.open(io.BytesIO(data)).convert("RGB")
    return im.getpixel((im.width // 2, im.height // 2))


# ---------------- المطابقة التلقائية ----------------
def test_zip_matches_by_sku_and_barcode_and_lists_the_rest(admin):
    a = _prod(admin, "أ", sku="SKU-A")
    b = _prod(admin, "ب", barcode="6221234567890")
    r = _upload(admin, zip_files={"SKU-A.png": _png((200, 0, 0)), "6221234567890.png": _png((0, 200, 0)), "nobody.png": _png(), "broken.png": b"not an image"})
    assert r.status_code == 200, r.text
    batch = r.json()
    it = _items(batch)
    assert it["SKU-A.png"]["status"] == "matched" and it["SKU-A.png"]["product"]["id"] == a["id"] and it["SKU-A.png"]["how"] == "sku"
    assert it["6221234567890.png"]["product"]["id"] == b["id"] and it["6221234567890.png"]["how"] == "barcode"
    assert it["nobody.png"]["status"] == "unmatched" and it["nobody.png"]["product"] is None
    assert it["broken.png"]["status"] == "invalid" and "صورة" in it["broken.png"]["error"] and it["broken.png"]["thumb_url"] is None
    assert batch["counts"] == {"matched": 2, "unmatched": 1, "ambiguous": 0, "duplicate": 0, "invalid": 1} and batch["total"] == 4
    thumb = admin.get(it["SKU-A.png"]["thumb_url"])
    assert thumb.status_code == 200 and thumb.headers["content-type"] == "image/webp"


def test_name_matching_rules(admin):
    p = _prod(admin, "منتج", sku="Abc-1")
    q = _prod(admin, "تاني", sku="XYZ")
    z = {"folder/sub/abc-1.png": _png(), "xyz_2.png": _png(), "__MACOSX/._abc-1.png": b"junk", ".DS_Store": b"junk", "Thumbs.db": b"junk"}
    batch = _upload(admin, zip_files=z).json()
    it = _items(batch)
    assert set(it) == {"abc-1.png", "xyz_2.png"}                                    # الملفات الزبالة اتتجاهلت والمجلدات اتشالت
    assert it["abc-1.png"]["product"]["id"] == p["id"]                              # من غير حساسية لحالة الحروف
    assert it["xyz_2.png"]["product"]["id"] == q["id"] and it["xyz_2.png"]["how"] == "sku_suffix"   # ترقيم _2 اتشال


def test_ambiguous_name_needs_manual_choice(admin, db):
    """الـ API بيمنع باركود يطابق SKU منتج تاني، لكن بيانات قديمة ممكن تكون كده: الكود لازم يتصرف (يسأل المستخدم) مش يخمّن."""
    a = _prod(admin, "بالـ SKU", sku="X1")
    row = models.Product(name="بالباركود", price=1, barcode="x1")
    db.add(row)
    db.commit()
    b = {"id": row.id}
    batch = _upload(admin, files={"X1.png": _png()}).json()
    item = batch["items"][0]
    assert item["status"] == "ambiguous" and {c["id"] for c in item["candidates"]} == {a["id"], b["id"]} and item["product"] is None
    done = admin.put(f"{B}/{batch['token']}/items/{item['id']}", json={"product_id": b["id"]}).json()
    assert done["items"][0]["status"] == "matched" and done["items"][0]["product"]["id"] == b["id"] and done["items"][0]["how"] == "manual"


def test_two_images_for_one_product_first_wins_and_user_can_swap(admin):
    p = _prod(admin, "منتج", sku="DUP")
    batch = _upload(admin, files={"DUP.png": _png((200, 0, 0)), "DUP_2.png": _png((0, 0, 200))}).json()
    it = _items(batch)
    assert it["DUP.png"]["status"] == "matched" and it["DUP_2.png"]["status"] == "duplicate" and batch["counts"]["duplicate"] == 1
    swapped = _items(admin.put(f"{B}/{batch['token']}/items/{it['DUP_2.png']['id']}", json={"product_id": p["id"]}).json())
    assert swapped["DUP_2.png"]["status"] == "matched" and swapped["DUP.png"]["status"] == "duplicate"
    # المنتج الواحد مفيش ليه غير صورة مطابقة واحدة
    applied = admin.post(f"{B}/{batch['token']}/apply", json={}).json()
    assert applied["applied"] == 1
    assert _center(admin.get(f"/api/products/{p['id']}/image").content)[2] > 150          # الأزرق (اللي اخترناه)


# ---------------- المطابقة اليدوية والتطبيق ----------------
def test_manual_assign_unassign_and_validation(admin):
    p = _prod(admin, "منتج", sku="S1")
    batch = _upload(admin, files={"random-name.png": _png(), "bad.txt": b"nope"}).json()
    it = _items(batch)
    iid = it["random-name.png"]["id"]
    url = f"{B}/{batch['token']}/items/{iid}"
    assert it["random-name.png"]["status"] == "unmatched"
    state = lambda r: _items(r.json())["random-name.png"]["status"]  # noqa: E731
    assert state(admin.put(url, json={"product_id": p["id"]})) == "matched"
    assert state(admin.put(url, json={"product_id": None})) == "unmatched"                               # شيل الاختيار
    assert admin.put(url, json={"product_id": 99999}).status_code == 400                               # منتج مش موجود
    assert admin.put(f"{B}/{batch['token']}/items/999", json={"product_id": p["id"]}).status_code == 400
    assert admin.put(f"{B}/{batch['token']}/items/{it['bad.txt']['id']}", json={"product_id": p["id"]}).status_code == 400   # صورة تالفة مينفعش تتربط
    assert admin.get(f"{B}/{batch['token']}").json()["counts"]["unmatched"] == 1                       # الحالة متحفوظة بين الطلبات


def test_apply_links_images_skips_existing_and_cleans_everything(admin, db):
    a, b, c = (_prod(admin, n, sku=s) for n, s in (("أ", "A1"), ("ب", "B1"), ("ج", "C1")))
    batch = _upload(admin, zip_files={"A1.png": _png((200, 0, 0)), "B1.png": _png((0, 200, 0)), "nope.png": _png((0, 0, 200)), "bad.png": b"x"}).json()
    r = admin.post(f"{B}/{batch['token']}/apply", json={}).json()
    assert r == {"applied": 2, "skipped_existing": 0, "failed": [], "unmatched_discarded": 1, "invalid": 1}
    keys = {x.sku: x.image_key for x in db.query(models.Product).all()}
    db.rollback()  # ينهي الـ transaction (MySQL REPEATABLE READ)
    assert all(admin.get(f"/api/products/{p['id']}/image").status_code == 200 for p in (a, b)) and admin.get(f"/api/products/{c['id']}/image").status_code == 404
    store = get_store()
    assert len(store.list_keys()) == 2 and store.staging_list() == []                 # الصور غير المطابقة والدفعة اتمسحوا
    assert admin.get(f"{B}/{batch['token']}").status_code == 404                       # الدفعة انتهت
    # دفعة تانية: A1 عنده صورة => بيتخطى، وبـ replace بيستبدل والقديمة بتتمسح
    old_key = db.query(models.Product).filter_by(sku="A1").one().image_key
    again = _upload(admin, files={"A1.png": _png((250, 250, 0))}).json()
    assert admin.post(f"{B}/{again['token']}/apply", json={}).json()["skipped_existing"] == 1
    assert len(store.list_keys()) == 2
    third = _upload(admin, files={"A1.png": _png((250, 250, 0))}).json()
    assert admin.post(f"{B}/{third['token']}/apply", json={"replace_existing": True}).json()["applied"] == 1
    db.rollback()  # ينهي الـ transaction (MySQL REPEATABLE READ)
    new_key = db.query(models.Product).filter_by(sku="A1").one().image_key
    assert new_key != old_key and old_key not in store.list_keys() and len(store.list_keys()) == 2


def test_discard_removes_all_staged_files(admin):
    _prod(admin, "أ", sku="A1")
    batch = _upload(admin, files={"A1.png": _png(), "B.png": _png()}).json()
    assert len(get_store().list_keys()) == 2
    assert admin.delete(f"{B}/{batch['token']}").json() == {"discarded": True}
    assert get_store().list_keys() == [] and get_store().staging_list() == []
    assert admin.delete(f"{B}/{batch['token']}").status_code == 404


# ---------------- ملكية وصلاحيات ----------------
def test_ownership_and_permissions(admin, login, anon):
    _prod(admin, "أ", sku="A1")
    batch = _upload(admin, files={"A1.png": _png()}).json()
    other = login("editor2", ["products.edit"])
    assert other.get(f"{B}/{batch['token']}").status_code == 404                                      # دفعة غيره = مش موجودة
    assert other.post(f"{B}/{batch['token']}/apply", json={}).status_code == 404
    assert login("viewer2", ["products.view"]).post(B, files=[("files", ("a.png", _png(), "image/png"))]).status_code == 403
    assert anon.post(B, files=[("files", ("a.png", _png(), "image/png"))]).status_code == 401
    ok = login("editor3", ["products.edit"])
    assert _upload(ok, files={"zzz.png": _png()}).status_code == 200


# ---------------- حدود وأخطاء ----------------
def test_limits_and_bad_inputs(admin, monkeypatch):
    assert admin.post(B, files=[("files", ("x.zip", b"PK\x03\x04broken", "application/zip"))]).status_code == 400
    assert _upload(admin, zip_files={"__MACOSX/a.png": b"x", ".DS_Store": b"x"}).status_code == 400    # مفيش صور فعلية
    monkeypatch.setattr(image_batches, "MAX_BATCH_IMAGES", 3)
    r = _upload(admin, zip_files={f"{i}.png": _png() for i in range(4)})
    assert r.status_code == 400 and "الحد الأقصى 3" in r.json()["detail"]
    assert get_store().list_keys() == []                                                           # الرفض ما سابش ملفات


def test_zip_bigger_than_general_body_limit_is_accepted(admin):
    """الحد العام للطلب 12MB؛ مسار الصور بالجملة ليه حد أكبر."""
    noisy = io.BytesIO()
    Image.frombytes("RGB", (1500, 1500), os.urandom(1500 * 1500 * 3)).save(noisy, "PNG")
    assert len(noisy.getvalue()) < 8 * 1024 * 1024
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_STORED) as z:
        for i in range(3):
            z.writestr(f"n{i}.png", noisy.getvalue())
    assert len(buf.getvalue()) > 12 * 1024 * 1024
    r = admin.post(B, files=[("files", ("big.zip", buf.getvalue(), "application/zip"))])
    assert r.status_code == 200 and r.json()["total"] == 3


def test_oversize_single_image_in_zip_is_reported_not_fatal(admin, monkeypatch):
    from app.services import image_batches as ib

    monkeypatch.setattr(ib, "MAX_IMAGE_BYTES", 500)
    batch = _upload(admin, zip_files={"big.png": _png(size=(300, 300)) + os.urandom(2000), "small.png": b"x" * 10}).json()
    it = _items(batch)
    assert it["big.png"]["status"] == "invalid" and "كبيرة" in it["big.png"]["error"]


def test_storage_failure_marks_only_that_image_invalid(admin, monkeypatch):
    from app.services import media_store

    real, state = media_store.LocalMediaStore.put, {"n": 0}

    def flaky(self, key, name, data):
        state["n"] += 1
        if state["n"] == 1:
            raise OSError("disk full")
        return real(self, key, name, data)

    monkeypatch.setattr(media_store.LocalMediaStore, "put", flaky)
    batch = _upload(admin, files={"a.png": _png(), "b.png": _png()}).json()
    it = _items(batch)
    assert it["a.png"]["status"] == "invalid" and it["b.png"]["status"] == "unmatched"
    assert len(get_store().list_keys()) == 1                                                       # مفيش ملف يتيم من اللي فشل


def test_expired_batches_are_purged_on_next_batch(admin):
    old = _upload(admin, files={"old.png": _png()}).json()
    path = get_store()._staging(old["token"]) / "meta.json"
    past = time.time() - 25 * 3600
    os.utime(path, (past, past))
    new = _upload(admin, files={"new.png": _png()}).json()
    assert admin.get(f"{B}/{old['token']}").status_code == 404
    assert len(get_store().list_keys()) == 1 and [t for t, _ in get_store().staging_list()] == [new["token"]]


def test_zip_name_encoding_fix_for_arabic_names():
    name = "منتج-1.png"
    info = zipfile.ZipInfo(name.encode("utf-8").decode("cp437"))
    info.flag_bits = 0
    assert image_batches._fix_zip_name(info) == name

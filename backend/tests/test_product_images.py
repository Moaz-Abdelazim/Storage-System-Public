"""صور المنتجات: رفع/تحديث/حذف، المعالجة (WebP، Thumbnail، EXIF، Metadata)، الرفض، الصلاحيات، وعدم ترك ملفات يتيمة."""
import io
import os

import pytest
from PIL import Image

from app import models
from app.services import product_images
from app.services.images import MAX_IMAGE_BYTES, MAIN_MAX, THUMB_MAX, ImageError, process_image
from app.services.media_store import get_store

from .helpers import make_image_bytes, make_product, upload_image


def _open(data):
    return Image.open(io.BytesIO(data))


# ---------------- معالجة الصورة (وحدة) ----------------
def test_process_outputs_webp_main_and_thumb_with_aspect_ratio():
    out = process_image(make_image_bytes("JPEG", (3000, 1500)))
    main, thumb = _open(out.main), _open(out.thumb)
    assert main.format == "WEBP" and thumb.format == "WEBP"
    assert main.size == (MAIN_MAX, MAIN_MAX // 2)
    assert thumb.size == (THUMB_MAX, THUMB_MAX // 2)


def test_small_image_is_never_upscaled():
    out = process_image(make_image_bytes("PNG", (100, 60)))
    assert _open(out.main).size == (100, 60)
    assert _open(out.thumb).size == (100, 60)


@pytest.mark.parametrize("fmt", ["JPEG", "PNG", "WEBP"])
def test_accepts_jpeg_png_webp(fmt):
    assert _open(process_image(make_image_bytes(fmt)).main).format == "WEBP"


def test_exif_orientation_is_applied():
    # orientation=6 => الصورة لازم تتلف 90° (العرض والطول يتبدلوا)
    out = process_image(make_image_bytes("JPEG", (400, 200), exif_orientation=6))
    assert _open(out.main).size == (200, 400)


def test_metadata_is_stripped():
    out = process_image(make_image_bytes("JPEG", (400, 200), exif_orientation=3))
    for blob in (out.main, out.thumb):
        im = _open(blob)
        assert "exif" not in im.info and "icc_profile" not in im.info and not im.getexif()


def test_png_with_transparency_is_kept():
    img = Image.new("RGBA", (50, 50), (255, 0, 0, 0))
    buf = io.BytesIO()
    img.save(buf, "PNG")
    out = _open(process_image(buf.getvalue()).main)
    assert out.mode == "RGBA" and out.getpixel((1, 1))[3] == 0


@pytest.mark.parametrize("data,why", [
    (b"", "فاضي"),
    (b"this is not an image at all", "نص"),
    (b"\x89PNG\r\n\x1a\n" + b"garbage" * 10, "PNG تالف"),
    (make_image_bytes("GIF"), "GIF مش مدعومة"),
    (make_image_bytes("BMP"), "BMP مش مدعومة"),
    (make_image_bytes("JPEG", (800, 600))[:300], "JPEG مقطوع"),
])
def test_rejects_bad_or_unsupported(data, why):
    with pytest.raises(ImageError):
        process_image(data)


def test_rejects_huge_dimensions_and_huge_files():
    huge = Image.new("1", (9000, 9000))  # 81MP لكن الملف صغير
    buf = io.BytesIO()
    huge.save(buf, "PNG")
    with pytest.raises(ImageError):
        process_image(buf.getvalue())
    with pytest.raises(ImageError):
        process_image(b"\xff\xd8\xff" + os.urandom(MAX_IMAGE_BYTES + 10))


# ---------------- API ----------------
def test_upload_then_serve_main_and_thumb(admin):
    p = make_product(admin)
    r = upload_image(admin, p["id"], make_image_bytes("JPEG", (2000, 1000)))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["has_image"] and "size=main" in body["image_url"] and "size=thumb" in body["thumb_url"]
    main = admin.get(body["image_url"])
    thumb = admin.get(body["thumb_url"])
    assert main.status_code == thumb.status_code == 200
    assert main.headers["content-type"] == "image/webp"
    assert _open(main.content).size == (MAIN_MAX, MAIN_MAX // 2) and _open(thumb.content).size == (THUMB_MAX, THUMB_MAX // 2)
    assert len(thumb.content) < len(main.content)


def test_product_payload_exposes_image_urls_only_when_present(admin):
    p = make_product(admin)
    assert p["has_image"] is False and p["image_url"] is None and p["thumb_url"] is None
    upload_image(admin, p["id"], make_image_bytes())
    got = admin.get(f"/api/products/{p['id']}").json()
    assert got["has_image"] and got["thumb_url"].startswith(f"/api/products/{p['id']}/image?size=thumb&v=")
    listed = admin.get("/api/products", params={"page": 1}).json()["items"][0]
    assert listed["thumb_url"] == got["thumb_url"]


def test_wrong_extension_still_judged_by_content(admin):
    p = make_product(admin)
    # امتداد .jpg ومحتوى نص
    assert upload_image(admin, p["id"], b"not an image", "fake.jpg").status_code == 400
    # امتداد .txt ومحتوى PNG حقيقي => مقبول (الحكم للمحتوى)
    assert upload_image(admin, p["id"], make_image_bytes("PNG"), "photo.txt", "text/plain").status_code == 200


def test_rejected_upload_changes_nothing(admin, _media_dir):
    p = make_product(admin)
    assert upload_image(admin, p["id"], make_image_bytes("GIF")).status_code == 400
    assert get_store().list_keys() == []
    assert admin.get(f"/api/products/{p['id']}").json()["has_image"] is False


def test_oversize_upload_rejected(admin):
    p = make_product(admin)
    r = upload_image(admin, p["id"], b"\xff\xd8\xff" + os.urandom(MAX_IMAGE_BYTES + 1024))
    assert r.status_code in (400, 413)


def test_replace_removes_old_files_and_changes_url(admin):
    p = make_product(admin)
    first = upload_image(admin, p["id"], make_image_bytes("JPEG", color=(1, 2, 3))).json()
    second = upload_image(admin, p["id"], make_image_bytes("PNG", color=(9, 9, 9))).json()
    assert first["image_url"] != second["image_url"]
    assert len(get_store().list_keys()) == 1  # القديمة اتمسحت
    assert admin.get(second["image_url"]).status_code == 200


def test_delete_image_is_clean_and_idempotent(admin):
    p = make_product(admin)
    upload_image(admin, p["id"], make_image_bytes())
    r = admin.delete(f"/api/products/{p['id']}/image")
    assert r.status_code == 200 and r.json()["removed"] is True
    assert get_store().list_keys() == []
    assert admin.get(f"/api/products/{p['id']}/image").status_code == 404
    assert admin.delete(f"/api/products/{p['id']}/image").json()["removed"] is False
    assert admin.get(f"/api/products/{p['id']}").json()["has_image"] is False


def test_404s(admin):
    assert upload_image(admin, 99999, make_image_bytes()).status_code == 404
    assert admin.get("/api/products/99999/image").status_code == 404
    assert admin.delete("/api/products/99999/image").status_code == 404
    p = make_product(admin)
    assert admin.get(f"/api/products/{p['id']}/image").status_code == 404  # ملوش صورة
    assert admin.get(f"/api/products/{p['id']}/image", params={"size": "huge"}).status_code == 400


def test_etag_and_cache_headers(admin):
    p = make_product(admin)
    body = upload_image(admin, p["id"], make_image_bytes()).json()
    r = admin.get(body["thumb_url"])  # فيه v=<key> الحالي => كاش طويل
    assert "immutable" in r.headers["cache-control"] and r.headers["cache-control"].startswith("private")
    etag = r.headers["etag"]
    assert admin.get(body["thumb_url"], headers={"If-None-Match": etag}).status_code == 304
    r2 = admin.get(f"/api/products/{p['id']}/image", params={"size": "thumb"})  # من غير v => لازم revalidate
    assert r2.headers["cache-control"] == "private, no-cache" and r2.status_code == 200


def test_missing_file_gives_404_not_500(admin, db):
    p = make_product(admin)
    db.query(models.Product).filter_by(id=p["id"]).update({"image_key": "a" * 32})
    db.commit()
    assert admin.get(f"/api/products/{p['id']}/image").status_code == 404


def test_permissions(admin, login, anon):
    p = make_product(admin)
    body = upload_image(admin, p["id"], make_image_bytes()).json()
    viewer = login("viewer1", ["products.view"])
    assert viewer.get(body["thumb_url"]).status_code == 200  # القراءة بصلاحية عرض المنتجات
    assert upload_image(viewer, p["id"], make_image_bytes()).status_code == 403
    assert viewer.delete(f"/api/products/{p['id']}/image").status_code == 403
    editor = login("editor1", ["products.edit"])
    assert upload_image(editor, p["id"], make_image_bytes()).status_code == 200
    nobody = login("nobody1", ["suppliers.view"])
    assert nobody.get(body["thumb_url"]).status_code == 403
    assert anon.get(body["thumb_url"]).status_code == 401
    assert anon.put(f"/api/products/{p['id']}/image", files={"file": ("a.jpg", b"x", "image/jpeg")}).status_code == 401


def test_audit_log_records_image_changes(admin, db):
    p = make_product(admin)
    upload_image(admin, p["id"], make_image_bytes())
    admin.delete(f"/api/products/{p['id']}/image")
    actions = [a.action for a in db.query(models.AuditLog).filter_by(entity_type="product", entity_id=str(p["id"])).all()]
    assert "image_set" in actions and "image_remove" in actions


# ---------------- سلامة الملفات عند الفشل (خدمة مباشرة) ----------------
def _product(db):
    p = models.Product(name="x", price=1)
    db.add(p)
    db.commit()
    return p


def test_failure_while_writing_files_leaves_nothing_and_keeps_old(db, monkeypatch):
    p = _product(db)
    old = product_images.replace_product_image(db, p, make_image_bytes(), None)
    from app.services import media_store

    real_put, calls = media_store.LocalMediaStore.put, {"n": 0}

    def flaky(self, key, name, data):
        calls["n"] += 1
        if calls["n"] == 2:  # فشل في كتابة الـ thumb
            raise OSError("disk full")
        return real_put(self, key, name, data)

    monkeypatch.setattr(media_store.LocalMediaStore, "put", flaky)
    with pytest.raises(OSError):
        product_images.replace_product_image(db, p, make_image_bytes(color=(5, 5, 5)), None)
    db.refresh(p)
    assert p.image_key == old and get_store().list_keys() == [old]  # القديمة سليمة ومفيش ملف يتيم


def test_failure_on_db_commit_removes_new_files_and_keeps_old(db, monkeypatch):
    p = _product(db)
    old = product_images.replace_product_image(db, p, make_image_bytes(), None)
    with monkeypatch.context() as m:  # context: عشان ما نلغيش MEDIA_DIR بتاع الـ fixture
        m.setattr(db, "commit", lambda: (_ for _ in ()).throw(RuntimeError("db down")))
        with pytest.raises(RuntimeError):
            product_images.replace_product_image(db, p, make_image_bytes(color=(7, 7, 7)), None)
    db.rollback()
    db.refresh(p)
    assert p.image_key == old and get_store().list_keys() == [old]


def test_store_rejects_path_traversal():
    s = get_store()
    for bad in ("../x", "a" * 31, "A" * 32, "", "a" * 32 + "/.."):
        with pytest.raises(ValueError):
            s.put(bad, "main.webp", b"x")
    with pytest.raises(ValueError):
        s.put("a" * 32, "../../etc/passwd", b"x")


# ---------------- أمر الصيانة check-images ----------------
def test_check_images_finds_orphans_and_missing_and_fix_removes_orphans(admin, db):
    from app import cli

    p1, p2 = make_product(admin, "أ"), make_product(admin, "ب")
    upload_image(admin, p1["id"], make_image_bytes())
    upload_image(admin, p2["id"], make_image_bytes("PNG"))
    store = get_store()
    # يتيم: ملفات مش مربوطة بحد
    store.put("b" * 32, "main.webp", b"x")
    store.put("b" * 32, "thumb.webp", b"x")
    # مفقود: منتج بيشاور على ملفات اتمسحت
    key2 = db.query(models.Product).filter_by(id=p2["id"]).one().image_key
    store.delete(key2)
    res = cli.check_images(db)
    assert res["orphans"] == ["b" * 32] and res["missing"] == [(p2["id"], "ب")] and res["removed"] == 0
    assert store.exists("b" * 32, "main.webp")                                  # من غير --fix مفيش مسح
    res = cli.check_images(db, fix=True)
    assert res["removed"] == 1 and not store.exists("b" * 32, "main.webp")
    assert cli.check_images(db)["orphans"] == []


def test_check_images_does_not_treat_staged_batch_images_as_orphans(admin, db):
    from app import cli

    admin.post("/api/product-images/batches", files=[("files", ("x.png", make_image_bytes("PNG"), "image/png"))])
    res = cli.check_images(db)
    assert res["orphans"] == [] and res["on_disk"] == 1

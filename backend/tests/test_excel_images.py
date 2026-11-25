"""استيراد/تصدير المنتجات بالصور من Excel (عايمة + جوه الخلية)، وسيناريوهات التكامل (صور / ملف كبير)."""
import io
import zipfile

import pytest
from PIL import Image

from app import models
from app.services.media_store import get_store
from app.services.xlsx_images import extract_images

from .helpers import make_image_bytes
from .xlsx_fixtures import build_xlsx, png

XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
HEADERS = ["SKU", "الاسم", "الصورة"]


def color_for(i):
    return ((i * 53) % 230 + 10, (i * 97) % 230 + 10, (i * 29) % 230 + 10)


def _heights(n):
    return {r: 60 for r in range(2, n + 2)}


def _post(c, data, **params):
    return c.post("/api/excel/products/import", params=params, files={"file": ("p.xlsx", data, XLSX_MIME)})


def _center(data):
    im = Image.open(io.BytesIO(data)).convert("RGB")
    return im.getpixel((im.width // 2, im.height // 2))


def _close(a, b, tol=14):
    return all(abs(x - y) <= tol for x, y in zip(a, b))


def _product(db, sku):
    db.rollback()  # بينهي الـ transaction: MySQL (REPEATABLE READ) كان هيفضل يشوف لقطة قديمة من قبل commit السيرفر
    return db.query(models.Product).filter_by(sku=sku).one()


def _swap_media(xlsx, replacements):
    out = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(xlsx)) as zin, zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zout:
        for item in zin.infolist():
            zout.writestr(item, replacements.get(item.filename.lstrip("/"), zin.read(item.filename)))
    return out.getvalue()


# ---------------- السيناريو 1: استيراد منتج وصورته ----------------
def test_scenario_import_product_with_image_shows_in_list_and_details(admin, db):
    xlsx = build_xlsx([["IMG-1", "منتج بصورة", ""]], images={"C2": png((220, 20, 20))}, row_heights={2: 60}, headers=HEADERS)
    r = _post(admin, xlsx)
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["created"] == 1 and out["images_saved"] == 1 and out["image_errors_total"] == 0 and out["errors_total"] == 0
    assert "ربط 1 صورة" in out["message"]
    p = _product(db, "IMG-1")
    assert p.image_key and len(get_store().list_keys()) == 1                       # الصورة الأساسية اتخزنت
    assert get_store().exists(p.image_key, "main.webp") and get_store().exists(p.image_key, "thumb.webp")   # والـ thumbnail اتعمل
    listed = next(x for x in admin.get("/api/products", params={"search": "IMG-1", "page": 1}).json()["items"])
    assert listed["thumb_url"]                                                     # ظهرت في جدول المنتجات
    thumb = admin.get(listed["thumb_url"])
    assert thumb.status_code == 200 and max(Image.open(io.BytesIO(thumb.content)).size) <= 256 and _close(_center(thumb.content), (220, 20, 20))
    detail = admin.get(f"/api/products/{p.id}").json()                             # وصفحة التفاصيل
    assert detail["image_url"] and _close(_center(admin.get(detail["image_url"]).content), (220, 20, 20))


@pytest.mark.parametrize("mode", ["floating", "in_cell", "mixed"])
def test_each_image_goes_to_the_right_product(admin, db, mode):
    n = 12
    rows = [[f"S{i}", f"منتج {i}", ""] for i in range(1, n + 1)]
    imgs = {f"C{i + 1}": png(color_for(i)) for i in range(1, n + 1)}
    if mode == "floating":
        xlsx = build_xlsx(rows, images=dict(reversed(list(imgs.items()))), row_heights=_heights(n), headers=HEADERS)   # ترتيب إضافة معكوس
    elif mode == "in_cell":
        xlsx = build_xlsx(rows, in_cell=imgs, headers=HEADERS)
    else:
        items = list(imgs.items())
        xlsx = build_xlsx(rows, images=dict(items[::2]), in_cell=dict(items[1::2]), row_heights=_heights(n), headers=HEADERS)
    out = _post(admin, xlsx).json()
    assert out["created"] == n and out["images_saved"] == n and out["image_errors_total"] == 0, out
    for i in range(1, n + 1):
        p = _product(db, f"S{i}")
        data = admin.get(f"/api/products/{p.id}/image").content
        assert _close(_center(data), color_for(i)), f"S{i} اتربط بصورة غلط"


def test_products_without_images_and_old_files_without_any_images(admin, db):
    rows = [["A", "بدون صورة", ""], ["B", "بصورة", ""], ["C", "بدون صورة 2", ""]]
    out = _post(admin, build_xlsx(rows, images={"C3": png((1, 200, 1))}, row_heights=_heights(3), headers=HEADERS)).json()
    assert out["created"] == 3 and out["images_saved"] == 1
    assert [bool(_product(db, s).image_key) for s in "ABC"] == [False, True, False]
    # ملف قديم من غير أي صور ولا عمود صورة: شغال زي الأول
    old = _post(admin, build_xlsx([["D", "قديم", ""]], headers=["SKU", "الاسم", "ملاحظة"])).json()
    assert old["created"] == 1 and old["images_saved"] == 0 and old["image_errors_total"] == 0 and old["images_found"] == 0


# ---------------- حالات الخطأ: صورة واحدة مش بتوقع الباقي ----------------
def test_corrupt_image_is_reported_with_row_and_other_products_are_saved(admin, db):
    rows = [[f"K{i}", f"سليم {i}", ""] for i in range(1, 4)]
    imgs = {f"C{i + 1}": png(color_for(i)) for i in range(1, 4)}
    xlsx = build_xlsx(rows, images=imgs, row_heights=_heights(3), headers=HEADERS)
    xlsx = _swap_media(xlsx, {"xl/media/image2.png": b"\x89PNG\r\n\x1a\n" + b"garbage" * 20})     # الصورة التانية (صف 3) تالفة
    out = _post(admin, xlsx).json()
    assert out["created"] == 3 and out["images_saved"] == 2 and out["image_errors_total"] == 1
    err = out["image_errors"][0]
    assert err["row"] == 3 and "سليم 2" in err["message"] and "تالفة" in err["message"] and err["column"] == "الصورة"
    assert [bool(_product(db, f"K{i}").image_key) for i in (1, 2, 3)] == [True, False, True]


def test_unsupported_format_gif_is_reported(admin, db):
    gif = make_image_bytes("GIF", (30, 30))
    xlsx = build_xlsx([["G1", "جيف", ""], ["G2", "عادي", ""]], images={"C2": gif, "C3": png((9, 99, 9))}, row_heights=_heights(2), headers=HEADERS)
    out = _post(admin, xlsx).json()
    assert out["created"] == 2 and out["images_saved"] == 1
    assert out["image_errors"][0]["row"] == 2 and "مش مدعومة" in out["image_errors"][0]["message"]


def test_oversized_image_is_reported(admin, monkeypatch):
    from app.services import images, xlsx_images

    monkeypatch.setattr(images, "MAX_IMAGE_BYTES", 400)
    monkeypatch.setattr(xlsx_images, "MAX_IMAGE_BYTES", 400)
    big = make_image_bytes("PNG", (200, 200), (5, 5, 5)) + b"\0" * 0
    import os
    noisy = Image.frombytes("RGB", (64, 64), os.urandom(64 * 64 * 3))
    buf = io.BytesIO()
    noisy.save(buf, "PNG")
    xlsx = build_xlsx([["B1", "كبيرة", ""]], images={"C2": buf.getvalue()}, row_heights={2: 60}, headers=HEADERS)
    out = _post(admin, xlsx).json()
    assert out["created"] == 1 and out["images_saved"] == 0 and out["image_errors"][0]["row"] == 2 and "كبيرة" in out["image_errors"][0]["message"]
    _ = big


def test_image_in_wrong_column_is_not_linked_when_image_column_exists(admin, db):
    xlsx = build_xlsx([["W1", "عمود غلط", ""], ["W2", "صح", ""]], images={"B2": png((200, 0, 0)), "C3": png((0, 200, 0))}, row_heights=_heights(2), headers=HEADERS)
    out = _post(admin, xlsx).json()
    assert out["images_saved"] == 1 and out["image_errors_total"] == 1
    assert out["image_errors"][0]["row"] == 2 and "B2" in out["image_errors"][0]["message"] and "عمود" in out["image_errors"][0]["message"]
    assert not _product(db, "W1").image_key and _product(db, "W2").image_key


def test_without_image_column_header_images_link_by_row(admin, db):
    xlsx = build_xlsx([["N1", "اسم", "x"]], images={"C2": png((0, 0, 200))}, row_heights={2: 60}, headers=["SKU", "الاسم", "ملاحظة"])
    assert _post(admin, xlsx).json()["images_saved"] == 1 and _product(db, "N1").image_key


def test_rejected_row_image_is_dropped_with_clear_message(admin, db):
    xlsx = build_xlsx([["R1", "", ""], ["R2", "سليم", ""]], images={"C2": png((9, 9, 200)), "C3": png((9, 200, 9))}, row_heights=_heights(2), headers=HEADERS)
    out = _post(admin, xlsx).json()
    assert out["created"] == 1 and out["errors_total"] == 1 and out["images_saved"] == 1
    assert out["image_errors"][0]["row"] == 2 and "مرفوضة" in out["image_errors"][0]["message"]


def test_image_on_empty_row_is_reported(admin):
    xlsx = build_xlsx([["E1", "واحد", ""], ["", "", ""], ["", "", ""]][:1], images={"C2": png((1, 1, 200)), "C6": png((1, 200, 1))}, row_heights={2: 60, 6: 60}, headers=HEADERS)
    out = _post(admin, xlsx).json()
    assert out["images_saved"] == 1 and any(e["row"] == 6 and "من غير بيانات" in e["message"] for e in out["image_errors"])


def test_existing_product_with_image_is_skipped_unless_replace(admin, db):
    first = _post(admin, build_xlsx([["X1", "منتج", ""]], images={"C2": png((200, 10, 10))}, row_heights={2: 60}, headers=HEADERS)).json()
    key1 = _product(db, "X1").image_key
    second = _post(admin, build_xlsx([["X1", "منتج", ""]], images={"C2": png((10, 10, 200))}, row_heights={2: 60}, headers=HEADERS)).json()
    assert first["images_saved"] == 1 and second["images_saved"] == 0 and second["images_skipped_existing"] == 1 and second["updated"] == 1
    assert _product(db, "X1").image_key == key1
    third = _post(admin, build_xlsx([["X1", "منتج", ""]], images={"C2": png((10, 10, 200))}, row_heights={2: 60}, headers=HEADERS), replace_images=1).json()
    key3 = _product(db, "X1").image_key
    assert third["images_saved"] == 1 and key3 != key1 and get_store().list_keys() == [key3]       # القديمة اتمسحت
    assert _close(_center(admin.get(f"/api/products/{_product(db, 'X1').id}/image").content), (10, 10, 200))


def test_same_product_on_two_rows_with_same_or_different_images(admin, db):
    same = png((100, 100, 10))
    rows = [["M1", "متعدد", ""], ["M1", "متعدد", ""]]
    out = _post(admin, build_xlsx(rows, images={"C2": same, "C3": same}, row_heights=_heights(2), headers=HEADERS)).json()
    assert out["created"] == 1 and out["images_saved"] == 1 and out["image_errors_total"] == 0          # نفس الصورة مكررة = عادي
    rows2 = [["M2", "متعدد2", ""], ["M2", "متعدد2", ""]]
    out2 = _post(admin, build_xlsx(rows2, images={"C2": png((1, 1, 1)), "C3": png((250, 250, 250))}, row_heights=_heights(2), headers=HEADERS)).json()
    assert out2["images_saved"] == 1 and out2["image_errors_total"] == 1 and "زيادة" in out2["image_errors"][0]["message"]


def test_storage_failure_for_one_image_does_not_lose_products(admin, db, monkeypatch):
    from app.services import media_store

    real_put = media_store.LocalMediaStore.put

    def flaky(self, key, name, data):
        if getattr(flaky, "armed", True) and not getattr(flaky, "fired", False):
            flaky.fired = True
            raise OSError("disk full")
        return real_put(self, key, name, data)

    monkeypatch.setattr(media_store.LocalMediaStore, "put", flaky)
    rows = [[f"F{i}", f"منتج {i}", ""] for i in range(1, 4)]
    xlsx = build_xlsx(rows, images={f"C{i + 1}": png(color_for(i)) for i in range(1, 4)}, row_heights=_heights(3), headers=HEADERS)
    out = _post(admin, xlsx).json()
    assert out["created"] == 3 and out["images_saved"] == 2 and out["image_errors_total"] == 1 and "تعذّر حفظ الصورة" in out["image_errors"][0]["message"]
    assert len(get_store().list_keys()) == 2                                   # مفيش ملفات يتيمة من المحاولة اللي فشلت


# ---------------- السيناريو 4: ملف كبير ----------------
def test_scenario_big_file_every_image_linked_to_its_product(admin, db):
    n = 150
    rows = [[f"BIG{i}", f"كبير {i}", ""] for i in range(1, n + 1)]
    imgs = {f"C{i + 1}": png(color_for(i), (24, 24)) for i in range(1, n + 1)}
    xlsx = build_xlsx(rows, images=imgs, row_heights=_heights(n), headers=HEADERS)
    out = _post(admin, xlsx).json()
    assert out["created"] == n and out["images_saved"] == n and out["image_errors_total"] == 0, out
    assert len(get_store().list_keys()) == n
    for i in range(1, n + 1, 7):                         # عينة موزعة (الكل اتفحص بالفعل في اختبار mode فوق)
        data = admin.get(f"/api/products/{_product(db, f'BIG{i}').id}/image").content
        assert _close(_center(data), color_for(i)), f"BIG{i}"


# ---------------- التصدير ----------------
def _export(c, **params):
    r = c.get("/api/excel/products/export", params=params)
    assert r.status_code == 200
    return r


def test_export_embeds_primary_image_next_to_product_row_and_roundtrips(admin, db):
    from openpyxl import load_workbook

    xlsx = build_xlsx([["EX1", "أول", ""], ["EX2", "تاني", ""], ["EX3", "تالت", ""]], images={"C2": png((220, 30, 30)), "C4": png((30, 30, 220))},
                      row_heights=_heights(3), headers=HEADERS)
    assert _post(admin, xlsx).json()["images_saved"] == 2
    r = _export(admin)
    assert r.headers["x-images-included"] == "2" and r.headers["x-images-omitted"] == "0"
    wb = load_workbook(io.BytesIO(r.content))                                   # الملف بيفتح سليم
    ws = wb["المنتجات"]
    header = [c.value for c in ws[1]]
    assert header[-1] == "الصورة" and len(ws._images) == 2
    sku_col = header.index("SKU") + 1
    by_row = extract_images(r.content, image_col=len(header), first_data_row=2).by_row
    skus = {row: ws.cell(row=row, column=sku_col).value for row in by_row}
    assert sorted(skus.values()) == ["EX1", "EX3"]                               # الصورة جنب صف المنتج الصح
    for row, imgs in by_row.items():
        want = (220, 30, 30) if skus[row] == "EX1" else (30, 30, 220)
        data = _export_img = imgs[0]
        assert _close(_center(extract_images(r.content, image_col=len(header)).read(data)), want, tol=25)
    # إعادة استيراد الملف المصدّر: مفيش تغيير ولا أخطاء (المنتجات عندها صور فبتتخطى)
    again = _post(admin, r.content).json()
    assert again["errors_total"] == 0 and again["image_errors_total"] == 0 and again["images_saved"] == 0 and again["images_skipped_existing"] == 2


def test_export_without_images_or_without_any_image(admin):
    admin.post("/api/products", json={"name": "من غير صورة", "price": 1})
    r = _export(admin)
    assert r.headers["x-images-included"] == "0"
    from openpyxl import load_workbook
    assert len(load_workbook(io.BytesIO(r.content))["المنتجات"]._images) == 0
    r2 = _export(admin, with_images=0)
    assert "الصورة" not in [c.value for c in load_workbook(io.BytesIO(r2.content))["المنتجات"][1]]


def test_export_multi_location_product_has_one_image_on_first_row(admin, db):
    from openpyxl import load_workbook
    from .helpers import make_location, make_product, make_warehouse, upload_image

    w = make_warehouse(admin)
    a, b = make_location(admin, warehouse_id=w["id"], code="A1"), make_location(admin, warehouse_id=w["id"], code="A2")
    p = make_product(admin, "متعدد المواقع", locations=[(a["id"], 3), (b["id"], 4)])
    upload_image(admin, p["id"], make_image_bytes("PNG", (200, 100), (10, 200, 10)))
    ws = load_workbook(io.BytesIO(_export(admin).content))["المنتجات"]
    data_rows = [r for r in range(2, ws.max_row + 1) if ws.cell(row=r, column=4).value]            # الصفوف اللي فيها اسم منتج (الشيت فيه صفوف منسّقة فاضية)
    assert data_rows == [2, 3] and len(ws._images) == 1 and ws._images[0].anchor._from.row == 1   # صورة واحدة على الصف الأول (0-based = 1)


def test_export_limit_reports_omitted(admin, db, monkeypatch):
    from app.routers import excel_io

    monkeypatch.setattr(excel_io, "MAX_EXPORT_IMAGES", 1)
    rows = [[f"L{i}", f"م{i}", ""] for i in (1, 2)]
    _post(admin, build_xlsx(rows, images={"C2": png((9, 9, 9)), "C3": png((200, 9, 9))}, row_heights=_heights(2), headers=HEADERS))
    r = _export(admin)
    assert (r.headers["x-images-included"], r.headers["x-images-omitted"]) == ("1", "1")


def test_template_has_image_column_and_instructions(admin):
    from openpyxl import load_workbook
    wb = load_workbook(io.BytesIO(admin.get("/api/excel/products/template").content))
    assert wb["المنتجات"][1][-1].value == "الصورة"
    assert any("الصورة" in str(r[0].value) for r in wb["تعليمات"].iter_rows(min_row=2))


def test_file_with_more_than_200_images_is_accepted(admin, db):
    """كل صورة = ملف في الـ zip: الحد القديم (200 ملف) كان بيرفض أي ملف فيه ~190 صورة."""
    n = 260
    rows = [[f"MANY{i}", f"م{i}", ""] for i in range(1, n + 1)]
    imgs = {f"C{i + 1}": png(((i * 7) % 256, (i // 256) * 40 % 256, (i * 13) % 256), (6, 6)) for i in range(1, n + 1)}
    xlsx = build_xlsx(rows, images=imgs, row_heights=_heights(n), headers=HEADERS)
    assert len(zipfile.ZipFile(io.BytesIO(xlsx)).infolist()) > 200
    out = _post(admin, xlsx)
    assert out.status_code == 200, out.text
    assert out.json()["created"] == n and out.json()["images_saved"] == n

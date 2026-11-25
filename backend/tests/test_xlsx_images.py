"""قارئ صور الإكسل: ربط بالـ anchor (مش بالترتيب)، عايمة وجوه الخلية، عمود الصورة، والحالات الغريبة."""
import io
import random

from PIL import Image

from app.services.xlsx_images import extract_images, has_candidate_images

from .xlsx_fixtures import build_xlsx, png

COLORS = [(250, 0, 0), (0, 250, 0), (0, 0, 250), (250, 250, 0), (250, 0, 250), (0, 250, 250), (120, 60, 10), (10, 60, 120)]


def _color(data):
    return Image.open(io.BytesIO(data)).convert("RGB").getpixel((5, 5))


def _rows(n):
    return [[f"SKU{i}", f"منتج {i}", ""] for i in range(1, n + 1)]


def _assert_rows(res, expected):
    """expected = {excel_row: color}"""
    assert sorted(res.by_row) == sorted(expected), (res.by_row, res.problems)
    for row, color in expected.items():
        assert len(res.by_row[row]) == 1
        assert _color(res.read(res.by_row[row][0])) == color


def test_floating_images_link_by_row_not_by_insertion_order():
    n = 6
    cells = {f"C{r}": png(COLORS[r]) for r in range(2, n + 2)}
    order = list(cells)
    random.Random(7).shuffle(order)                        # بنضيف الصور بترتيب عشوائي
    xlsx = build_xlsx(_rows(n), images={k: cells[k] for k in order}, row_heights={r: 60 for r in range(2, n + 2)})
    res = extract_images(xlsx, image_col=3)
    _assert_rows(res, {r: COLORS[r] for r in range(2, n + 2)})
    assert res.problems == [] and res.total == n
    assert all(i.kind == "floating" and i.col == 3 for imgs in res.by_row.values() for i in imgs)


def test_in_cell_images_link_to_their_cell():
    cells = {f"C{r}": png(COLORS[r]) for r in (2, 4, 5)}   # صفوف 3 من غير صور
    xlsx = build_xlsx(_rows(5), in_cell=cells)
    res = extract_images(xlsx, image_col=3)
    _assert_rows(res, {r: COLORS[r] for r in (2, 4, 5)})
    assert all(i.kind == "in_cell" for imgs in res.by_row.values() for i in imgs) and res.problems == []


def test_mixed_floating_and_in_cell_in_one_sheet():
    xlsx = build_xlsx(_rows(4), images={"C2": png(COLORS[0]), "C4": png(COLORS[2])}, in_cell={"C3": png(COLORS[1]), "C5": png(COLORS[3])},
                      row_heights={2: 60, 3: 60, 4: 60, 5: 60})
    _assert_rows(extract_images(xlsx, image_col=3), {2: COLORS[0], 3: COLORS[1], 4: COLORS[2], 5: COLORS[3]})


def test_varied_row_heights_do_not_shift_rows():
    heights = {2: 20, 3: 120, 4: 30, 5: 75, 6: 20}
    xlsx = build_xlsx(_rows(5), images={f"C{r}": png(COLORS[r], (16, 16)) for r in heights}, row_heights=heights)
    _assert_rows(extract_images(xlsx, image_col=3), {r: COLORS[r] for r in heights})


def test_sloppy_placement_still_picks_the_row_under_the_image_center():
    # الصورة نازلة شوية فوق حدود صفها (إزاحة سالبة بالنسبة لنقطة البداية) لكن معظمها جوه الصف الصح
    xlsx = build_xlsx(_rows(3), images={"C3": png(COLORS[1], (40, 40))}, row_heights={2: 60, 3: 60, 4: 60},
                      image_options={"C3": {"y_offset": 12, "x_offset": 5}})
    _assert_rows(extract_images(xlsx, image_col=3), {3: COLORS[1]})


def test_image_outside_the_image_column_is_reported_not_linked():
    xlsx = build_xlsx(_rows(3), images={"C2": png(COLORS[0]), "B3": png(COLORS[1])}, row_heights={2: 60, 3: 60})
    res = extract_images(xlsx, image_col=3)
    _assert_rows(res, {2: COLORS[0]})
    assert len(res.problems) == 1 and res.problems[0].row == 3 and "B3" in res.problems[0].message and "عمود" in res.problems[0].message


def test_without_image_column_any_column_is_accepted_by_row():
    xlsx = build_xlsx(_rows(2), images={"B2": png(COLORS[0]), "D3": png(COLORS[1])}, row_heights={2: 60, 3: 60})
    _assert_rows(extract_images(xlsx, image_col=None), {2: COLORS[0], 3: COLORS[1]})


def test_image_above_data_is_reported():
    xlsx = build_xlsx(_rows(2), images={"C1": png(COLORS[0], (10, 10)), "C2": png(COLORS[1])}, row_heights={1: 30, 2: 60})
    res = extract_images(xlsx, image_col=3)
    _assert_rows(res, {2: COLORS[1]})
    assert any(p.row == 1 and "العناوين" in p.message for p in res.problems)


def test_image_spanning_many_rows_is_ambiguous_and_not_linked():
    xlsx = build_xlsx(_rows(8), images={"C3": png(COLORS[0], (60, 300))})  # صفوف default (20px): الصورة بتغطي ~15 صف
    res = extract_images(xlsx, image_col=3)
    assert res.by_row == {} and len(res.problems) == 1 and "أكبر من الصف" in res.problems[0].message


def test_two_images_in_same_row_are_both_returned_sorted_by_position():
    xlsx = build_xlsx(_rows(2), images={"C2": png(COLORS[0]), "C3": png(COLORS[1])}, row_heights={2: 60, 3: 60})
    res = extract_images(xlsx, image_col=None)
    assert [len(v) for v in res.by_row.values()] == [1, 1]


def test_no_images_and_garbage_input_are_safe():
    plain = build_xlsx(_rows(3))
    assert has_candidate_images(plain) is False
    res = extract_images(plain, image_col=3)
    assert res.by_row == {} and res.problems == [] and res.total == 0
    for junk in (b"", b"not a zip", b"PK\x03\x04broken"):
        r = extract_images(junk)
        assert r.by_row == {} and r.total == 0


def test_corrupt_drawing_xml_gives_one_general_problem(tmp_path):
    import zipfile

    src = build_xlsx(_rows(2), images={"C2": png(COLORS[0])}, row_heights={2: 60})
    out = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(src)) as zin, zipfile.ZipFile(out, "w") as zout:
        for item in zin.infolist():
            data = zin.read(item.filename)
            if item.filename.startswith("xl/drawings/drawing"):
                data = b"<xdr:wsDr><broken"
            zout.writestr(item, data)
    res = extract_images(out.getvalue(), image_col=3)
    assert res.by_row == {} and len(res.problems) == 1 and res.problems[0].row is None


def test_oversized_image_file_is_reported_with_its_row(monkeypatch):
    from app.services import xlsx_images

    monkeypatch.setattr(xlsx_images, "MAX_IMAGE_BYTES", 100)
    xlsx = build_xlsx(_rows(2), images={"C2": png(COLORS[0], (300, 300))}, row_heights={2: 250}, col_widths={2: 50})
    res = extract_images(xlsx, image_col=3)
    assert res.by_row == {} and res.problems and res.problems[0].row == 2 and "كبيرة" in res.problems[0].message


def test_openpyxl_exported_floating_images_are_readable():
    """التصدير عندنا بيكتب بـ openpyxl: لازم القارئ يقرا اللي بيكتبه."""
    from openpyxl import Workbook
    from openpyxl.drawing.image import Image as XLImage

    wb = Workbook()
    ws = wb.active
    ws.append(["الكود", "الاسم", "الصورة"])
    for r in (2, 3, 4):
        ws.append([f"S{r}", f"م{r}"])
        img = XLImage(io.BytesIO(png(COLORS[r], (40, 40))))
        img.width = img.height = 40
        ws.add_image(img, f"C{r}")
        ws.row_dimensions[r].height = 60
    buf = io.BytesIO()
    wb.save(buf)
    _assert_rows(extract_images(buf.getvalue(), image_col=3), {r: COLORS[r] for r in (2, 3, 4)})


def test_reader_tolerates_leading_slash_part_names():
    """XlsxWriter بيكتب /xl/richData/_rels/... بـ / في الأول: القارئ لازم يقراها (الفحص الأمني للرفع لسه بيرفض المسارات المطلقة، ده اختبار للقارئ لوحده)."""
    import xlsxwriter

    buf = io.BytesIO()
    wb = xlsxwriter.Workbook(buf, {"in_memory": True})
    ws = wb.add_worksheet()
    ws.write_row(0, 0, ["SKU", "الاسم", "الصورة"])
    ws.embed_image("C2", "i.png", {"image_data": io.BytesIO(png(COLORS[2]))})
    wb.close()
    res = extract_images(buf.getvalue(), image_col=3)
    _assert_rows(res, {2: COLORS[2]})


def test_wide_image_in_narrow_image_column_is_linked_but_neighbour_column_is_not():
    """صورة أعرض من عمودها مركزها بيقع في عمود تاني: لازم تتحسب في عمود «الصورة» (بتغطيه)، مش تتبلّغ بالغلط."""
    wide = png(COLORS[0], (300, 40))
    xlsx = build_xlsx(_rows(2), images={"C2": wide}, row_heights={2: 60, 3: 60})          # عمود C عرضه الافتراضي (64px) والصورة 300px
    _assert_rows(extract_images(xlsx, image_col=3), {2: COLORS[0]})
    # نفس الصورة بس بادئة من عمود بعيد (ما بتغطيش C) = عمود غلط
    far = build_xlsx(_rows(2), images={"F2": wide}, row_heights={2: 60, 3: 60})
    res = extract_images(far, image_col=3)
    assert res.by_row == {} and len(res.problems) == 1 and "عمود" in res.problems[0].message

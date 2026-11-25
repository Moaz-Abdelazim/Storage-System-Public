"""بناء ملفات .xlsx اختبارية بصور حقيقية (XlsxWriter بيكتب الصور جوه الخلية بنفس صيغة Excel 365)."""
import io
import zipfile

import xlsxwriter

from .helpers import make_image_bytes

HEADERS = ["الكود", "اسم المنتج", "الصورة"]


def png(color, size=(40, 40)):
    return make_image_bytes("PNG", size, color)


def build_xlsx(rows, images=None, in_cell=None, headers=HEADERS, row_heights=None, image_options=None, col_widths=None):
    """
    rows: [[sku, name, ""], ...] تبدأ من الصف 2. images: {"C2": bytes} عايمة. in_cell: {"C3": bytes} جوه الخلية.
    row_heights: {excel_row: points}. image_options: {"C2": {...xlsxwriter options}}.
    """
    buf = io.BytesIO()
    wb = xlsxwriter.Workbook(buf, {"in_memory": True})
    ws = wb.add_worksheet("Sheet1")
    ws.write_row(0, 0, headers)
    for i, row in enumerate(rows, start=1):
        ws.write_row(i, 0, row)
    for r, h in (row_heights or {}).items():
        ws.set_row(r - 1, h)
    for c, w in (col_widths or {}).items():
        ws.set_column(c, c, w)
    for cell, data in (images or {}).items():
        opts = {"image_data": io.BytesIO(data), "object_position": 1}
        opts.update((image_options or {}).get(cell, {}))
        ws.insert_image(cell, "img.png", opts)
    for cell, data in (in_cell or {}).items():
        ws.embed_image(cell, "img.png", {"image_data": io.BytesIO(data)})
    wb.close()
    return _like_excel(buf.getvalue())


def _like_excel(data: bytes) -> bytes:
    """XlsxWriter بيكتب بعض أسماء الأجزاء بـ / في الأول (/xl/richData/...) وExcel الحقيقي لأ: بنشيلها عشان الملف يطابق ملف Excel فعلي."""
    out = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(data)) as zin, zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zout:
        for item in zin.infolist():
            zout.writestr(item.filename.lstrip("/"), zin.read(item.filename))
    return out.getvalue()

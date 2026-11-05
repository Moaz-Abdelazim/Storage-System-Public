import io
import logging
import re
from datetime import datetime
from typing import Optional

import pandas as pd
from fastapi import APIRouter, Depends, HTTPException, UploadFile, File
from fastapi.responses import StreamingResponse
from sqlalchemy import func
from sqlalchemy.orm import Session, joinedload

from .. import models, auth, permissions
from ..query_helpers import end_exclusive, join_valid, parse_dt
from ..display_utils import format_user_display
from ..database import get_db
from ..location_utils import LocationIndex, lock_products
from ..timezone_utils import now_cairo
from ..barcode import barcode_conflict, normalize_excel_barcode, sku_conflicts_with_barcode
from ..money import q3
from ..rate_limit import limit_per_user
from ..services import inventory
from ..services.audit import audit

_log = logging.getLogger("app.excel")
MAX_EXPORT_ROWS = 100_000  # حد أقصى لعدد صفوف كل شيت في تصدير الحركات (حماية الذاكرة)

router = APIRouter(prefix="/api/excel", tags=["Excel Import/Export"])

MAX_UPLOAD_BYTES = 10 * 1024 * 1024  # 10MB
MAX_IMPORT_ROWS = 20000
MAX_UNCOMPRESSED_BYTES = 80 * 1024 * 1024  # حماية من zip bomb: xlsx ملف zip، حجمه بعد الفك لازم يفضل معقول
MAX_ZIP_ENTRIES = 200


def _validate_xlsx_container(content: bytes) -> None:
    """فحص بنية الـ zip قبل ما pandas/openpyxl يفكّوه: تضخّم الحجم، عدد الملفات، ومسارات خطرة."""
    import zipfile

    try:
        with zipfile.ZipFile(io.BytesIO(content)) as z:
            infos = z.infolist()
            if len(infos) > MAX_ZIP_ENTRIES or sum(i.file_size for i in infos) > MAX_UNCOMPRESSED_BYTES:
                raise HTTPException(status_code=400, detail="الملف كبير أو غير طبيعي - اتأكد إنه ملف Excel سليم")
            if any(i.filename.startswith(("/", "..")) or ".." in i.filename.split("/") for i in infos):
                raise HTTPException(status_code=400, detail="الملف غير صالح")
            if "[Content_Types].xml" not in {i.filename for i in infos}:
                raise HTTPException(status_code=400, detail="الملف مش Excel (.xlsx) سليم")
    except zipfile.BadZipFile:
        raise HTTPException(status_code=400, detail="الملف مش Excel (.xlsx) سليم")


def _safe_cell(value):
    """
    حماية من Formula/CSV Injection: أي نص بيبدأ بـ = + @ (أو - متبوع بغير رقم) ممكن يتنفذ كمعادلة لما
    الملف يتفتح في Excel/Sheets، فبنحط قبله ' عشان يتعامل معاه كنص عادي.
    """
    if isinstance(value, str) and value:
        first = value[0]
        if first in "=+@\t\r" or (first == "-" and len(value) > 1 and not value[1].isdigit()):
            return "'" + value
    return value


def _safe_df(df):
    out = df.copy()
    for col in out.columns:
        if out[col].dtype == object:
            out[col] = out[col].map(_safe_cell)
    return out

# أعمدة شيت المنتجات - نفس الترتيب في الاستيراد والتصدير
# عمود "ID" هو المعرّف الداخلي للمنتج - بيترجع معاه في التصدير عشان لو استوردت
# نفس الملف تاني، النظام يعرف يحدّث نفس المنتج بدل ما يعمل نسخة مكررة منه
#
# عمود "الموقع" = الـ Full Location Code (مثلاً W1-F3-R5). لو المنتج في أكتر من مكان
# بيتكتب له صف لكل مكان (نفس المنتج، موقع مختلف، وكمية المكان ده). كل كمية لازم يتحدد لها مكان - لو
# كتبت كود مخزن بس (مثلاً W1) الكمية بتتحط في مكان الاستلام الافتراضي للمخزن ده (لو متحدد).
# صف من غير موقع وكميته صفر/فاضية = تحديث بيانات المنتج بس من غير ما نلمس المخزون.
# الاستيراد ما بينشئش مواقع جديدة أبدًا - المواقع لازم تتعرّف الأول من صفحة "إدارة المواقع".
#
# عمود "الحالة" = مفعّل / غير مفعّل. لو العمود مش موجود في الملف أو الخلية فاضية: المنتج الجديد بيتعمل
# مفعّل، والمنتج الموجود حالته ما بتتغيّرش.
PRODUCT_COLUMNS = ["ID", "SKU", "الباركود", "الاسم", "التصنيف", "الماركة", "الوحدة", "السعر", "الموقع", "الكمية", "حد الطلب الأدنى", "الحالة"]

# أسماء بديلة مقبولة للأعمدة (إنجليزي/عربي) - بتتحوّل للأسماء الأساسية قبل القراءة
_COLUMN_ALIASES = {
    "id": "ID", "sku": "SKU", "product code": "SKU", "كود المنتج": "SKU",
    "barcode": "الباركود", "bar code": "الباركود", "الباركود": "الباركود", "باركود": "الباركود", "كود الباركود": "الباركود",
    "product name": "الاسم", "name": "الاسم", "اسم المنتج": "الاسم", "الاسم": "الاسم",
    "category": "التصنيف", "التصنيف": "التصنيف", "brand": "الماركة", "الماركة": "الماركة",
    "unit": "الوحدة", "الوحدة": "الوحدة", "price": "السعر", "السعر": "السعر",
    "location": "الموقع", "الموقع": "الموقع", "كود الموقع": "الموقع", "location code": "الموقع",
    "quantity": "الكمية", "qty": "الكمية", "الكمية": "الكمية",
    "min quantity": "حد الطلب الأدنى", "حد الطلب الأدنى": "حد الطلب الأدنى",
    "status": "الحالة", "active": "الحالة", "is_active": "الحالة", "الحالة": "الحالة",
}

_ACTIVE_YES = {"مفعل", "فعال", "نشط", "active", "enabled", "yes", "y", "true", "1", "نعم"}
_ACTIVE_NO = {"غير مفعل", "غير فعال", "غير نشط", "معطل", "موقوف", "inactive", "disabled", "no", "n", "false", "0", "لا"}


def _parse_active(value):
    """True = مفعّل، False = غير مفعّل، None = الخلية فاضية. بترمي ValueError لو القيمة مش مفهومة."""
    if pd.isna(value):
        return None
    text = re.sub(r"[\u064B-\u0652\u0640]", "", str(value))  # شيل التشكيل والتطويل (مفعّل -> مفعل)
    text = " ".join(text.lower().split())
    if not text or text == "nan":
        return None
    if text in _ACTIVE_YES:
        return True
    if text in _ACTIVE_NO:
        return False
    raise ValueError(value)


def _sheet_bytes(df, instructions=None):
    buffer = io.BytesIO()
    with pd.ExcelWriter(buffer, engine="openpyxl") as writer:
        df.pipe(_safe_df).to_excel(writer, index=False, sheet_name="المنتجات")
        if "الباركود" in df.columns:
            # عمود الباركود نص: Excel مايشيلش الأصفار من الأول ومايحوّلهوش لصيغة علمية لما المستخدم يكتب/يعدّل
            ws = writer.sheets["المنتجات"]
            col = list(df.columns).index("الباركود") + 1
            for (cell,) in ws.iter_rows(min_row=2, max_row=max(ws.max_row, 1000), min_col=col, max_col=col):
                cell.number_format = "@"  # شامل الصفوف الفاضية عشان الصفوف اللي هيضيفها المستخدم تبقى نص برضه
        if instructions:
            pd.DataFrame({"تعليمات": instructions}).pipe(_safe_df).to_excel(writer, index=False, sheet_name="تعليمات")
    buffer.seek(0)
    return buffer


@router.get("/products/export")
def export_products(db: Session = Depends(get_db), current_user: models.User = Depends(auth.require_permission("products.export"))):
    idx = LocationIndex(db)
    products = db.query(models.Product).order_by(models.Product.name).all()
    data = []
    for p in products:
        base = {
            "ID": p.id, "SKU": p.sku, "الباركود": p.barcode, "الاسم": p.name,
            "التصنيف": p.category.name if p.category else None,
            "الماركة": p.brand.name if p.brand else None,
            "الوحدة": p.unit, "السعر": p.price, "حد الطلب الأدنى": p.min_quantity,
            "الحالة": "مفعّل" if p.is_active else "غير مفعّل",
        }
        locs = sorted((pl for pl in p.locations if pl.quantity > 0), key=lambda pl: idx.full_code(pl.location_id))
        # صف لكل مكان تخزين. منتج من غير مخزون بيطلع صف واحد بكمية 0 وموقع فاضي (وإعادة استيراده ما بتغيّرش حاجة)
        for pl in locs:
            data.append({**base, "الموقع": idx.full_code(pl.location_id), "الكمية": pl.quantity})
        if not locs:
            data.append({**base, "الموقع": None, "الكمية": 0})
    df = pd.DataFrame(data, columns=PRODUCT_COLUMNS)
    buffer = _sheet_bytes(df)

    filename = f"products_export_{datetime.now().strftime('%Y%m%d_%H%M')}.xlsx"
    return StreamingResponse(
        buffer,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )


@router.get("/products/template")
def download_template(_user: models.User = Depends(auth.require_permission("products.import"))):
    """شيت فاضي بنفس الأعمدة عشان تعرف تملأه صح (+ شيت تعليمات للمواقع)."""
    df = pd.DataFrame(columns=PRODUCT_COLUMNS)
    buffer = _sheet_bytes(df, instructions=[
        "عمود الموقع = الكود الكامل للمكان، مثال: W1-F3-R5 (لازم يكون معرّف مسبقًا في صفحة إدارة المواقع).",
        "الاستيراد ما بينشئش مواقع جديدة - أي صف بكود موقع مش موجود بيتم رفضه وبيظهر سببه.",
        "لو المنتج في أكتر من مكان اكتب صف لكل مكان (نفس الـ SKU أو الـ ID أو الاسم) وفي كل صف كمية المكان ده.",
        "الموقع لازم يكون مكان نهائي (مالوش مواقع تحته).",
        "لو كتبت كود مخزن بس (مثال: W1) الكمية بتدخل مكان الاستلام الافتراضي للمخزن ده (لو متحدد)، وبعدين تتنقل لمكانها النهائي.",
        "أي كمية أكبر من صفر لازم يكون ليها موقع. صف من غير موقع وكميته صفر/فاضية = تحديث بيانات المنتج بس.",
        "عمود الباركود اختياري. اكتبه كنص (العمود متنسّق كنص) عشان الأصفار في الأول ماتتشالش، وأي باركود بصيغة علمية (1.23E+12) بيترفض. خلية الباركود الفاضية ما بتمسحش باركود موجود.",
        "الباركود لازم يكون مميز لكل منتج ومينفعش يساوي SKU منتج تاني. المطابقة بالترتيب: ID ثم SKU ثم الباركود ثم الاسم.",
        "عمود الحالة: اكتب مفعّل أو غير مفعّل. لو سبته فاضي: المنتج الجديد بيتعمل مفعّل، والمنتج الموجود حالته ما بتتغيّرش.",
    ])
    return StreamingResponse(
        buffer,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": "attachment; filename=products_template.xlsx"},
    )


MAX_REPORTED_ERRORS = 500   # سقف الأخطاء المرجّعة للواجهة (مع عدّاد بالإجمالي)
IMPORT_BATCH_SIZE = 500     # عدد المنتجات في كل commit


class _ImportErrors:
    """أخطاء الاستيراد المنظمة: [{row, column, message}] بسقف MAX_REPORTED_ERRORS وعدّاد بالإجمالي."""

    def __init__(self):
        self.items = []
        self.total = 0

    def add(self, row, column, message):
        self.total += 1
        if len(self.items) < MAX_REPORTED_ERRORS:
            self.items.append({"row": row, "column": column, "message": message})

    @property
    def truncated(self) -> bool:
        return self.total > len(self.items)


@router.post("/products/import")
def import_products(
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
    current_user: models.User = Depends(auth.require_permission("products.import")),
    _limit: None = Depends(limit_per_user("excel_import", 20, 60 * 60)),
):
    """
    استيراد منتجات من Excel. المطابقة بالترتيب: ID ثم SKU ثم الباركود، والاسم **جوه الملف بس** (منتج من غير ID/SKU/باركود = منتج جديد).
    الأداء: خرائط (id/sku/barcode) بتتحمّل مرة واحدة في الذاكرة بدل استعلام لكل صف، والكتابة على دفعات ~500 منتج في commit واحد
    (لو دفعة فشلت بنعيدها منتج منتج عشان الغلطة تتحدد). الأخطاء بشكل {row, column, message} وبسقف 500 مع عدّاد.
    """
    if not (file.filename or "").lower().endswith(".xlsx"):
        raise HTTPException(status_code=400, detail="لازم الملف يكون Excel بصيغة .xlsx")

    if file.size is not None and file.size > MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail="الملف كبير جدًا (الحد الأقصى 10 ميجا)")
    content = file.file.read(MAX_UPLOAD_BYTES + 1)  # def عادي (مش async): الشغل كله blocking (pandas + DB) فمينفعش يقفل الـ event loop
    if len(content) > MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail="الملف كبير جدًا (الحد الأقصى 10 ميجا)")
    _validate_xlsx_container(content)
    try:
        df = pd.read_excel(io.BytesIO(content), dtype=str)
    except Exception:
        raise HTTPException(status_code=400, detail="مقدرش أقرأ الملف، اتأكد إنه شيت اكسل سليم")
    if len(df) > MAX_IMPORT_ROWS:
        raise HTTPException(status_code=400, detail=f"الملف فيه صفوف كتير ({len(df)}). الحد الأقصى {MAX_IMPORT_ROWS} صف في المرة الواحدة")
    df = df.rename(columns={c: _COLUMN_ALIASES.get(str(c).strip().lower(), str(c).strip()) for c in df.columns})

    required = {"الاسم"}
    if not required.issubset(set(df.columns)):
        raise HTTPException(
            status_code=400,
            detail=f"لازم يكون في الأعمدة دي على الأقل: {', '.join(required)}. الأعمدة الموجودة: {', '.join(map(str, df.columns))}",
        )
    has_location_col = "الموقع" in df.columns
    has_status_col = "الحالة" in df.columns

    errors = _ImportErrors()
    P = models.Product
    idx = LocationIndex(db)

    # ---- خرائط في الذاكرة (استعلام واحد) بدل استعلام لكل صف ----
    by_id, by_sku, sku_lower, barcode_lower = {}, {}, {}, {}
    for pid, sku_v, barcode_v in db.query(P.id, P.sku, P.barcode).all():
        by_id[pid] = pid
        if sku_v:
            by_sku.setdefault(sku_v, pid)
            sku_lower.setdefault(sku_v.lower(), pid)
        if barcode_v:
            barcode_lower.setdefault(barcode_v.lower(), pid)

    def barcode_conflict_msg(barcode, exclude_id):
        low = barcode.lower()
        owner = barcode_lower.get(low)
        if owner is not None and owner != exclude_id:
            return "الباركود ده مستخدم في منتج تاني"
        owner = sku_lower.get(low)
        if owner is not None and owner != exclude_id:
            return "الباركود ده مستخدم كـ SKU لمنتج تاني"
        return None

    # ---- المرحلة 1: قراءة الصفوف وتجميعها حسب المنتج (صف لكل مكان تخزين) ----
    can_adjust = permissions.has_permission(current_user, "products.adjust")
    can_price = permissions.has_permission(current_user, "products.price")
    seen_barcodes = {}  # الباركود (lower) -> مفتاح المنتج اللي استخدمه في الملف (لمنع تكراره لمنتجين مختلفين)
    entries = {}  # key -> {"product_id", "name", "sku", "row", "loc_rows": {location_id: qty}, "values"}
    for i, row in df.iterrows():
        rn = i + 2
        try:
            product_id = _clean(row.get("ID"))  # اختياري - لو موجود بيبقى هو المرجع الأساسي للمطابقة
            sku = _clean(row.get("SKU"))  # اختياري
            try:
                barcode = normalize_excel_barcode(row.get("الباركود"))  # اختياري
            except ValueError as exc:
                errors.add(rn, "الباركود", str(exc))
                continue
            name = _clean(row.get("الاسم"))
            if not name:
                errors.add(rn, "الاسم", "اسم المنتج فاضي")
                continue

            # --- الموقع والكمية (بيتحققوا قبل أي حاجة عشان الصف المرفوض ما يعملش منتج) ---
            loc_raw = _clean(row.get("الموقع")) if has_location_col else None
            location_id, qty = None, None
            if loc_raw:
                res = idx.resolve(loc_raw)
                if res["error"]:
                    errors.add(rn, "الموقع", f"موقع غير صالح ({loc_raw}) - {res['error']}. عرّف الموقع من صفحة إدارة المواقع الأول (الاستيراد ما بينشئش مواقع)")
                    continue
                target = res["location"]
                if target is None:  # كود مخزن بس -> مكان الاستلام الافتراضي بتاعه
                    target = idx.default_location(res["warehouse"].id)
                    if target is None:
                        errors.add(rn, "الموقع", f"({loc_raw}) كود مخزن ومفيش له مكان استلام افتراضي - اكتب الكود الكامل للمكان أو حدد مكان افتراضي للمخزن")
                        continue
                location_id = target.id
                if not idx.is_leaf(location_id):
                    errors.add(rn, "الموقع", f"الموقع ({idx.full_code(location_id)}) فيه مستويات تحته - حدد الموقع النهائي")
                    continue
                qty = _strict_num(row.get("الكمية"))
                if qty is None or qty < 0:
                    errors.add(rn, "الكمية", f"كمية الموقع ({loc_raw}) لازم تكون رقم صحيح وموجب")
                    continue
            else:
                raw_qty = row.get("الكمية")
                qty = 0.0 if pd.isna(raw_qty) else _strict_num(raw_qty)
                if qty is None or qty < 0:
                    errors.add(rn, "الكمية", "الكمية لازم تكون رقم صحيح وموجب")
                    continue
                if qty > 0:
                    errors.add(rn, "الموقع", f"الكمية ({qty:g}) لازم يتحدد لها موقع - اكتب كود المكان في عمود الموقع (مفيش بضاعة من غير مكان)")
                    continue

            if not can_adjust and (loc_raw or (qty or 0) > 0):
                errors.add(rn, "الموقع", "ملكش صلاحية تعديل الكميات والمواقع - سيب عمودي الكمية والموقع فاضيين")
                continue

            # --- حالة المنتج (مفعّل / غير مفعّل) ---
            is_active = None
            if has_status_col:
                try:
                    is_active = _parse_active(row.get("الحالة"))
                except ValueError:
                    errors.add(rn, "الحالة", f"قيمة الحالة غير مفهومة ({_clean(row.get('الحالة'))}) - اكتب مفعّل أو غير مفعّل")
                    continue

            # --- مطابقة المنتج: ID ثم SKU ثم الباركود، ثم الاسم (جوه نفس الملف بس) ---
            matched_id = None
            if product_id:
                try:
                    matched_id = by_id.get(int(float(product_id)))
                except (ValueError, TypeError):
                    matched_id = None
            if matched_id is None and sku:
                matched_id = by_sku.get(sku)
            if barcode:
                by_barcode_id = barcode_lower.get(barcode.lower())
                if matched_id is None and by_barcode_id is not None:
                    matched_id = by_barcode_id
                elif matched_id is not None and by_barcode_id is not None and by_barcode_id != matched_id:
                    errors.add(rn, "الباركود", f"الباركود ({barcode}) تابع لمنتج تاني غير المنتج المطابق")
                    continue
                conflict = barcode_conflict_msg(barcode, matched_id)
                if conflict:
                    errors.add(rn, "الباركود", conflict)
                    continue
            if sku:
                owner = barcode_lower.get(sku.lower())
                if owner is not None and owner != matched_id:
                    errors.add(rn, "SKU", f"الـ SKU ({sku}) مستخدم كباركود لمنتج تاني")
                    continue
            key = ("pid", matched_id) if matched_id else (("sku", sku) if sku else (("barcode", barcode.lower()) if barcode else ("name", name.lower())))
            if barcode:
                owner_key, owner_name = seen_barcodes.setdefault(barcode.lower(), (key, name.lower()))
                # نفس الباركود لمنتجين مختلفين: مفتاحين مختلفين، أو منتج جديد بنفس الباركود لكن باسم مختلف
                if owner_key != key or (matched_id is None and owner_name != name.lower()):
                    errors.add(rn, "الباركود", f"نفس الباركود ({barcode}) مستخدم لمنتجين مختلفين في الملف")
                    continue

            entry = entries.get(key)
            if entry is None:
                entry = entries[key] = {
                    "product_id": matched_id, "name": name, "sku": sku, "barcode": barcode, "row": rn, "loc_rows": {},
                    "values": dict(
                        category=_clean(row.get("التصنيف")), brand=_clean(row.get("الماركة")),
                        unit=_clean(row.get("الوحدة")) or "قطعة", price=_num(row.get("السعر")),
                        min_quantity=_num(row.get("حد الطلب الأدنى")), is_active=is_active,
                    ),
                }
            if location_id:
                if location_id in entry["loc_rows"]:
                    errors.add(rn, "الموقع", f"نفس المنتج ونفس الموقع ({idx.full_code(location_id)}) مكرر في الملف - اجمع الكميات في صف واحد")
                    continue
                entry["loc_rows"][location_id] = round(qty, 3)
        except Exception:
            _log.exception("excel import: row %s failed", rn)
            errors.add(rn, None, "خطأ غير متوقع في قراءة الصف - راجع بيانات الصف")

    # ---- تصنيفات/ماركات جديدة: مرة واحدة قبل الدفعات ----
    category_cache = {c.name: c.id for c in db.query(models.Category).all()}
    brand_cache = {b.name: b.id for b in db.query(models.Brand).all()}
    for entry in entries.values():
        for kind, cache, model in (("category", category_cache, models.Category), ("brand", brand_cache, models.Brand)):
            nm = (entry["values"][kind] or "").strip()
            if nm and nm not in cache:
                obj = model(name=nm)
                db.add(obj)
                db.flush()
                cache[nm] = obj.id
    db.commit()

    # ---- المرحلة 2: الكتابة على دفعات ----
    stats = {"created": 0, "updated": 0}

    def write_entry(entry, product) -> bool:
        """بيكتب منتج واحد (من غير commit). بيرجّع False لو اترفض بسبب صلاحية (الخطأ اتسجّل)."""
        v = entry["values"]
        values = dict(
            sku=entry["sku"], barcode=entry["barcode"], name=entry["name"],
            category_id=category_cache.get((v["category"] or "").strip()) if v["category"] else None,
            brand_id=brand_cache.get((v["brand"] or "").strip()) if v["brand"] else None,
            unit=v["unit"], price=v["price"], min_quantity=v["min_quantity"], is_active=v["is_active"],
        )
        old_total = None
        if product and not can_price and values["price"] is not None and round(float(product.price or 0), 2) != round(float(values["price"]), 2):
            errors.add(entry["row"], "السعر", f"({entry['name']}): ملكش صلاحية تغيير سعر منتج موجود")
            return False
        if product:
            old_total = float(inventory.product_total(db, product.id))
            old_price = float(product.price or 0)
            for k, val in values.items():
                if k == "is_active" and val is None:  # الحالة فاضية = ما نغيّرش حالة المنتج الموجود
                    continue
                if k in ("sku", "barcode") and not values[k]:
                    continue  # ما نمسحش SKU/باركود موجود لو الخلية سايبينها فاضية
                setattr(product, k, val)
            if values["price"] is not None and round(old_price, 2) != round(float(values["price"]), 2):
                audit(db, current_user, "product", product.id, "price_change",
                      {"old": round(old_price, 2), "new": round(float(values["price"]), 2), "source": "excel"})
        else:
            if values["is_active"] is None:
                values["is_active"] = True  # منتج جديد من غير حالة = مفعّل
            product = models.Product(**values, quantity=0.0)
            db.add(product)
            db.flush()
            lock_products(db, [product.id])
        # الأماكن اللي في الملف بس بتتغير (الباقي ما بيتلمسش)، والإجمالي بيتحدّث بالفرق جوه الـ service
        for loc_id, qty in sorted(entry["loc_rows"].items()):
            inventory.set_location_quantity(db, product.id, loc_id, qty)
        new_total = float(inventory.product_total(db, product.id))
        if old_total is None:
            stats["created"] += 1
        else:
            stats["updated"] += 1
            if new_total != old_total:
                db.add(models.QuantityAdjustment(
                    product_id=product.id, old_quantity=old_total, new_quantity=new_total,
                    difference=round(new_total - old_total, 3),
                    changed_by_id=current_user.id, changed_by_username=current_user.username,
                    reason="استيراد Excel", date=now_cairo(),
                ))
        return True

    def run_batch(batch) -> None:
        locked = lock_products(db, [e["product_id"] for e in batch if e["product_id"]])
        for entry in batch:
            write_entry(entry, locked.get(entry["product_id"]) if entry["product_id"] else None)

    all_entries = list(entries.values())
    for start in range(0, len(all_entries), IMPORT_BATCH_SIZE):
        batch = all_entries[start:start + IMPORT_BATCH_SIZE]
        snapshot = (stats["created"], stats["updated"], errors.total, len(errors.items))
        try:
            run_batch(batch)
            db.commit()
        except Exception:
            db.rollback()
            # رجّع العدّادات لما قبل الدفعة وأعدها منتج منتج (commit لكل واحد) عشان نعرف المنتج الغلطان بالظبط
            stats["created"], stats["updated"] = snapshot[0], snapshot[1]
            errors.total, del_from = snapshot[2], snapshot[3]
            del errors.items[del_from:]
            for entry in batch:
                try:
                    run_batch([entry])
                    db.commit()
                except Exception as exc:
                    db.rollback()
                    _log.exception("excel import: product %s failed", entry.get("name"))
                    if isinstance(exc, (inventory.BelowReserved, inventory.InsufficientStock)):
                        errors.add(entry["row"], "الكمية", f"({entry['name']}): {exc}")
                    else:
                        errors.add(entry["row"], None, f"({entry['name']}): تعذّر حفظ المنتج")

    created, updated = stats["created"], stats["updated"]
    message = f"تم إضافة {created} منتج وتحديث {updated} منتج"
    if errors.total:
        message += f" - وتم رفض/تجاهل {errors.total} صف (راجع التفاصيل)"
    audit(db, current_user, "import", "products", "excel_import",
          {"created": created, "updated": updated, "errors": errors.total, "rows": int(len(df))})
    db.commit()
    return {"created": created, "updated": updated, "errors": errors.items, "errors_total": errors.total,
            "errors_truncated": errors.truncated, "message": message}


def _strict_num(value):
    """رقم صحيح أو None (عكس _num اللي بترجع 0 لو القيمة غلط)."""
    try:
        if pd.isna(value):
            return None
        return float(str(value).replace(",", "").strip())
    except (ValueError, TypeError):
        return None


def _clean(value):
    if pd.isna(value):
        return None
    text = str(value).strip()
    return text if text and text != "nan" else None


def _num(value):
    try:
        if pd.isna(value):
            return 0.0
        return float(value)
    except (ValueError, TypeError):
        return 0.0


# ---------- تصدير حركات الوارد/الصادر لعمل analysis عليها ----------
@router.get("/movements/export")
def export_movements(
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(auth.require_permission("invoices.view")),
):
    """
    تصدير الوارد والصادر والنقل (بيحترم date_from/date_to). الاستعلامات بـ JOIN واحد لكل شيت (مش lazy-load لكل صف).
    من غير finance.view أعمدة الأسعار بتتشال، والنقل بيطلع بس لو عنده transfers.view. حد أقصى MAX_EXPORT_ROWS صف لكل شيت.
    """
    start, end_excl = parse_dt(date_from, "تاريخ البداية"), end_exclusive(date_to)
    show_prices = permissions.has_permission(current_user, "finance.view")
    can_transfers = permissions.has_permission(current_user, "transfers.view")
    idx = LocationIndex(db)
    P, U = models.Product, models.User

    def _invoice_rows(Inv, Item, Party, party_fk, user_fk, user_snap, party_col, user_col):
        q = (
            db.query(Inv.id, Inv.date, P.name, P.sku, Item.location_id, Item.quantity, Item.unit_price, Party.name,
                     U.username, U.is_active, getattr(Inv, user_snap), Inv.notes)
            .join(Item, join_valid(Inv, Item))  # الحركة الفعلية (completed) بس
            .join(P, P.id == Item.product_id)
            .outerjoin(Party, Party.id == getattr(Inv, party_fk))
            .outerjoin(U, U.id == getattr(Inv, user_fk))
        )
        if start:
            q = q.filter(Inv.date >= start)
        if end_excl:
            q = q.filter(Inv.date < end_excl)
        rows = q.order_by(Inv.date.desc(), Inv.id.desc(), Item.id).limit(MAX_EXPORT_ROWS).all()
        out = []
        for inv_id, date, pname, sku, loc_id, qty, price, party, uname, uactive, usnap, notes in rows:
            row = {
                "رقم المستند": inv_id, "التاريخ": date, "المنتج": pname, "SKU": sku,
                "الموقع": idx.full_code(loc_id) if loc_id else None, "الكمية": qty,
            }
            if show_prices:
                row["سعر الوحدة"] = price
            row[party_col] = party
            user_obj = None if uname is None else type("U", (), {"username": uname, "is_active": uactive})()
            row[user_col] = format_user_display(user_obj, usnap)
            row["ملاحظات"] = notes
            out.append(row)
        return out

    in_data = _invoice_rows(models.StockInInvoice, models.StockInItem, models.Supplier, "supplier_id", "received_by_id",
                            "received_by_username", "المورد", "استلمها")
    out_data = _invoice_rows(models.StockOutInvoice, models.StockOutItem, models.Recipient, "recipient_id", "delivered_by_id",
                             "delivered_by_username", "استلمها (الجهة)", "سلمها")

    buffer = io.BytesIO()
    with pd.ExcelWriter(buffer, engine="openpyxl") as writer:
        pd.DataFrame(in_data).pipe(_safe_df).to_excel(writer, index=False, sheet_name="الوارد")
        pd.DataFrame(out_data).pipe(_safe_df).to_excel(writer, index=False, sheet_name="الصادر")
        if can_transfers:
            T = models.LocationTransfer
            tq = db.query(T).options(joinedload(T.product), joinedload(T.moved_by))
            if start:
                tq = tq.filter(T.date >= start)
            if end_excl:
                tq = tq.filter(T.date < end_excl)
            transfers = tq.order_by(T.date.desc(), T.id.desc()).limit(MAX_EXPORT_ROWS).all()
            pd.DataFrame(
                [
                    {
                        "التاريخ": t.date,
                        "المنتج": t.product.name if t.product else None,
                        "SKU": t.product.sku if t.product else None,
                        "الكمية": t.quantity,
                        "من موقع": idx.full_code(t.from_location_id),
                        "إلى موقع": idx.full_code(t.to_location_id),
                        "اللي نقل": format_user_display(t.moved_by, t.moved_by_username),
                        "ملاحظات": t.notes,
                    }
                    for t in transfers
                ],
                columns=["التاريخ", "المنتج", "SKU", "الكمية", "من موقع", "إلى موقع", "اللي نقل", "ملاحظات"],
            ).pipe(_safe_df).to_excel(writer, index=False, sheet_name="النقل بين المواقع")
    buffer.seek(0)

    filename = f"movements_export_{datetime.now().strftime('%Y%m%d_%H%M')}.xlsx"
    return StreamingResponse(
        buffer,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )


# ---------- سجل تعديلات الكمية اليدوية - مش ظاهر في الموقع، تحميل فقط (للأدمن) ----------
@router.get("/quantity-adjustments/export")
def export_quantity_adjustments(db: Session = Depends(get_db), admin: models.User = Depends(auth.require_permission("reports.view"))):
    logs = db.query(models.QuantityAdjustment).order_by(models.QuantityAdjustment.date.desc()).all()
    data = [
        {
            "التاريخ": log.date,
            "المنتج": log.product.name if log.product else None,
            "SKU": log.product.sku if log.product else None,
            "الكمية قبل": log.old_quantity,
            "الكمية بعد": log.new_quantity,
            "الفرق": log.difference,
            "اللي عدّل": format_user_display(log.changed_by, log.changed_by_username),
            "السبب": log.reason,
        }
        for log in logs
    ]
    df = pd.DataFrame(data)

    buffer = io.BytesIO()
    with pd.ExcelWriter(buffer, engine="openpyxl") as writer:
        df.pipe(_safe_df).to_excel(writer, index=False, sheet_name="تعديلات الكمية")
    buffer.seek(0)

    filename = f"quantity_adjustments_{datetime.now().strftime('%Y%m%d_%H%M')}.xlsx"
    return StreamingResponse(
        buffer,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )

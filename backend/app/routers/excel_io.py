import io
import re
from datetime import datetime

import pandas as pd
from fastapi import APIRouter, Depends, HTTPException, UploadFile, File
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session

from .. import models, auth
from ..display_utils import format_user_display
from ..database import get_db
from ..location_utils import LocationIndex, lock_products
from ..timezone_utils import now_cairo

router = APIRouter(prefix="/api/excel", tags=["Excel Import/Export"])

MAX_UPLOAD_BYTES = 10 * 1024 * 1024  # 10MB
MAX_IMPORT_ROWS = 20000


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
PRODUCT_COLUMNS = ["ID", "SKU", "الاسم", "التصنيف", "الماركة", "الوحدة", "السعر", "الموقع", "الكمية", "حد الطلب الأدنى", "الحالة"]

# أسماء بديلة مقبولة للأعمدة (إنجليزي/عربي) - بتتحوّل للأسماء الأساسية قبل القراءة
_COLUMN_ALIASES = {
    "id": "ID", "sku": "SKU", "product code": "SKU", "كود المنتج": "SKU",
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
        if instructions:
            pd.DataFrame({"تعليمات": instructions}).pipe(_safe_df).to_excel(writer, index=False, sheet_name="تعليمات")
    buffer.seek(0)
    return buffer


@router.get("/products/export")
def export_products(db: Session = Depends(get_db), current_user: models.User = Depends(auth.get_current_user)):
    idx = LocationIndex(db)
    products = db.query(models.Product).order_by(models.Product.name).all()
    data = []
    for p in products:
        base = {
            "ID": p.id, "SKU": p.sku, "الاسم": p.name,
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
def download_template():
    """شيت فاضي بنفس الأعمدة عشان تعرف تملأه صح (+ شيت تعليمات للمواقع)."""
    df = pd.DataFrame(columns=PRODUCT_COLUMNS)
    buffer = _sheet_bytes(df, instructions=[
        "عمود الموقع = الكود الكامل للمكان، مثال: W1-F3-R5 (لازم يكون معرّف مسبقًا في صفحة إدارة المواقع).",
        "الاستيراد ما بينشئش مواقع جديدة - أي صف بكود موقع مش موجود بيتم رفضه وبيظهر سببه.",
        "لو المنتج في أكتر من مكان اكتب صف لكل مكان (نفس الـ SKU أو الـ ID أو الاسم) وفي كل صف كمية المكان ده.",
        "الموقع لازم يكون مكان نهائي (مالوش مواقع تحته).",
        "لو كتبت كود مخزن بس (مثال: W1) الكمية بتدخل مكان الاستلام الافتراضي للمخزن ده (لو متحدد)، وبعدين تتنقل لمكانها النهائي.",
        "أي كمية أكبر من صفر لازم يكون ليها موقع. صف من غير موقع وكميته صفر/فاضية = تحديث بيانات المنتج بس.",
        "عمود الحالة: اكتب مفعّل أو غير مفعّل. لو سبته فاضي: المنتج الجديد بيتعمل مفعّل، والمنتج الموجود حالته ما بتتغيّرش.",
    ])
    return StreamingResponse(
        buffer,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": "attachment; filename=products_template.xlsx"},
    )


@router.post("/products/import")
async def import_products(
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
    current_user: models.User = Depends(auth.get_current_user),
):
    if not file.filename.endswith((".xlsx", ".xls")):
        raise HTTPException(status_code=400, detail="لازم الملف يكون Excel (.xlsx)")

    if file.size is not None and file.size > MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail="الملف كبير جدًا (الحد الأقصى 10 ميجا)")
    content = await file.read(MAX_UPLOAD_BYTES + 1)
    if len(content) > MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail="الملف كبير جدًا (الحد الأقصى 10 ميجا)")
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

    errors = []
    idx = LocationIndex(db)
    category_cache = {c.name: c for c in db.query(models.Category).all()}
    brand_cache = {b.name: b for b in db.query(models.Brand).all()}

    def get_or_create_category(name):
        if not name:
            return None
        name = name.strip()
        if not name:
            return None
        if name in category_cache:
            return category_cache[name].id
        category = models.Category(name=name)
        db.add(category)
        db.flush()
        category_cache[name] = category
        return category.id

    def get_or_create_brand(name):
        if not name:
            return None
        name = name.strip()
        if not name:
            return None
        if name in brand_cache:
            return brand_cache[name].id
        brand = models.Brand(name=name)
        db.add(brand)
        db.flush()
        brand_cache[name] = brand
        return brand.id

    # ---- المرحلة 1: قراءة الصفوف وتجميعها حسب المنتج (صف لكل مكان تخزين) ----
    entries = {}  # key -> {"product", "name", "sku", "row", "unassigned", "loc_rows": {location_id: qty}}
    for i, row in df.iterrows():
        rn = i + 2
        try:
            product_id = _clean(row.get("ID"))  # اختياري - لو موجود بيبقى هو المرجع الأساسي للمطابقة
            sku = _clean(row.get("SKU"))  # اختياري
            name = _clean(row.get("الاسم"))
            if not name:
                errors.append(f"صف {rn}: اسم المنتج فاضي")
                continue

            # --- الموقع والكمية (بيتحققوا قبل أي حاجة عشان الصف المرفوض ما يعملش منتج) ---
            loc_raw = _clean(row.get("الموقع")) if has_location_col else None
            location_id, qty = None, None
            if loc_raw:
                res = idx.resolve(loc_raw)
                if res["error"]:
                    errors.append(f"صف {rn}: موقع غير صالح ({loc_raw}) - {res['error']}. عرّف الموقع من صفحة إدارة المواقع الأول (الاستيراد ما بينشئش مواقع)")
                    continue
                target = res["location"]
                if target is None:  # كود مخزن بس -> مكان الاستلام الافتراضي بتاعه
                    target = idx.default_location(res["warehouse"].id)
                    if target is None:
                        errors.append(f"صف {rn}: ({loc_raw}) كود مخزن ومفيش له مكان استلام افتراضي - اكتب الكود الكامل للمكان أو حدد مكان افتراضي للمخزن")
                        continue
                location_id = target.id
                if not idx.is_leaf(location_id):
                    errors.append(f"صف {rn}: الموقع ({idx.full_code(location_id)}) فيه مستويات تحته - حدد الموقع النهائي")
                    continue
                qty = _strict_num(row.get("الكمية"))
                if qty is None or qty < 0:
                    errors.append(f"صف {rn}: كمية الموقع ({loc_raw}) لازم تكون رقم صحيح وموجب")
                    continue
            else:
                raw_qty = row.get("الكمية")
                qty = 0.0 if pd.isna(raw_qty) else _strict_num(raw_qty)
                if qty is None or qty < 0:
                    errors.append(f"صف {rn}: الكمية لازم تكون رقم صحيح وموجب")
                    continue
                if qty > 0:
                    errors.append(f"صف {rn}: الكمية ({qty:g}) لازم يتحدد لها موقع - اكتب كود المكان في عمود الموقع (مفيش بضاعة من غير مكان)")
                    continue

            # --- حالة المنتج (مفعّل / غير مفعّل) ---
            is_active = None
            if has_status_col:
                try:
                    is_active = _parse_active(row.get("الحالة"))
                except ValueError:
                    errors.append(f"صف {rn}: قيمة الحالة غير مفهومة ({_clean(row.get('الحالة'))}) - اكتب مفعّل أو غير مفعّل")
                    continue

            # --- مطابقة المنتج: ID ثم SKU ثم الاسم (جوه نفس الملف بس) ---
            product = None
            if product_id:
                try:
                    product = db.query(models.Product).filter(models.Product.id == int(float(product_id))).first()
                except (ValueError, TypeError):
                    product = None
            if not product and sku:
                product = db.query(models.Product).filter(models.Product.sku == sku).first()
            key = ("pid", product.id) if product else (("sku", sku) if sku else ("name", name.lower()))

            entry = entries.get(key)
            if entry is None:
                entry = entries[key] = {
                    "product": product, "name": name, "sku": sku, "row": rn, "loc_rows": {},
                    "values": dict(
                        category=_clean(row.get("التصنيف")), brand=_clean(row.get("الماركة")),
                        unit=_clean(row.get("الوحدة")) or "قطعة", price=_num(row.get("السعر")),
                        min_quantity=_num(row.get("حد الطلب الأدنى")), is_active=is_active,
                    ),
                }
            if location_id:
                if location_id in entry["loc_rows"]:
                    errors.append(f"صف {rn}: نفس المنتج ونفس الموقع ({idx.full_code(location_id)}) مكرر في الملف - اجمع الكميات في صف واحد")
                    continue
                entry["loc_rows"][location_id] = round(qty, 3)
        except Exception as e:
            errors.append(f"صف {rn}: خطأ - {str(e)}")

    # ---- المرحلة 2: الكتابة في الداتا بيز (كل منتج في commit مستقل) ----
    created, updated = 0, 0
    for entry in entries.values():
        try:
            v = entry["values"]
            values = dict(
                sku=entry["sku"], name=entry["name"],
                category_id=get_or_create_category(v["category"]), brand_id=get_or_create_brand(v["brand"]),
                unit=v["unit"], price=v["price"], min_quantity=v["min_quantity"],
                is_active=v["is_active"],
            )
            product = entry["product"]
            old_total = None
            if product:
                old_total = round(product.quantity or 0, 3)
                for k, val in values.items():
                    if k == "is_active" and val is None:  # الحالة فاضية = ما نغيّرش حالة المنتج الموجود
                        continue
                    if k != "sku" or entry["sku"]:  # ما نمسحش SKU موجود لو الخلية سايبينها فاضية بالغلط
                        setattr(product, k, val)
            else:
                if values["is_active"] is None:
                    values["is_active"] = True  # منتج جديد من غير حالة = مفعّل
                product = models.Product(**values, quantity=0.0)
                db.add(product)
                db.flush()

            lock_products(db, [product.id])  # قفل صف المنتج (FOR UPDATE) قبل تعديل كمياته
            by_loc = {pl.location_id: pl for pl in product.locations}
            # الإجمالي = القديم - (القديم في الأماكن اللي في الملف) + (الجديد فيها). الأماكن اللي مش في الملف ما بتتلمسش.
            new_total = round(product.quantity or 0, 3)
            for loc_id, qty in entry["loc_rows"].items():
                pl = by_loc.get(loc_id)
                new_total = round(new_total - (pl.quantity if pl else 0) + qty, 3)
                if qty <= 0:
                    if pl:
                        db.delete(pl)
                elif pl:
                    pl.quantity = qty
                else:
                    db.add(models.ProductLocation(product_id=product.id, location_id=loc_id, quantity=qty))
            product.quantity = new_total

            if old_total is None:
                created += 1
            else:
                updated += 1
                if new_total != old_total:
                    db.add(models.QuantityAdjustment(
                        product_id=product.id, old_quantity=old_total, new_quantity=new_total,
                        difference=round(new_total - old_total, 3),
                        changed_by_id=current_user.id, changed_by_username=current_user.username,
                        reason="استيراد Excel", date=now_cairo(),
                    ))
            db.commit()
        except Exception as e:
            db.rollback()
            errors.append(f"صف {entry['row']} ({entry['name']}): خطأ - {str(e)}")

    message = f"تم إضافة {created} منتج وتحديث {updated} منتج"
    if errors:
        message += f" - وتم رفض/تجاهل {len(errors)} صف (راجع التفاصيل)"
    return {"created": created, "updated": updated, "errors": errors, "message": message}


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
def export_movements(db: Session = Depends(get_db), current_user: models.User = Depends(auth.get_current_user)):
    idx = LocationIndex(db)
    in_invoices = db.query(models.StockInInvoice).order_by(models.StockInInvoice.date.desc()).all()
    out_invoices = db.query(models.StockOutInvoice).order_by(models.StockOutInvoice.date.desc()).all()

    in_data = [
        {
            "رقم الفاتورة": inv.id,
            "التاريخ": inv.date,
            "المنتج": it.product.name if it.product else None,
            "SKU": it.product.sku if it.product else None,
            "الموقع": idx.full_code(it.location_id) if it.location_id else None,
            "الكمية": it.quantity,
            "سعر الوحدة": it.unit_price,
            "المورد": inv.supplier.name if inv.supplier else None,
            "استلمها": format_user_display(inv.received_by, inv.received_by_username),
            "ملاحظات": inv.notes,
        }
        for inv in in_invoices
        for it in inv.items
    ]
    out_data = [
        {
            "رقم الفاتورة": inv.id,
            "التاريخ": inv.date,
            "المنتج": it.product.name if it.product else None,
            "SKU": it.product.sku if it.product else None,
            "الموقع": idx.full_code(it.location_id) if it.location_id else None,
            "الكمية": it.quantity,
            "سعر الوحدة": it.unit_price,
            "استلمها (الجهة)": inv.recipient.name if inv.recipient else None,
            "سلمها": format_user_display(inv.delivered_by, inv.delivered_by_username),
            "ملاحظات": inv.notes,
        }
        for inv in out_invoices
        for it in inv.items
    ]

    buffer = io.BytesIO()
    with pd.ExcelWriter(buffer, engine="openpyxl") as writer:
        pd.DataFrame(in_data).pipe(_safe_df).to_excel(writer, index=False, sheet_name="الوارد")
        pd.DataFrame(out_data).pipe(_safe_df).to_excel(writer, index=False, sheet_name="الصادر")
        transfers = db.query(models.LocationTransfer).order_by(models.LocationTransfer.date.desc()).all()
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
def export_quantity_adjustments(db: Session = Depends(get_db), admin: models.User = Depends(auth.require_admin)):
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

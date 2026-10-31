"""
الباركود (حقل منفصل عن الـ SKU): تنظيف/تحقق في مكان واحد (API + Excel) + فحوص التفرّد.
- الـ SKU = كود داخلي للشركة، والباركود = الكود اللي بيتقرا من الملصق. مسموح منتج SKU بتاعه = الباركود بتاعه.
- ممنوع باركود منتج يساوي SKU منتج تاني (والعكس) عشان المسح (باركود ثم SKU) ميجيبش منتج غلط.
"""
import re
from typing import Optional

from sqlalchemy import func
from sqlalchemy.orm import Session

from . import models

MAX_LEN = 64
_VISIBLE_ASCII = re.compile(r"[\x21-\x7E]+")
_STARTS_ALNUM = re.compile(r"[A-Za-z0-9]")
_FLOAT_SUFFIX = re.compile(r"^(\d+)\.0+$")
_SCIENTIFIC = re.compile(r"^[+-]?\d+(\.\d+)?[eE][+-]?\d+$")


def normalize_barcode(value) -> Optional[str]:
    """
    فاضي/None ← None. غير كده بيتحقق: 1-64 حرف، ASCII مرئي من غير مسافات، وأول حرف حرف/رقم
    (بيقفل Excel formula injection: = + @ -). بيتخزن زي ما اتكتب (الأصفار وحالة الأحرف محفوظة).
    """
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    if len(text) > MAX_LEN:
        raise ValueError("الباركود لازم يكون من 1 لـ 64 حرف")
    if not _VISIBLE_ASCII.fullmatch(text):
        raise ValueError("الباركود لازم يكون حروف إنجليزي وأرقام ورموز من غير مسافات")
    if not _STARTS_ALNUM.match(text):
        raise ValueError("الباركود لازم يبدأ بحرف أو رقم")
    return text


def normalize_excel_barcode(value) -> Optional[str]:
    """
    باركود جاي من خلية Excel: الخلية الرقمية بتتقرا "123.0" فبنشيل الـ .0، والصيغة العلمية (1.23E+12)
    مرفوضة لأن Excel خسّر الأرقام (لازم الخلية تتكتب كنص).
    """
    if value is None:
        return None
    text = str(value).strip()
    if not text or text.lower() == "nan":
        return None
    m = _FLOAT_SUFFIX.match(text)
    if m:
        text = m.group(1)
    elif _SCIENTIFIC.match(text):
        raise ValueError("الباركود لازم يتكتب كنص في Excel (الصيغة العلمية بتضيّع الأرقام)")
    return normalize_barcode(text)


def barcode_conflict(db: Session, barcode: str, exclude_product_id: Optional[int] = None) -> Optional[str]:
    """رسالة عربي لو الباركود مستخدم كباركود أو كـ SKU لمنتج تاني، وإلا None. المقارنة case-insensitive."""
    P = models.Product
    low = barcode.lower()
    q = db.query(P.id).filter(func.lower(P.barcode) == low)
    if exclude_product_id is not None:
        q = q.filter(P.id != exclude_product_id)
    if q.first():
        return "الباركود ده مستخدم في منتج تاني"
    q = db.query(P.id).filter(func.lower(P.sku) == low)
    if exclude_product_id is not None:
        q = q.filter(P.id != exclude_product_id)
    if q.first():
        return "الباركود ده مستخدم كـ SKU لمنتج تاني"
    return None


def sku_conflicts_with_barcode(db: Session, sku: str, exclude_product_id: Optional[int] = None) -> bool:
    """SKU جديد مينفعش يساوي باركود منتج تاني."""
    P = models.Product
    q = db.query(P.id).filter(func.lower(P.barcode) == sku.lower())
    if exclude_product_id is not None:
        q = q.filter(P.id != exclude_product_id)
    return q.first() is not None

"""دوال مساعدة مشتركة للاستعلامات (ترقيم، فلاتر تاريخ، بحث LIKE آمن، إجماليات الفواتير)."""
import math
from datetime import datetime, timedelta
from typing import Dict, Iterable, Optional, Tuple

from fastapi import HTTPException
from sqlalchemy import func, or_
from sqlalchemy.orm import Session


def like_escape(value: str) -> str:
    """بيهرّب % و _ و \\ عشان يتعاملوا كحروف عادية في LIKE."""
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def contains(column, value: str):
    """شرط 'العمود يحتوي على النص' (LIKE %..%) آمن."""
    return column.like(f"%{like_escape(value.strip())}%", escape="\\")


def parse_dt(value: Optional[str], label: str = "التاريخ") -> Optional[datetime]:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        raise HTTPException(status_code=400, detail=f"صيغة {label} غير صحيحة")


def end_exclusive(date_to: Optional[str]) -> Optional[datetime]:
    """نهاية الفترة (غير شاملة): تاريخ بس (YYYY-MM-DD) = بداية اليوم التالي، ووقت كامل = زي ما هو."""
    end = parse_dt(date_to, "تاريخ النهاية")
    if end is None:
        return None
    return end + timedelta(days=1) if len(date_to) <= 10 else end


def clamp_page(page: Optional[int]) -> int:
    return max(1, page or 1)


def clamp_page_size(page_size: Optional[int], default: int, max_value: int) -> int:
    if not page_size or page_size <= 0:
        return default
    return min(page_size, max_value)


def total_pages(total: int, page_size: int) -> int:
    return max(1, math.ceil(total / page_size)) if total else 1


def invoice_totals(db: Session, item_model, invoice_ids: Iterable[int]) -> Dict[int, Tuple[int, float]]:
    """{invoice_id: (عدد الأصناف, إجمالي القيمة)} لمجموعة فواتير بضربة واحدة (من غير تحميل الأصناف)."""
    ids = list(invoice_ids)
    if not ids:
        return {}
    rows = (
        db.query(
            item_model.invoice_id,
            func.count(item_model.id),
            func.coalesce(func.sum(item_model.quantity * func.coalesce(item_model.unit_price, 0)), 0),
        )
        .filter(item_model.invoice_id.in_(ids))
        .group_by(item_model.invoice_id)
        .all()
    )
    return {r[0]: (r[1], round(float(r[2] or 0), 2)) for r in rows}


def page_args(page: Optional[int], page_size: Optional[int], default: int = 15, max_value: int = 100, export: bool = False):
    """(page, page_size) بعد التقييد. export=True بيرجّع حد أكبر (تصدير CSV) - لحد 5000 صف."""
    if export:
        return 1, 5000
    return clamp_page(page), clamp_page_size(page_size, default, max_value)


def paged(items, total: int, page: int, page_size: int, **extra) -> dict:
    out = {"items": items, "total": total, "page": page, "page_size": page_size, "total_pages": total_pages(total, page_size)}
    out.update(extra)
    return out


def slice_page(rows: list, page: int, page_size: int) -> list:
    return rows[(page - 1) * page_size: page * page_size]


# ---------------------------------------------------------------------------
# helper موحّد لاستبعاد المستندات غير الفعلية من التقارير (قاعدة 14):
#  - الوارد: completed فقط (الملغي بره).
#  - الصادر: completed فقط (عرض السعر والحجز والملغي مش حركة فعلية ومش مبيعات).
# كل استعلام تقرير/إجمالي/ربحية بيستخدم valid_invoice(Inv)؛ سجل الحركة بيستخدم movement_invoice(Inv, include_voided).
# ---------------------------------------------------------------------------
def valid_invoice(Inv):
    return Inv.status == "completed"


def movement_invoice(Inv, include_voided: bool = False):
    """للسجل: المكتمل، ولو include_voided كمان الملغي (بعلامة). عروض الأسعار والحجوزات مش حركة فعلية فمبتظهرش هنا."""
    return Inv.status.in_(("completed", "voided")) if include_voided else valid_invoice(Inv)


def join_valid(Inv, Item):
    """شرط الـ JOIN بين المستند وأسطره + صلاحيته للتقارير (inner join فبيشتغل زي الفلتر)."""
    from sqlalchemy import and_

    return and_(Inv.id == Item.invoice_id, valid_invoice(Inv))


def out_net_factor(Inv):
    """
    معامل توزيع الخصم على الأسطر: 1 - (خصم المستند / إجمالي قيمة الأصناف قبل الخصم). حصة كل سطر من الخصم بنسبة قيمته،
    فصافي السطر = قيمته × المعامل ومجموع الصافي = صافي المستند. القسمة على صفر = معامل 1 (من غير خصم).
    """
    return 1 - func.coalesce(func.coalesce(Inv.discount_amount, 0) / func.nullif(Inv.subtotal_amount, 0), 0)

"""
كل تواريخ النظام (الفواتير، سجلات الدخول، تعديلات الكمية...) بتتسجل بتوقيت
القاهرة (مصر) مباشرة، مش UTC، عشان تبقى مفهومة زي ما هي لما تتعرض في الموقع
أو تتصدّر Excel من غير أي تحويل إضافي.
"""
from datetime import datetime
from zoneinfo import ZoneInfo

CAIRO_TZ = ZoneInfo("Africa/Cairo")


def now_cairo() -> datetime:
    """الوقت الحالي بتوقيت القاهرة، كـ naive datetime (من غير معلومات منطقة زمنية) عشان يتخزن عادي في القاعدة."""
    return datetime.now(CAIRO_TZ).replace(tzinfo=None)

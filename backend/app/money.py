"""
تقريب الكميات والفلوس في مكان واحد (ROUND_HALF_UP).

- الكميات بـ 3 خانات عشرية (كيلو/لتر/متر)، والفلوس بخانتين.
- كل الحسابات الداخلية بـ Decimal. أعمدة الـ DB من نوع DECIMAL (Numeric)، وبتتقرا في Python كـ float
  (asdecimal=False) عشان شكل الـ API والـ analytics يفضل زي ما هو - الدقة بتتضمن من الـ DB نفسه ومن
  إن كل كتابة بتعدّي على q3/q2 قبل ما توصل للـ DB.
"""
from decimal import Decimal, ROUND_HALF_UP
from typing import Union

Number = Union[int, float, str, Decimal, None]

_Q3 = Decimal("0.001")
_Q2 = Decimal("0.01")


def to_decimal(value: Number) -> Decimal:
    # str() عشان 0.1 يبقى Decimal("0.1") مش القيمة الثنائية الطويلة للـ float
    return Decimal(str(value if value is not None else 0))


def q3(value: Number) -> Decimal:
    """تقريب كمية لـ 3 خانات."""
    return to_decimal(value).quantize(_Q3, rounding=ROUND_HALF_UP)


def q2(value: Number) -> Decimal:
    """تقريب مبلغ لخانتين."""
    return to_decimal(value).quantize(_Q2, rounding=ROUND_HALF_UP)


def qty_float(value: Number) -> float:
    """كمية مقرّبة كـ float (لعرضها في الـ JSON)."""
    return float(q3(value))


def money_float(value: Number) -> float:
    return float(q2(value))

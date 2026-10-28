"""
حساب إجماليات فاتورة الصادر (خصم + ضريبة على إجمالي الفاتورة) بـ Decimal عشان نتفادى مشاكل الـ float
(زي bug حفظ 25 كـ 24.89). الحساب هنا في الباك اند هو المرجع، والفرونت بيعرض معاينة بس.

الترتيب: الإجمالي الفرعي = مجموع (كمية × سعر) → الخصم → الضريبة على (الإجمالي - الخصم) → الإجمالي النهائي.
كل مبلغ بيتقرّب لخانتين عشريتين (ROUND_HALF_UP).
"""
from decimal import Decimal, ROUND_HALF_UP
from typing import Iterable, Optional, Tuple

from fastapi import HTTPException

_TWO = Decimal("0.01")
ADJUSTMENT_KINDS = ("amount", "percent")


def _d(value) -> Decimal:
    return Decimal(str(value if value is not None else 0))


def _q2(value: Decimal) -> Decimal:
    return value.quantize(_TWO, rounding=ROUND_HALF_UP)


def subtotal_of(lines: Iterable[Tuple[float, Optional[float]]]) -> float:
    """مجموع (كمية × سعر) مقرّب لخانتين. السعر الفاضي بيتحسب صفر."""
    total = sum((_d(round(q, 3)) * _d(p) for q, p in lines), Decimal(0))
    return float(_q2(total))


def normalize_adjustment(kind: Optional[str], value, label: str) -> Tuple[Optional[str], Optional[Decimal]]:
    """بيتحقق من (نوع، قيمة) الخصم أو الضريبة. القيمة صفر أو الفاضية معناها "مفيش" فبترجع (None, None)."""
    if value is None or value == "":
        return None, None
    if kind not in ADJUSTMENT_KINDS:
        raise HTTPException(status_code=400, detail=f"نوع {label} لازم يكون مبلغ ثابت أو نسبة %")
    v = _d(value)
    if v < 0:
        raise HTTPException(status_code=400, detail=f"قيمة {label} لا يمكن تكون سالبة")
    if v == 0:
        return None, None
    if kind == "percent" and v > 100:
        raise HTTPException(status_code=400, detail=f"نسبة {label} لا يمكن تكون أكبر من 100%")
    return kind, v


def compute_totals(lines, discount_type=None, discount_value=None, tax_type=None, tax_value=None) -> dict:
    """lines: [(quantity, unit_price)]. بترجع كل القيم اللي بتتخزن في الفاتورة (floats مقرّبة لخانتين)."""
    subtotal = _q2(sum((_d(round(q, 3)) * _d(p) for q, p in lines), Decimal(0)))
    d_type, d_val = normalize_adjustment(discount_type, discount_value, "الخصم")
    t_type, t_val = normalize_adjustment(tax_type, tax_value, "الضريبة")

    discount = Decimal(0)
    if d_type == "percent":
        discount = _q2(subtotal * d_val / 100)
    elif d_type == "amount":
        discount = _q2(d_val)
    if discount > subtotal:
        raise HTTPException(status_code=400, detail="الخصم أكبر من إجمالي الفاتورة")

    base = subtotal - discount
    tax = Decimal(0)
    if t_type == "percent":
        tax = _q2(base * t_val / 100)
    elif t_type == "amount":
        tax = _q2(t_val)

    return {
        "subtotal_amount": float(subtotal),
        "discount_type": d_type, "discount_value": float(_q2(d_val)) if d_val is not None else None,
        "discount_amount": float(discount),
        "tax_type": t_type, "tax_value": float(_q2(t_val)) if t_val is not None else None,
        "tax_amount": float(tax),
        "grand_total": float(_q2(base + tax)),
    }

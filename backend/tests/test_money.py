from decimal import Decimal

import pytest

from app.money import q2, q3, to_decimal


@pytest.mark.parametrize("raw,expected", [
    (0.1, "0.100"), (0.0005, "0.001"), (0.0004, "0.000"), (2.0005, "2.001"), ("1.2345", "1.235"),
    (25, "25.000"), (None, "0.000"), (Decimal("0.0015"), "0.002"),
])
def test_q3_half_up(raw, expected):
    assert str(q3(raw)) == expected


@pytest.mark.parametrize("raw,expected", [(2.675, "2.68"), (1.005, "1.01"), (0.125, "0.13"), (10, "10.00")])
def test_q2_half_up_not_bankers_or_binary(raw, expected):
    assert str(q2(raw)) == expected  # round(2.675, 2) بتدي 2.67 في Python - هنا 2.68


def test_to_decimal_uses_decimal_repr_of_float():
    assert to_decimal(0.1) + to_decimal(0.2) == Decimal("0.3")

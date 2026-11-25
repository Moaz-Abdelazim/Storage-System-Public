from decimal import Decimal

import pytest
from sqlalchemy.exc import DBAPIError

from app import cli, models
from app.services import inventory
from app.services.inventory import InsufficientStock


@pytest.fixture()
def stock(db):
    """منتج + موقعين جاهزين."""
    wh = models.Warehouse(code="W1", name="مخزن")
    db.add(wh)
    db.flush()
    a = models.Location(warehouse_id=wh.id, code="A", name="أ", type="rack")
    b = models.Location(warehouse_id=wh.id, code="B", name="ب", type="rack")
    p = models.Product(name="منتج", price=5, quantity=0, min_quantity=0)
    db.add_all([a, b, p])
    db.commit()
    return p.id, a.id, b.id


def _state(db, pid):
    db.expire_all()
    total = db.get(models.Product, pid).quantity
    rows = {pl.location_id: pl.quantity for pl in db.query(models.ProductLocation).filter_by(product_id=pid)}
    return total, rows


def test_add_creates_row_and_increments_total(db, stock):
    pid, a, _ = stock
    inventory.add_stock(db, pid, a, 10)
    inventory.add_stock(db, pid, a, 0.5)
    db.commit()
    assert _state(db, pid) == (10.5, {a: 10.5})


def test_decimal_precision_no_drift(db, stock):
    pid, a, _ = stock
    for _ in range(10):
        inventory.add_stock(db, pid, a, 0.1)
    for _ in range(10):
        inventory.remove_stock(db, pid, a, 0.1)
    db.commit()
    assert _state(db, pid) == (0, {})  # من غير 0.0000000001 متبقية، والصف الصفري اتشال


def test_remove_exact_balance_deletes_row(db, stock):
    pid, a, _ = stock
    inventory.add_stock(db, pid, a, 3)
    inventory.remove_stock(db, pid, a, 3)
    db.commit()
    assert _state(db, pid) == (0, {})


def test_remove_more_than_available_raises_and_changes_nothing(db, stock):
    pid, a, _ = stock
    inventory.add_stock(db, pid, a, 3)
    db.commit()
    with pytest.raises(InsufficientStock) as e:
        inventory.remove_stock(db, pid, a, 3.001)
    assert e.value.available == 3 and "المتاحة" in e.value.message
    db.rollback()
    assert _state(db, pid) == (3, {a: 3})


def test_remove_from_empty_location_raises(db, stock):
    pid, a, _ = stock
    with pytest.raises(InsufficientStock) as e:
        inventory.remove_stock(db, pid, a, 1)
    assert e.value.available == 0


@pytest.mark.parametrize("bad", [0, -1, 0.0004])
def test_non_positive_quantity_rejected(db, stock, bad):
    pid, a, _ = stock
    with pytest.raises(ValueError):
        inventory.add_stock(db, pid, a, bad)
    with pytest.raises(ValueError):
        inventory.remove_stock(db, pid, a, bad)


def test_transfer_keeps_total_and_moves_between_locations(db, stock):
    pid, a, b = stock
    inventory.add_stock(db, pid, a, 10)
    inventory.transfer_stock(db, pid, a, b, 4)
    inventory.transfer_stock(db, pid, a, b, 6)
    db.commit()
    assert _state(db, pid) == (10, {b: 10})


def test_transfer_insufficient_and_same_location(db, stock):
    pid, a, b = stock
    inventory.add_stock(db, pid, a, 1)
    with pytest.raises(InsufficientStock):
        inventory.transfer_stock(db, pid, a, b, 2)
    with pytest.raises(ValueError):
        inventory.transfer_stock(db, pid, a, a, 1)


def test_set_location_quantity_updates_total_by_delta(db, stock):
    pid, a, b = stock
    inventory.add_stock(db, pid, a, 5)
    inventory.add_stock(db, pid, b, 7)
    assert inventory.set_location_quantity(db, pid, a, 9) == (Decimal("5.000"), Decimal("9.000"))
    assert inventory.set_location_quantity(db, pid, b, 0)[1] == 0
    db.commit()
    assert _state(db, pid) == (9, {a: 9})


def test_replace_locations(db, stock):
    pid, a, b = stock
    inventory.add_stock(db, pid, a, 5)
    old, new = inventory.replace_locations(db, pid, {b: Decimal("2.5")})
    db.commit()
    assert (old, new) == (5, Decimal("2.5")) and _state(db, pid) == (2.5, {b: 2.5})


def test_db_check_constraint_blocks_negative(db, stock):
    pid, a, _ = stock
    db.add(models.ProductLocation(product_id=pid, location_id=a, quantity=-1))
    with pytest.raises(DBAPIError):  # SQLite: IntegrityError، MySQL: OperationalError(3819)
        db.flush()
    db.rollback()


def test_check_stock_detects_drift(db, stock):
    pid, a, _ = stock
    inventory.add_stock(db, pid, a, 5)
    db.commit()
    assert cli.check_stock(db) == []
    db.query(models.Product).filter_by(id=pid).update({"quantity": 9})
    db.commit()
    problems = cli.check_stock(db)
    assert len(problems) == 1 and problems[0]["product_quantity"] == 9 and problems[0]["locations_sum"] == 5


def test_with_retry_retries_only_deadlocks(db):
    from sqlalchemy.exc import OperationalError

    class Orig(Exception):
        pass

    calls = []

    def flaky():
        calls.append(1)
        if len(calls) < 3:
            raise OperationalError("x", {}, Orig(1213, "Deadlock found"))
        return "ok"

    assert inventory.with_retry(db, flaky, base_delay=0) == "ok" and len(calls) == 3

    def fatal():
        raise OperationalError("x", {}, Orig(1045, "Access denied"))

    with pytest.raises(OperationalError):
        inventory.with_retry(db, fatal, base_delay=0)

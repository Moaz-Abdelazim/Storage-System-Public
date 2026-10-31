"""
تزامن حقيقي (threads + sessions منفصلة). بيشتغل على SQLite وعلى MySQL 8 (TEST_DATABASE_URL).
الاختبارات دي هي اللي بتثبت إن الرصيد عمره ما بيبقى سالب ولا بيتفرق عن مجموع الأماكن.
"""
import threading

from app import models

from .helpers import get_product, location_qty, make_location, make_product, make_warehouse

THREADS = 8


def _run_parallel(clients, fn):
    barrier, results = threading.Barrier(len(clients)), []

    def go(i, c):
        barrier.wait()
        results.append(fn(i, c))

    ts = [threading.Thread(target=go, args=(i, c)) for i, c in enumerate(clients)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    return results


def _balances(session_factory, product_id):
    db = session_factory()
    try:
        total = db.query(models.Product.quantity).filter_by(id=product_id).scalar()
        locs = sum(pl.quantity for pl in db.query(models.ProductLocation).filter_by(product_id=product_id))
        return total, round(locs, 3)
    finally:
        db.close()


def test_concurrent_stock_out_never_goes_negative(admin, login, session_factory):
    wh = make_warehouse(admin)
    loc = make_location(admin, warehouse_id=wh["id"])
    prod = make_product(admin, price=10, locations=[(loc["id"], 10)])
    clients = [login("admin1", create=False) for _ in range(THREADS)]
    codes = _run_parallel(clients, lambda i, c: c.post(
        "/api/stock/out", json={"items": [{"product_id": prod["id"], "quantity": 5, "location_id": loc["id"]}]}).status_code)
    assert sorted(set(codes)) == [200, 400] and codes.count(200) == 2, codes
    assert _balances(session_factory, prod["id"]) == (0, 0)


def test_concurrent_mixed_in_out_keeps_totals_consistent(admin, login, session_factory):
    wh = make_warehouse(admin)
    a = make_location(admin, warehouse_id=wh["id"], code="A1")
    prod = make_product(admin, price=1, locations=[(a["id"], 50)])
    clients = [login("admin1", create=False) for _ in range(THREADS)]

    def op(i, c):
        if i % 2 == 0:
            return c.post("/api/stock/in", json={"items": [{"product_id": prod["id"], "quantity": 3, "location_id": a["id"]}]}).status_code
        return c.post("/api/stock/out", json={"items": [{"product_id": prod["id"], "quantity": 2, "location_id": a["id"]}]}).status_code

    codes = _run_parallel(clients, op)
    assert set(codes) == {200}, codes
    assert _balances(session_factory, prod["id"]) == (50 + 4 * 3 - 4 * 2, 50 + 4 * 3 - 4 * 2)


def test_concurrent_opposite_transfers_do_not_deadlock(admin, login, session_factory):
    wh = make_warehouse(admin)
    a = make_location(admin, warehouse_id=wh["id"], code="A1")
    b = make_location(admin, warehouse_id=wh["id"], code="B1")
    prod = make_product(admin, locations=[(a["id"], 20), (b["id"], 20)])
    clients = [login("admin1", create=False) for _ in range(THREADS)]

    def op(i, c):
        src, dst = (a, b) if i % 2 == 0 else (b, a)
        return c.post("/api/locations/transfer", json={
            "product_id": prod["id"], "from_location_id": src["id"], "to_location_id": dst["id"], "quantity": 1}).status_code

    codes = _run_parallel(clients, op)
    assert set(codes) == {200}, codes
    got = get_product(admin, prod["id"])
    assert got["quantity"] == 40 and location_qty(got, a["id"]) + location_qty(got, b["id"]) == 40


def test_concurrent_multi_product_invoices_opposite_order_no_deadlock(admin, login, session_factory):
    wh = make_warehouse(admin)
    loc = make_location(admin, warehouse_id=wh["id"])
    p1 = make_product(admin, "p1", locations=[(loc["id"], 100)])
    p2 = make_product(admin, "p2", locations=[(loc["id"], 100)])
    clients = [login("admin1", create=False) for _ in range(THREADS)]

    def op(i, c):
        ids = [p1["id"], p2["id"]] if i % 2 == 0 else [p2["id"], p1["id"]]
        return c.post("/api/stock/out", json={"items": [
            {"product_id": pid, "quantity": 1, "location_id": loc["id"]} for pid in ids]}).status_code

    codes = _run_parallel(clients, op)
    assert set(codes) == {200}, codes
    assert _balances(session_factory, p1["id"]) == (92, 92) and _balances(session_factory, p2["id"]) == (92, 92)


def test_concurrent_adjust_does_not_lose_parallel_movements(admin, login, session_factory):
    """جرد (set) بالتوازي مع صرف: الإجمالي لازم يفضل = مجموع الأماكن مهما كان الترتيب."""
    wh = make_warehouse(admin)
    loc = make_location(admin, warehouse_id=wh["id"])
    prod = make_product(admin, locations=[(loc["id"], 30)])
    clients = [login("admin1", create=False) for _ in range(4)]

    def op(i, c):
        if i == 0:
            return c.post(f"/api/products/{prod['id']}/adjust-quantity", json={"location_id": loc["id"], "new_quantity": 40}).status_code
        return c.post("/api/stock/out", json={"items": [{"product_id": prod["id"], "quantity": 1, "location_id": loc["id"]}]}).status_code

    codes = _run_parallel(clients, op)
    assert set(codes) <= {200, 400}, codes
    total, locs = _balances(session_factory, prod["id"])
    assert total == locs and total >= 0

"""اختبارات خط الأساس لدورة المخزون (وارد/صادر/نقل/تعليق) - بتثبّت السلوك الحالي قبل أي تعديل."""
from .helpers import get_product, location_qty, make_location, make_product, make_warehouse


def _setup(c):
    wh = make_warehouse(c)
    loc = make_location(c, warehouse_id=wh["id"], code="A1")
    return wh, loc


def test_stock_in_exact_quantity_no_float_drift(admin):
    wh, loc = _setup(admin)
    p = make_product(admin, "كمية دقيقة")
    for q in (25, 0.1, 0.2):
        r = admin.post("/api/stock/in", json={"items": [{"product_id": p["id"], "quantity": q, "location_id": loc["id"]}]})
        assert r.status_code == 200, r.text
    got = get_product(admin, p["id"])
    assert got["quantity"] == 25.3
    assert location_qty(got, loc["id"]) == 25.3


def test_stock_in_to_warehouse_default_location(admin):
    wh, loc = _setup(admin)
    assert admin.put(f"/api/locations/warehouses/{wh['id']}/default-location", json={"location_id": loc["id"]}).status_code == 200
    p = make_product(admin)
    r = admin.post("/api/stock/in", json={"items": [{"product_id": p["id"], "quantity": 7, "warehouse_id": wh["id"]}]})
    assert r.status_code == 200, r.text
    assert location_qty(get_product(admin, p["id"]), loc["id"]) == 7


def test_stock_in_without_any_location_is_rejected(admin):
    p = make_product(admin)
    r = admin.post("/api/stock/in", json={"items": [{"product_id": p["id"], "quantity": 1}]})
    assert r.status_code == 400


def test_stock_out_deducts_and_uses_product_price(admin):
    _, loc = _setup(admin)
    p = make_product(admin, price=10, locations=[(loc["id"], 10)])
    r = admin.post("/api/stock/out", json={"items": [{"product_id": p["id"], "quantity": 4, "location_id": loc["id"], "unit_price": 999}]})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["items"][0]["unit_price"] == 10  # unit_price القادم من العميل بيتتجاهل
    assert body["grand_total"] == 40
    got = get_product(admin, p["id"])
    assert got["quantity"] == 6 and location_qty(got, loc["id"]) == 6


def test_stock_out_more_than_available_rejected(admin):
    _, loc = _setup(admin)
    p = make_product(admin, locations=[(loc["id"], 3)])
    r = admin.post("/api/stock/out", json={"items": [{"product_id": p["id"], "quantity": 4, "location_id": loc["id"]}]})
    assert r.status_code == 400
    assert get_product(admin, p["id"])["quantity"] == 3  # مفيش أثر جانبي


def test_stock_out_invoice_is_atomic_when_one_line_fails(admin):
    _, loc = _setup(admin)
    a = make_product(admin, "أ", locations=[(loc["id"], 10)])
    b = make_product(admin, "ب", locations=[(loc["id"], 1)])
    r = admin.post("/api/stock/out", json={"items": [
        {"product_id": a["id"], "quantity": 5, "location_id": loc["id"]},
        {"product_id": b["id"], "quantity": 2, "location_id": loc["id"]},
    ]})
    assert r.status_code == 400
    assert get_product(admin, a["id"])["quantity"] == 10


def test_discount_and_tax_totals(admin):
    _, loc = _setup(admin)
    p = make_product(admin, price=100, locations=[(loc["id"], 10)])
    r = admin.post("/api/stock/out", json={
        "items": [{"product_id": p["id"], "quantity": 2, "location_id": loc["id"]}],
        "discount_type": "percent", "discount_value": 10, "tax_type": "percent", "tax_value": 14,
    })
    assert r.status_code == 200, r.text
    b = r.json()
    assert (b["subtotal"], b["discount_amount"], b["tax_amount"], b["grand_total"]) == (200, 20, 25.2, 205.2)


def test_transfer_between_locations_keeps_total(admin):
    wh, a = _setup(admin)
    b = make_location(admin, warehouse_id=wh["id"], code="B1", name="رف ب")
    p = make_product(admin, locations=[(a["id"], 10)])
    r = admin.post("/api/locations/transfer", json={"product_id": p["id"], "from_location_id": a["id"], "to_location_id": b["id"], "quantity": 4})
    assert r.status_code == 200, r.text
    got = get_product(admin, p["id"])
    assert got["quantity"] == 10 and location_qty(got, a["id"]) == 6 and location_qty(got, b["id"]) == 4


def test_transfer_more_than_available_rejected(admin):
    wh, a = _setup(admin)
    b = make_location(admin, warehouse_id=wh["id"], code="B1", name="رف ب")
    p = make_product(admin, locations=[(a["id"], 2)])
    r = admin.post("/api/locations/transfer", json={"product_id": p["id"], "from_location_id": a["id"], "to_location_id": b["id"], "quantity": 3})
    assert r.status_code == 400


def test_held_invoice_lifecycle(admin):
    _, loc = _setup(admin)
    p = make_product(admin, price=5, locations=[(loc["id"], 10)])
    item = {"product_id": p["id"], "quantity": 3, "location_id": loc["id"]}
    inv = admin.post("/api/stock/out", json={"items": [item], "status": "held"}).json()
    assert inv["status"] == "held" and get_product(admin, p["id"])["quantity"] == 7  # المعلّقة بتخصم فعلًا
    # تعديل المعلّقة
    upd = admin.put(f"/api/stock/out/{inv['id']}", json={"items": [{**item, "quantity": 5}]})
    assert upd.status_code == 200 and get_product(admin, p["id"])["quantity"] == 5
    # إتمامها
    assert admin.post(f"/api/stock/out/{inv['id']}/complete").json()["status"] == "completed"
    # المقفولة مينفعش تتعدّل أو تتمسح
    assert admin.delete(f"/api/stock/out/{inv['id']}").status_code == 409
    assert admin.put(f"/api/stock/out/{inv['id']}", json={"items": [item]}).status_code == 409


def test_delete_held_invoice_restores_stock(admin):
    _, loc = _setup(admin)
    p = make_product(admin, locations=[(loc["id"], 10)])
    inv = admin.post("/api/stock/out", json={"items": [{"product_id": p["id"], "quantity": 6, "location_id": loc["id"]}], "status": "held"}).json()
    assert admin.delete(f"/api/stock/out/{inv['id']}").status_code == 200
    got = get_product(admin, p["id"])
    assert got["quantity"] == 10 and location_qty(got, loc["id"]) == 10


def test_inactive_product_cannot_be_used(admin):
    _, loc = _setup(admin)
    p = make_product(admin, locations=[(loc["id"], 5)])
    assert admin.patch(f"/api/products/{p['id']}/active", json={"is_active": False}).status_code == 200
    r = admin.post("/api/stock/out", json={"items": [{"product_id": p["id"], "quantity": 1, "location_id": loc["id"]}]})
    assert r.status_code == 400


def test_totals_consistent_after_mixed_operations(admin, db):
    """products.quantity لازم يساوي مجموع product_locations بعد أي خليط عمليات."""
    from app import models
    from sqlalchemy import func

    wh, a = _setup(admin)
    b = make_location(admin, warehouse_id=wh["id"], code="B1", name="رف ب")
    p = make_product(admin, locations=[(a["id"], 20)])
    admin.post("/api/stock/in", json={"items": [{"product_id": p["id"], "quantity": 5, "location_id": b["id"]}]})
    admin.post("/api/stock/out", json={"items": [{"product_id": p["id"], "quantity": 8, "location_id": a["id"]}]})
    admin.post("/api/locations/transfer", json={"product_id": p["id"], "from_location_id": a["id"], "to_location_id": b["id"], "quantity": 2})
    db.expire_all()
    prod = db.get(models.Product, p["id"])
    total = db.query(func.coalesce(func.sum(models.ProductLocation.quantity), 0)).filter_by(product_id=p["id"]).scalar()
    assert prod.quantity == total == 17

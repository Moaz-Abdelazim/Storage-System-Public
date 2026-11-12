"""مؤشرات تشغيلية: قيمة المخزون بالتكلفة (آخر سعر شراء) وبسعر البيع، ودقة الجرد، ونسبة تحت الحد."""
from .helpers import make_location, make_product, make_warehouse


def _receive(c, p, loc, qty, price):
    r = c.post("/api/stock/in", json={"items": [{"product_id": p["id"], "quantity": qty, "unit_price": price, "location_id": loc["id"]}]})
    assert r.status_code == 200, r.text


def test_stock_valuation_cost_uses_last_purchase_price_and_reports_coverage(admin):
    wh = make_warehouse(admin)
    a = make_location(admin, warehouse_id=wh["id"], code="A1")
    p1 = make_product(admin, "له-تكلفة", price=20)
    p2 = make_product(admin, "بدون-تكلفة", price=100, locations=[(a["id"], 3)])
    _receive(admin, p1, a, 4, 6)       # شراء أول بـ 6
    _receive(admin, p1, a, 6, 8)       # آخر شراء بـ 8 (هو المعتمد)  -> الرصيد 10
    v = admin.get("/api/analytics/stock-valuation").json()
    assert v["cost_value_basis"] == "last_purchase_price" and v["sale_value_basis"] == "sale_price"
    assert v["products_in_stock"] == 2 and v["costed_products"] == 1 and v["uncosted_products"] == 1
    assert v["cost_value"] == 80                      # 10 × 8 (المنتج الثاني ملوش سعر شراء: مش بيتحسب ولا بيتستنتج من سعر البيع)
    assert v["sale_value"] == 10 * 20 + 3 * 100
    assert v["costed_sale_value"] == 200 and v["uncosted_sale_value"] == 300 and v["potential_margin"] == 120
    assert v["coverage_pct"] == 50


def test_stock_valuation_empty_system(admin):
    v = admin.get("/api/analytics/stock-valuation").json()
    assert v["cost_value"] == 0 and v["sale_value"] == 0 and v["coverage_pct"] == 0 and v["products_in_stock"] == 0


def _approved_session(c, wh, counts, note="ج"):
    st = c.post("/api/stocktakes", json={"warehouse_id": wh["id"], "note": note}).json()
    lines = c.get(f"/api/stocktakes/{st['id']}", params={"page_size": 200}).json()["items"]
    by = {l["product_name"]: l["line_id"] for l in lines}
    c.put(f"/api/stocktakes/{st['id']}/counts", json={"counts": [{"line_id": by[k], "counted_qty": v} for k, v in counts.items()]})
    c.post(f"/api/stocktakes/{st['id']}/submit")
    assert c.post(f"/api/stocktakes/{st['id']}/approve").status_code == 200
    return st


def test_stocktake_accuracy_and_variance(admin):
    wh = make_warehouse(admin)
    a = make_location(admin, warehouse_id=wh["id"], code="A1")
    for name, price, q in (("س1", 10, 10), ("س2", 4, 10), ("س3", 5, 10), ("س4", 1, 10)):
        make_product(admin, name, price=price, locations=[(a["id"], q)])
    _approved_session(admin, wh, {"س1": 10, "س2": 10, "س3": 8, "س4": 12})     # مطابق ×2، عجز 2×5=10، زيادة 2×1=2
    r = admin.get("/api/analytics/stocktake-accuracy", params={"days": 30}).json()
    assert (r["sessions"], r["counted_lines"], r["accurate_lines"], r["variance_lines"], r["accuracy_pct"]) == (1, 4, 2, 2, 50.0)
    assert (r["shortage_qty"], r["surplus_qty"], r["shortage_value"], r["surplus_value"], r["net_value"]) == (2, 2, 10, 2, -8)
    assert r["value_basis"] == "sale_price"
    assert r["by_warehouse"][0]["warehouse_code"] == "W1" and r["by_warehouse"][0]["accuracy_pct"] == 50.0
    assert r["by_user"][0]["username"] == "admin1" and r["by_user"][0]["sessions"] == 1
    assert r["by_location"][0]["location_code"] == "W1-A1" and r["by_location"][0]["net_value"] == -8


def test_stocktake_accuracy_ignores_unapproved_and_uncounted(admin):
    wh = make_warehouse(admin)
    a = make_location(admin, warehouse_id=wh["id"], code="A1")
    make_product(admin, "س1", price=10, locations=[(a["id"], 10)])
    admin.post("/api/stocktakes", json={"warehouse_id": wh["id"]})                  # مسودة: مش محسوبة
    r = admin.get("/api/analytics/stocktake-accuracy").json()
    assert r["sessions"] == 0 and r["accuracy_pct"] is None and r["by_session"] == []


def test_kpis_low_stock_percentage(admin):
    wh = make_warehouse(admin)
    a = make_location(admin, warehouse_id=wh["id"], code="A1")
    make_product(admin, "تحت", price=1, min_quantity=5, locations=[(a["id"], 2)])
    make_product(admin, "تمام", price=1, min_quantity=5, locations=[(a["id"], 50)])
    make_product(admin, "نفد", price=1, min_quantity=5)
    make_product(admin, "تمام2", price=1, min_quantity=1, locations=[(a["id"], 9)])
    k = admin.get("/api/analytics/kpis").json()
    k = k.get("kpis", k)
    assert k["active_products"] == 4 and k["low_stock_count"] == 2 and k["low_stock_pct"] == 50.0 and k["out_of_stock_pct"] == 25.0


def test_adjustments_report_includes_location_and_stored_value(admin):
    wh = make_warehouse(admin)
    a = make_location(admin, warehouse_id=wh["id"], code="A1")
    make_product(admin, "س1", price=10, locations=[(a["id"], 10)])
    st = _approved_session(admin, wh, {"س1": 7})
    admin.put  # noqa: B018
    rows = admin.get("/api/analytics/adjustments", params={"days": 30}).json()["items"]
    row = next(r for r in rows if r["stocktake_id"] == st["id"])
    assert row["location_code"] == "W1-A1" and row["difference"] == -3 and row["value_impact"] == -30

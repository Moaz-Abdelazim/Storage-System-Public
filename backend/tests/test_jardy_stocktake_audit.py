"""الجرد: سجل تسوية لكل سطر، منع الاعتماد/التعديل بعد الاعتماد، الإلغاء بسبب، والاعتماد المتزامن."""
import threading

from app import models

from .helpers import get_product, location_qty, make_location, make_product, make_warehouse


def _lines(c, sid):
    return c.get(f"/api/stocktakes/{sid}", params={"page_size": 200}).json()


def _session(c, cnt):
    """مخزن بموقعين ومنتجين: جرد-1 (10 في A1) و جرد-2 (5 في A1 + 5 في B1). cnt = {(اسم، كود الموقع): العدّ}."""
    wh = make_warehouse(c)
    a, b = make_location(c, warehouse_id=wh["id"], code="A1"), make_location(c, warehouse_id=wh["id"], code="B1", name="رف ب")
    p1 = make_product(c, "جرد-1", price=10, locations=[(a["id"], 10)])
    p2 = make_product(c, "جرد-2", price=4, locations=[(a["id"], 5), (b["id"], 5)])
    st = c.post("/api/stocktakes", json={"warehouse_id": wh["id"], "note": "جرد"}).json()
    by = {(l["product_name"], l["location_code"]): l["line_id"] for l in _lines(c, st["id"])["items"]}
    counts = [{"line_id": by[k], "counted_qty": v} for k, v in cnt.items()]
    assert c.put(f"/api/stocktakes/{st['id']}/counts", json={"counts": counts}).status_code == 200
    return wh, a, b, p1, p2, st, by


def test_approve_writes_one_audit_row_per_line_with_value(admin):
    wh, a, b, p1, p2, st, _ = _session(admin, {("جرد-1", "W1-A1"): 7, ("جرد-2", "W1-A1"): 6})   # عجز 3 (×10) وزيادة 1 (×4)
    admin.post(f"/api/stocktakes/{st['id']}/submit")
    r = admin.post(f"/api/stocktakes/{st['id']}/approve")
    assert r.status_code == 200 and r.json()["lines_adjusted"] == 2 and r.json()["uncounted_lines"] == 1

    rows = {x["product_name"]: x for x in admin.get(f"/api/stocktakes/{st['id']}/adjustments").json()["items"]}
    assert set(rows) == {"جرد-1", "جرد-2"}                    # السطر غير المعدود (جرد-2 في B1) ما اتسجلوش
    r1, r2 = rows["جرد-1"], rows["جرد-2"]
    assert (r1["location_code"], r1["old_quantity"], r1["new_quantity"], r1["difference"], r1["unit_price"], r1["value_difference"]) == ("W1-A1", 10, 7, -3, 10, -30)
    assert (r2["location_code"], r2["old_quantity"], r2["new_quantity"], r2["difference"], r2["value_difference"]) == ("W1-A1", 5, 6, 1, 4)
    for x in rows.values():
        assert x["stocktake_id"] == st["id"] and x["changed_by"] == "admin1" and x["reason"] == f"جرد #{st['id']}" and x["date"]
    assert get_product(admin, p1["id"])["quantity"] == 7


def test_adjustment_values_hidden_without_finance_view(admin, login):
    wh, a, b, p1, p2, st, _ = _session(admin, {("جرد-1", "W1-A1"): 7})
    admin.post(f"/api/stocktakes/{st['id']}/submit"); admin.post(f"/api/stocktakes/{st['id']}/approve")
    c = login("counter", ["stocktake.manage"])
    item = c.get(f"/api/stocktakes/{st['id']}/adjustments").json()["items"][0]
    assert item["difference"] == -3 and item["unit_price"] is None and item["value_difference"] is None
    assert login("nobody", ["products.view"]).get(f"/api/stocktakes/{st['id']}/adjustments").status_code == 403


def test_nothing_can_change_after_approval_and_double_approve_is_rejected(admin):
    wh, a, b, p1, p2, st, by = _session(admin, {("جرد-1", "W1-A1"): 7})
    admin.post(f"/api/stocktakes/{st['id']}/submit")
    assert admin.post(f"/api/stocktakes/{st['id']}/approve").status_code == 200
    again = admin.post(f"/api/stocktakes/{st['id']}/approve")
    assert again.status_code == 409 and "بالفعل" in again.json()["detail"]
    assert get_product(admin, p1["id"])["quantity"] == 7                                  # الفرق اتطبّق مرة واحدة بس
    assert admin.put(f"/api/stocktakes/{st['id']}/counts", json={"counts": [{"line_id": by[("جرد-1", "W1-A1")], "counted_qty": 1}]}).status_code == 409
    assert admin.post(f"/api/stocktakes/{st['id']}/submit").status_code == 409
    assert admin.post(f"/api/stocktakes/{st['id']}/cancel", json={"reason": "متأخر"}).status_code == 409
    assert get_product(admin, p1["id"])["quantity"] == 7


def test_cancel_stores_reason_and_blocks_further_actions(admin):
    wh, a, b, p1, p2, st, by = _session(admin, {("جرد-1", "W1-A1"): 7})
    r = admin.post(f"/api/stocktakes/{st['id']}/cancel", json={"reason": "  عدّ غلط  "})
    assert r.status_code == 200 and r.json()["status"] == "cancelled"
    h = _lines(admin, st["id"])["stocktake"]
    assert (h["status"], h["cancel_reason"], h["cancelled_by"]) == ("cancelled", "عدّ غلط", "admin1") and h["cancelled_at"]
    assert admin.post(f"/api/stocktakes/{st['id']}/submit").status_code == 409
    assert admin.put(f"/api/stocktakes/{st['id']}/counts", json={"counts": [{"line_id": by[("جرد-1", "W1-A1")], "counted_qty": 1}]}).status_code == 409
    assert get_product(admin, p1["id"])["quantity"] == 10                                  # مفيش أي تسوية
    audit = admin.get("/api/audit-log", params={"entity_type": "stocktake", "entity_id": st["id"]}).json()["items"]
    assert "cancel" in [x["action"] for x in audit]


def test_cancel_without_reason_is_allowed_and_oversized_reason_rejected(admin):
    wh, a, b, p1, p2, st, _ = _session(admin, {})
    assert admin.post(f"/api/stocktakes/{st['id']}/cancel", json={"reason": "س" * 301}).status_code == 422
    assert admin.post(f"/api/stocktakes/{st['id']}/cancel").status_code == 200
    assert _lines(admin, st["id"])["stocktake"]["cancel_reason"] is None


def test_line_status_in_detail(admin):
    wh, a, b, p1, p2, st, _ = _session(admin, {("جرد-1", "W1-A1"): 7, ("جرد-2", "W1-A1"): 5, ("جرد-2", "W1-B1"): 9})
    got = {(l["product_name"], l["location_code"]): l["line_status"] for l in _lines(admin, st["id"])["items"]}
    assert got == {("جرد-1", "W1-A1"): "shortage", ("جرد-2", "W1-A1"): "match", ("جرد-2", "W1-B1"): "surplus"}
    assert _lines(admin, st["id"])["stocktake"]["created_by"] == "admin1"


def test_concurrent_approve_applies_once(admin, login, session_factory):
    wh, a, b, p1, p2, st, _ = _session(admin, {("جرد-1", "W1-A1"): 7})
    admin.post(f"/api/stocktakes/{st['id']}/submit")
    clients = [login("admin1", create=False) for _ in range(6)]
    barrier, codes = threading.Barrier(len(clients)), []

    def go(c):
        barrier.wait()
        codes.append(c.post(f"/api/stocktakes/{st['id']}/approve").status_code)

    ts = [threading.Thread(target=go, args=(c,)) for c in clients]
    [t.start() for t in ts]; [t.join() for t in ts]
    assert codes.count(200) == 1 and all(c in (200, 409) for c in codes), codes
    g = get_product(admin, p1["id"])
    assert g["quantity"] == 7 and location_qty(g, a["id"]) == 7                            # الفرق اتطبّق مرة واحدة
    db = session_factory()
    try:
        assert db.query(models.QuantityAdjustment).filter_by(stocktake_id=st["id"]).count() == 1
    finally:
        db.close()

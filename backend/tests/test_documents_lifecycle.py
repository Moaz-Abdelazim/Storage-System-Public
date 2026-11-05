"""دورة إذن الصرف (quote → reserved → completed → voided) + الحجز (on_hand/reserved/available) + إلغاء الوارد + idempotency."""
import uuid

from app import models
from app.query_helpers import valid_invoice

from .helpers import get_product, location_qty, make_location, make_product, make_warehouse
from .test_concurrency import THREADS, _run_parallel


def _setup(c):
    wh = make_warehouse(c)
    return wh, make_location(c, warehouse_id=wh["id"], code="A1"), make_location(c, warehouse_id=wh["id"], code="B1", name="رف ب")


def _item(p, loc, qty):
    return {"product_id": p["id"], "quantity": qty, "location_id": loc["id"]}


def _doc(c, items, status="completed", **extra):
    r = c.post("/api/stock/out", json={"items": items, "status": status, **extra})
    assert r.status_code == 200, r.text
    return r.json()


def _bal(c, p):
    g = get_product(c, p["id"])
    return g["quantity"], g["reserved"], g["available"]


# ---- 1: عرض السعر لا يغير شيئًا (ويقبل كمية أكبر من المتاح) ----
def test_quote_changes_nothing_and_may_exceed_available(admin):
    _, a, _b = _setup(admin)
    p = make_product(admin, price=10, locations=[(a["id"], 5)])
    q = _doc(admin, [_item(p, a, 50)], "quote")
    assert q["status"] == "quote" and q["subtotal"] == 500
    assert _bal(admin, p) == (5, 0, 5)
    pr = admin.get(f"/api/stock/out/{q['id']}/print").json()
    assert pr["labels"]["banner"] == "عرض سعر — غير مؤثر على المخزون" and pr["labels"]["notice"] == "عرض سعر — مستند غير ضريبي"


def test_quote_keeps_snapshot_price(admin):
    _, a, _b = _setup(admin)
    p = make_product(admin, price=10, locations=[(a["id"], 5)])
    q = _doc(admin, [_item(p, a, 2)], "quote")
    assert admin.put(f"/api/products/{p['id']}", json={"price": 99}).status_code == 200
    got = admin.get(f"/api/stock/out/{q['id']}").json()
    assert got["items"][0]["unit_price"] == 10 and got["subtotal"] == 20
    upd = admin.put(f"/api/stock/out/{q['id']}", json={"items": [_item(p, a, 3)]}).json()
    assert upd["items"][0]["unit_price"] == 10 and upd["subtotal"] == 30  # السطر القديم بيحتفظ بسعره


# ---- 2/3: الحجز بيقلل المتاح بس + الرفض بيذكر كل الأصناف ----
def test_reserve_only_reduces_available(admin):
    _, a, _b = _setup(admin)
    p = make_product(admin, locations=[(a["id"], 10)])
    q = _doc(admin, [_item(p, a, 3)], "quote")
    r = admin.post(f"/api/stock/out/{q['id']}/reserve")
    assert r.status_code == 200 and r.json()["status"] == "reserved"
    assert _bal(admin, p) == (10, 3, 7)
    g = get_product(admin, p["id"])
    assert g["locations"][0]["reserved"] == 3 and g["locations"][0]["available"] == 7


def test_reserve_over_available_lists_every_affected_item(admin):
    _, a, b = _setup(admin)
    p1 = make_product(admin, "صنف-أول", locations=[(a["id"], 2)])
    p2 = make_product(admin, "صنف-ثاني", locations=[(b["id"], 1)])
    r = admin.post("/api/stock/out", json={"status": "reserved", "items": [_item(p1, a, 5), _item(p2, b, 4)]})
    assert r.status_code == 400
    d = r.json()["detail"]
    assert "صنف-أول" in d and "صنف-ثاني" in d and "W1-A1" in d and "W1-B1" in d
    assert _bal(admin, p1) == (2, 0, 2) and _bal(admin, p2) == (1, 0, 1)  # مفيش أثر جانبي


def test_direct_out_cannot_consume_reserved_quantity(admin):
    _, a, b = _setup(admin)
    p = make_product(admin, locations=[(a["id"], 10)])
    _doc(admin, [_item(p, a, 8)], "reserved")
    assert admin.post("/api/stock/out", json={"items": [_item(p, a, 3)]}).status_code == 400  # المتاح 2 بس
    assert _doc(admin, [_item(p, a, 2)])["status"] == "completed"
    # النقل والتسوية كمان مينفعش ياكلوا المحجوز
    assert admin.post("/api/locations/transfer", json={"product_id": p["id"], "from_location_id": a["id"], "to_location_id": b["id"], "quantity": 1}).status_code == 400
    r = admin.post(f"/api/products/{p['id']}/adjust-quantity", json={"location_id": a["id"], "new_quantity": 5})
    assert r.status_code == 409 and "المحجوز" in r.json()["detail"]
    assert admin.post(f"/api/products/{p['id']}/adjust-quantity", json={"location_id": a["id"], "new_quantity": 8}).status_code == 200


# ---- 4: تعديل الحجز ----
def test_edit_reservation_releases_old_then_reserves_new(admin):
    _, a, b = _setup(admin)
    p = make_product(admin, locations=[(a["id"], 10), (b["id"], 4)])
    d = _doc(admin, [_item(p, a, 10)], "reserved")
    assert _bal(admin, p) == (14, 10, 4)
    # نفس الكمية كلها تتنقل لمكان تاني: لازم الحجز القديم يتفك الأول
    assert admin.put(f"/api/stock/out/{d['id']}", json={"items": [_item(p, b, 4), _item(p, a, 6)]}).status_code == 200
    g = get_product(admin, p["id"])
    assert (g["reserved"], g["available"]) == (10, 4)
    assert [(l["full_code"], l["reserved"]) for l in g["locations"]] == [("W1-A1", 6), ("W1-B1", 4)]
    # تعديل فاشل (أكتر من المتاح) مبيسيبش الحجز القديم مفكوك
    assert admin.put(f"/api/stock/out/{d['id']}", json={"items": [_item(p, a, 11)]}).status_code == 400
    assert _bal(admin, p) == (14, 10, 4)
    # التعديل بيحتفظ بكمية المستند نفسه كمتاحة
    assert admin.put(f"/api/stock/out/{d['id']}", json={"items": [_item(p, a, 10)]}).status_code == 200


# ---- 5: حجزان متزامنان ----
def test_concurrent_reservations_never_exceed_available(admin, login, session_factory):
    _, a, _b = _setup(admin)
    p = make_product(admin, locations=[(a["id"], 10)])
    clients = [login("admin1", create=False) for _ in range(THREADS)]
    codes = _run_parallel(clients, lambda i, c: c.post("/api/stock/out", json={"status": "reserved", "items": [_item(p, a, 5)]}).status_code)
    assert sorted(set(codes)) == [200, 400] and codes.count(200) == 2, codes
    assert _bal(admin, p) == (10, 10, 0)
    s = session_factory()
    try:
        pl = s.query(models.ProductLocation).filter_by(product_id=p["id"]).one()
        assert pl.reserved == 10 and pl.quantity == 10
    finally:
        s.close()


def test_concurrent_completion_of_same_reservation_happens_once(admin, login):
    _, a, _b = _setup(admin)
    p = make_product(admin, locations=[(a["id"], 10)])
    d = _doc(admin, [_item(p, a, 4)], "reserved")
    clients = [login("admin1", create=False) for _ in range(4)]
    codes = _run_parallel(clients, lambda i, c: c.post(f"/api/stock/out/{d['id']}/complete").status_code)
    assert codes.count(200) == 1 and codes.count(409) == 3, codes
    assert _bal(admin, p) == (6, 0, 6)


# ---- 6/7/8: إتمام / إلغاء حجز / إلغاء صرف مكتمل ----
def test_complete_reservation_deducts_on_hand_and_ends_reservation(admin):
    _, a, _b = _setup(admin)
    p = make_product(admin, locations=[(a["id"], 10)])
    d = _doc(admin, [_item(p, a, 4)], "reserved")
    done = admin.post(f"/api/stock/out/{d['id']}/complete").json()
    assert done["status"] == "completed" and done["completed_at"]
    g = get_product(admin, p["id"])
    assert (g["quantity"], g["reserved"], g["available"]) == (6, 0, 6) and location_qty(g, a["id"]) == 6


def test_void_reservation_and_release(admin):
    _, a, _b = _setup(admin)
    p = make_product(admin, locations=[(a["id"], 10)])
    d = _doc(admin, [_item(p, a, 4)], "reserved")
    assert admin.post(f"/api/stock/out/{d['id']}/release").json()["status"] == "quote"
    assert _bal(admin, p) == (10, 0, 10)
    assert admin.post(f"/api/stock/out/{d['id']}/reserve").status_code == 200
    r = admin.post(f"/api/stock/out/{d['id']}/void", json={"reason": "اتلغى"})
    assert r.json()["status"] == "voided" and _bal(admin, p) == (10, 0, 10)
    assert admin.post(f"/api/stock/out/{d['id']}/void", json={"reason": "تاني"}).status_code == 409


def test_void_completed_out_restores_same_locations(admin):
    _, a, b = _setup(admin)
    p = make_product(admin, locations=[(a["id"], 5), (b["id"], 5)])
    d = _doc(admin, [_item(p, a, 5), _item(p, b, 2)])
    assert _bal(admin, p) == (3, 0, 3)
    assert admin.post(f"/api/stock/out/{d['id']}/void", json={"reason": "ab"}).status_code == 422  # سبب قصير
    r = admin.post(f"/api/stock/out/{d['id']}/void", json={"reason": "مرتجع كامل"})
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "voided" and body["void_reason"] == "مرتجع كامل" and body["voided_by_name"] and body["voided_at"]
    g = get_product(admin, p["id"])
    assert g["quantity"] == 10 and location_qty(g, a["id"]) == 5 and location_qty(g, b["id"]) == 5


def test_void_with_recreate_makes_new_quote_without_movement(admin):
    _, a, _b = _setup(admin)
    p = make_product(admin, price=10, locations=[(a["id"], 10)])
    d = _doc(admin, [_item(p, a, 4)])
    r = admin.post(f"/api/stock/out/{d['id']}/void", json={"reason": "تصحيح", "recreate": True}).json()
    new = admin.get(f"/api/stock/out/{r['recreated_invoice_id']}").json()
    assert new["status"] == "quote" and new["copied_from_id"] == d["id"] and new["items"][0]["quantity"] == 4
    assert _bal(admin, p) == (10, 0, 10)
    again = admin.post(f"/api/stock/out/{d['id']}/recreate")
    assert again.status_code == 200 and again.json()["status"] == "quote"
    assert admin.post(f"/api/stock/out/{new['id']}/recreate").status_code == 409  # مش ملغي


# ---- 9: إلغاء الوارد ----
def test_void_stock_in_blocks_negative_and_lists_everything(admin):
    _, a, b = _setup(admin)
    p1, p2 = make_product(admin, "وارد-1"), make_product(admin, "وارد-2")
    inv = admin.post("/api/stock/in", json={"items": [
        {"product_id": p1["id"], "quantity": 10, "location_id": a["id"]},
        {"product_id": p2["id"], "quantity": 10, "location_id": b["id"]}]}).json()
    assert inv["status"] == "completed"
    _doc(admin, [_item(p1, a, 6)])
    _doc(admin, [_item(p2, b, 3)], "reserved")
    r = admin.post(f"/api/stock/in/{inv['id']}/void", json={"reason": "غلط في الإدخال"})
    assert r.status_code == 409
    d = r.json()["detail"]
    assert "وارد-1" in d and "وارد-2" in d and "W1-A1" in d and "W1-B1" in d
    assert _bal(admin, p1)[0] == 4 and _bal(admin, p2) == (10, 3, 7)  # مفيش أثر
    assert admin.get(f"/api/stock/in/{inv['id']}").json()["status"] == "completed"


def test_void_stock_in_success_reverses_same_locations(admin):
    _, a, _b = _setup(admin)
    p = make_product(admin)
    inv = admin.post("/api/stock/in", json={"items": [{"product_id": p["id"], "quantity": 7, "location_id": a["id"]}]}).json()
    r = admin.post(f"/api/stock/in/{inv['id']}/void", json={"reason": "اتسجل بالغلط"})
    assert r.status_code == 200 and r.json()["status"] == "voided"
    assert _bal(admin, p) == (0, 0, 0)
    assert admin.post(f"/api/stock/in/{inv['id']}/void", json={"reason": "تاني"}).status_code == 409


# ---- 10/11/12/13/14: التقارير ----
def _two_item_example(admin):
    _, a, _b = _setup(admin)
    pa = make_product(admin, "أ", price=500, locations=[(a["id"], 100)])
    pb = make_product(admin, "ب", price=405, locations=[(a["id"], 100)])
    items = [_item(pa, a, 1), _item(pb, a, 2)]  # 500 + 810 = 1310
    return a, pa, pb, items


def test_document_totals_example_1310_50_14pct(admin):
    a, pa, pb, items = _two_item_example(admin)
    d = _doc(admin, items, discount_type="amount", discount_value=50, tax_type="percent", tax_value=14)
    assert (d["subtotal"], d["discount_amount"], d["net_amount"], d["tax_amount"], d["grand_total"]) == (1310, 50, 1260, 176.4, 1436.4)
    p = _doc(admin, items, "quote", discount_type="percent", discount_value=10, tax_type="percent", tax_value=14)
    assert (p["subtotal"], p["discount_amount"], p["net_amount"], p["tax_amount"], p["grand_total"]) == (1310, 131, 1179, 165.06, 1344.06)
    assert admin.post("/api/stock/out", json={"items": items, "tax_type": "amount", "tax_value": 5}).status_code == 422  # ضريبة % فقط


def test_reports_count_completed_only_and_split_gross_discount_net_tax(admin):
    a, pa, pb, items = _two_item_example(admin)
    _doc(admin, items, discount_type="amount", discount_value=50, tax_type="percent", tax_value=14)   # completed
    _doc(admin, items, "quote")
    _doc(admin, items, "reserved")
    gone = _doc(admin, items)
    assert admin.post(f"/api/stock/out/{gone['id']}/void", json={"reason": "ملغي للتجربة"}).status_code == 200

    k = admin.get("/api/analytics/kpis", params={"days": 30}).json()
    k = k.get("kpis", k)
    assert (k["out_gross_value"], k["out_discount_value"], k["out_value"], k["tax_collected"]) == (1310, 50, 1260, 176.4)
    assert k["stock_value_basis"] == "sale_price"

    t = admin.get("/api/reports/out-totals").json()
    month = t["month"]
    assert (month["gross_sales"], month["discounts"], month["net_sales"], month["tax_collected"], month["grand_total"]) == (1310, 50, 1260, 176.4, 1436.4)
    assert month["quantity"] == 3

    o = admin.get("/api/dashboard/overview").json()
    sales = o.get("out_month") or o.get("out", {}).get("month") or {}
    assert not sales or sales["net_sales"] == 1260

    log = admin.get("/api/stock/log", params={"page_size": 50}).json()
    assert log["totals"]["out_invoices_count"] == 1
    assert (log["totals"]["out_total_value"], log["totals"]["out_discount_total"], log["totals"]["out_net_value"]) == (1310, 50, 1260)
    assert all(r.get("status", "completed") == "completed" for r in log["items"])
    with_void = admin.get("/api/stock/log", params={"page_size": 50, "include_voided": 1}).json()
    assert any(r.get("status") == "voided" for r in with_void["items"]) and with_void["totals"]["out_invoices_count"] == 1


def test_profitability_allocates_document_discount_across_lines(admin):
    a, pa, pb, items = _two_item_example(admin)
    _doc(admin, items, discount_type="amount", discount_value=50)
    _doc(admin, items, "quote")
    _doc(admin, items, "reserved")
    m = admin.get("/api/analytics/margin", params={"days": 30}).json()
    rev = {r["name"]: r["revenue"] for r in m["items"]}
    assert rev == {"أ": 480.92, "ب": 779.08}  # كل سطر ناقص حصته من الخصم بنسبة قيمته
    assert round(sum(rev.values()), 2) == 1260  # = صافي المبيعات (ملخص الربحية بيحسب الأصناف اللي ليها تكلفة بس)
    assert {r["name"]: r["quantity"] for r in m["items"]} == {"أ": 1, "ب": 2}


def test_zero_subtotal_document_does_not_divide_by_zero(admin):
    _, a, _b = _setup(admin)
    p = make_product(admin, price=0, locations=[(a["id"], 5)])
    _doc(admin, [_item(p, a, 1)])
    assert admin.get("/api/analytics/margin", params={"days": 30}).status_code == 200
    assert admin.get("/api/analytics/kpis", params={"days": 30}).status_code == 200


def test_valid_invoice_helper_is_single_source():
    assert str(valid_invoice(models.StockOutInvoice)) == str(valid_invoice(models.StockInInvoice)).replace("stock_in", "stock_out")
    assert "completed" in str(valid_invoice(models.StockOutInvoice).compile(compile_kwargs={"literal_binds": True}))


# ---- 15: الصلاحيات والتدقيق ----
def test_each_transition_needs_its_permission(admin, login):
    _, a, _b = _setup(admin)
    p = make_product(admin, locations=[(a["id"], 10)])
    quoter = login("quoter", ["stock_out.quote"])
    reserver = login("reserver", ["stock_out.reserve"])
    completer = login("completer", ["stock_out.complete"])
    voider = login("voider", ["invoices.void"])
    plain = login("plain", ["products.view", "stock_out.create"])
    item = _item(p, a, 2)

    assert plain.post("/api/stock/out", json={"items": [item], "status": "quote"}).status_code == 403
    assert plain.post("/api/stock/out", json={"items": [item], "status": "reserved"}).status_code == 403
    q = quoter.post("/api/stock/out", json={"items": [item], "status": "quote"}).json()
    assert quoter.post("/api/stock/out", json={"items": [item], "status": "reserved"}).status_code == 403
    assert quoter.post("/api/stock/out", json={"items": [item]}).status_code == 403
    assert quoter.post(f"/api/stock/out/{q['id']}/reserve").status_code == 403
    assert reserver.post(f"/api/stock/out/{q['id']}/reserve").status_code == 200
    assert reserver.post(f"/api/stock/out/{q['id']}/complete").status_code == 403
    assert quoter.put(f"/api/stock/out/{q['id']}", json={"items": [item]}).status_code == 403  # محجوز: محتاج stock_out.reserve
    assert completer.post(f"/api/stock/out/{q['id']}/complete").status_code == 200
    assert quoter.post(f"/api/stock/out/{q['id']}/void", json={"reason": "مفيش صلاحية"}).status_code == 403  # تمّ: invoices.void بس
    assert completer.post(f"/api/stock/out/{q['id']}/void", json={"reason": "مفيش صلاحية"}).status_code == 403
    assert voider.post(f"/api/stock/out/{q['id']}/void", json={"reason": "عنده صلاحية"}).status_code == 200
    # الوارد: إلغاء محتاج invoices.void
    inv = admin.post("/api/stock/in", json={"items": [{"product_id": p["id"], "quantity": 1, "location_id": a["id"]}]}).json()
    assert plain.post(f"/api/stock/in/{inv['id']}/void", json={"reason": "مفيش صلاحية"}).status_code == 403
    # بيانات مالية: سعر الشراء مخفي للي ملوش finance.view
    assert login("viewer", ["invoices.view"]).get(f"/api/stock/in/{inv['id']}").json()["total_value"] is None


def test_every_transition_is_audited(admin):
    _, a, _b = _setup(admin)
    p = make_product(admin, locations=[(a["id"], 10)])
    q = _doc(admin, [_item(p, a, 2)], "quote")
    assert admin.put(f"/api/stock/out/{q['id']}", json={"items": [_item(p, a, 3)]}).status_code == 200
    admin.post(f"/api/stock/out/{q['id']}/reserve")
    admin.put(f"/api/stock/out/{q['id']}", json={"items": [_item(p, a, 4)]})
    admin.post(f"/api/stock/out/{q['id']}/release")
    admin.post(f"/api/stock/out/{q['id']}/reserve")
    admin.post(f"/api/stock/out/{q['id']}/complete")
    admin.post(f"/api/stock/out/{q['id']}/void", json={"reason": "تدقيق"})
    inv = admin.post("/api/stock/in", json={"items": [{"product_id": p["id"], "quantity": 1, "location_id": a["id"]}]}).json()
    admin.post(f"/api/stock/in/{inv['id']}/void", json={"reason": "تدقيق"})
    rows = admin.get("/api/audit-log", params={"entity_type": "stock_out", "entity_id": q["id"], "page_size": 50}).json()["items"]
    actions = [r["action"] for r in rows][::-1]
    assert actions == ["create_quote", "update_quote", "reserve", "update_reserved", "release", "reserve", "complete", "void"]
    assert all(r["username"] == "admin1" and r["summary"] for r in rows)
    ins = admin.get("/api/audit-log", params={"entity_type": "stock_in", "entity_id": inv["id"]}).json()["items"]
    assert sorted(r["action"] for r in ins) == ["create", "void"]


# ---- 16: idempotency ----
def test_idempotency_key_prevents_duplicate_documents(admin):
    _, a, _b = _setup(admin)
    p = make_product(admin, locations=[(a["id"], 10)])
    key = str(uuid.uuid4())
    body = {"items": [_item(p, a, 3)], "idempotency_key": key}
    r1, r2 = admin.post("/api/stock/out", json=body), admin.post("/api/stock/out", json=body)
    assert r1.status_code == r2.status_code == 200 and r1.json()["id"] == r2.json()["id"]
    assert _bal(admin, p) == (7, 0, 7)
    ik = str(uuid.uuid4())
    ib = {"items": [{"product_id": p["id"], "quantity": 2, "location_id": a["id"]}], "idempotency_key": ik}
    i1, i2 = admin.post("/api/stock/in", json=ib), admin.post("/api/stock/in", json=ib)
    assert i1.json()["id"] == i2.json()["id"] and _bal(admin, p)[0] == 9
    assert admin.post("/api/stock/out", json={"items": [_item(p, a, 1)], "idempotency_key": "bad key!"}).status_code == 422


def test_idempotency_key_of_another_user_is_a_conflict(admin, login):
    _, a, _b = _setup(admin)
    p = make_product(admin, locations=[(a["id"], 10)])
    key = str(uuid.uuid4())
    assert admin.post("/api/stock/out", json={"items": [_item(p, a, 1)], "idempotency_key": key}).status_code == 200
    other = login("other1", ["stock_out.create", "products.view"])
    assert other.post("/api/stock/out", json={"items": [_item(p, a, 1)], "idempotency_key": key}).status_code == 409


def test_concurrent_same_idempotency_key_creates_one_document(admin, login, session_factory):
    _, a, _b = _setup(admin)
    p = make_product(admin, locations=[(a["id"], 100)])
    key = str(uuid.uuid4())
    clients = [login("admin1", create=False) for _ in range(6)]
    res = _run_parallel(clients, lambda i, c: c.post("/api/stock/out", json={"items": [_item(p, a, 5)], "idempotency_key": key}))
    assert all(r.status_code == 200 for r in res), [r.text for r in res]
    assert len({r.json()["id"] for r in res}) == 1
    assert _bal(admin, p)[0] == 95
    s = session_factory()
    try:
        assert s.query(models.StockOutInvoice).filter_by(idempotency_key=key).count() == 1
    finally:
        s.close()


# ---- check-stock بيتحقق من تطابق الحجوزات ----
def test_check_stock_validates_reservations(admin, db):
    from sqlalchemy import update

    from app.cli import check_stock

    _, a, _b = _setup(admin)
    p = make_product(admin, locations=[(a["id"], 10)])
    d = _doc(admin, [_item(p, a, 4)], "reserved")
    assert check_stock(db) == []
    admin.post(f"/api/stock/out/{d['id']}/complete")
    assert check_stock(db) == []
    d2 = _doc(admin, [_item(p, a, 3)], "reserved")
    db.execute(update(models.ProductLocation).where(models.ProductLocation.product_id == p["id"]).values(reserved=1))  # تلف متعمد
    db.commit()
    bad = check_stock(db)
    assert len(bad) == 1 and "حجز غير مطابق" in bad[0]["name"] and d2

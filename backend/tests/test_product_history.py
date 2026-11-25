"""سجل الحركة التاريخي للمنتج: الأنواع، التحويل، الملغي، الأرصدة، الفلاتر، الـ Pagination، الصلاحيات، وسيناريوهات التكامل."""
import random

import pytest

from app import models

from .helpers import get_product, make_location, make_product, make_warehouse


def hist(c, pid, **params):
    r = c.get(f"/api/products/{pid}/history", params=params)
    assert r.status_code == 200, r.text
    return r.json()


def _stock_in(c, pid, qty, loc):
    r = c.post("/api/stock/in", json={"items": [{"product_id": pid, "quantity": qty, "location_id": loc}]})
    assert r.status_code == 200, r.text
    return r.json()


def _stock_out(c, pid, qty, loc, status=None):
    body = {"items": [{"product_id": pid, "quantity": qty, "location_id": loc}]}
    if status:
        body["status"] = status
    r = c.post("/api/stock/out", json=body)
    assert r.status_code == 200, r.text
    return r.json()


def _transfer(c, pid, a, b, qty):
    r = c.post("/api/locations/transfer", json={"product_id": pid, "from_location_id": a, "to_location_id": b, "quantity": qty})
    assert r.status_code == 200, r.text


def _world(c):
    """مخزنين: W1 (A1, A2) و W2 (B1) + منتج بـ 10 في A1 (كمية افتتاحية من غير إذن إضافة)."""
    w1, w2 = make_warehouse(c), make_warehouse(c, code="W2", name="مخزن 2")
    a1 = make_location(c, warehouse_id=w1["id"], code="A1")
    a2 = make_location(c, warehouse_id=w1["id"], code="A2", name="رف 2")
    b1 = make_location(c, warehouse_id=w2["id"], code="B1", name="رف ب")
    p = make_product(c, "منتج-س", locations=[(a1["id"], 10)])
    return w1, w2, a1, a2, b1, p


# ---------------- سيناريو الحركة ----------------
def test_scenario_stock_movement_shows_up_with_correct_balances(admin):
    w1, w2, a1, a2, b1, p = _world(admin)
    inv = _stock_in(admin, p["id"], 5, a1["id"])
    h = hist(admin, p["id"])
    assert h["total"] == 1 and h["current_quantity"] == 15 and h["opening_balance"] == 10  # الافتتاحية 10 من غير إذن
    row = h["items"][0]
    assert (row["kind"], row["quantity"], row["delta"], row["balance_before"], row["balance_after"]) == ("in", 5, 5, 10, 15)
    assert row["document_id"] == inv["id"] and row["location"]["code"] == "W1-A1" and row["location"]["warehouse_name"] == "مخزن رئيسي"
    assert row["user_name"] and row["status"] == "completed"
    _stock_out(admin, p["id"], 3, a1["id"])
    h = hist(admin, p["id"])
    assert [(r["kind"], r["balance_before"], r["balance_after"]) for r in h["items"]] == [("out", 15, 12), ("in", 10, 15)]  # الأحدث الأول
    assert h["current_quantity"] == get_product(admin, p["id"])["quantity"] == 12


def test_transfer_is_one_row_and_never_double_counts(admin):
    w1, w2, a1, a2, b1, p = _world(admin)
    _transfer(admin, p["id"], a1["id"], a2["id"], 4)
    h = hist(admin, p["id"])
    assert h["total"] == 1
    t = h["items"][0]
    assert t["kind"] == "transfer" and t["quantity"] == 4 and t["delta"] == 0
    assert t["from"]["code"] == "W1-A1" and t["to"]["code"] == "W1-A2"
    assert t["balance_before"] == t["balance_after"] == 10  # إجمالي المنتج ما اتغيّرش
    assert h["summary"]["transfers_quantity"] == 4 and h["summary"]["net_change"] == 0
    prod = get_product(admin, p["id"])
    assert sum(l["quantity"] for l in prod["locations"]) == prod["quantity"] == 10  # كميات المصدر والوجهة سليمة
    # نقل بين مخزنين: يظهر تحت فلتر أي من الاتنين
    _transfer(admin, p["id"], a2["id"], b1["id"], 2)
    assert hist(admin, p["id"], warehouse_id=w2["id"])["total"] == 1
    assert hist(admin, p["id"], warehouse_id=w1["id"])["total"] == 2


def test_manual_adjustment_and_stocktake_rows(admin):
    w1, w2, a1, a2, b1, p = _world(admin)
    r = admin.post(f"/api/products/{p['id']}/adjust-quantity", json={"location_id": a1["id"], "new_quantity": 7, "reason": "عجز"})
    assert r.status_code == 200, r.text
    h = hist(admin, p["id"])
    adj = h["items"][0]
    assert adj["kind"] == "adjustment" and adj["delta"] == -3 and "عجز" in adj["notes"] and adj["balance_after"] == 7
    # جلسة جرد
    st = admin.post("/api/stocktakes", json={"warehouse_id": w1["id"], "note": "جرد"}).json()
    lines = admin.get(f"/api/stocktakes/{st['id']}", params={"page_size": 200}).json()["items"]
    line = next(l for l in lines if l["product_name"] == "منتج-س" and l["location_code"] == "W1-A1")
    admin.put(f"/api/stocktakes/{st['id']}/counts", json={"counts": [{"line_id": line["line_id"], "counted_qty": 9}]})
    admin.post(f"/api/stocktakes/{st['id']}/submit")
    assert admin.post(f"/api/stocktakes/{st['id']}/approve").status_code == 200
    h = hist(admin, p["id"])
    cnt = h["items"][0]
    assert cnt["kind"] == "stocktake" and cnt["delta"] == 2 and cnt["stocktake_id"] == st["id"] and cnt["balance_before"] == 7 and cnt["balance_after"] == 9
    assert [r["kind"] for r in hist(admin, p["id"], kind="stocktake")["items"]] == ["stocktake"]
    assert [r["kind"] for r in hist(admin, p["id"], kind="adjustment")["items"]] == ["adjustment"]
    assert h["current_quantity"] == 9


def test_zero_difference_adjustment_is_not_a_movement(admin):
    w1, w2, a1, a2, b1, p = _world(admin)
    admin.post(f"/api/products/{p['id']}/adjust-quantity", json={"location_id": a1["id"], "new_quantity": 10, "reason": "بدون تغيير"})
    assert hist(admin, p["id"])["total"] == 0


# ---------------- الملغي والحجوزات ----------------
def test_voided_stock_in_hidden_by_default_and_flagged_when_requested(admin):
    w1, w2, a1, a2, b1, p = _world(admin)
    inv = _stock_in(admin, p["id"], 5, a1["id"])
    _stock_out(admin, p["id"], 2, a1["id"])
    assert admin.post(f"/api/stock/in/{inv['id']}/void", json={"reason": "غلط في الإدخال"}).status_code == 200
    h = hist(admin, p["id"])
    assert [r["kind"] for r in h["items"]] == ["out"] and h["current_quantity"] == 8
    hv = hist(admin, p["id"], include_voided=1)
    kinds = [(r["kind"], r["is_reversal"], r["voided"], r["delta"]) for r in hv["items"]]
    assert sorted(kinds) == sorted([("in", False, True, 5), ("in", True, True, -5), ("out", False, False, -2)])
    # الإلغاء بيظهر سببه، ومش داخل في الإجماليات
    rev = next(r for r in hv["items"] if r["is_reversal"])
    assert rev["void_reason"] == "غلط في الإدخال" and rev["voided_by"]
    assert hv["summary"]["in_quantity"] == 0 and hv["summary"]["out_quantity"] == 2 and hv["summary"]["net_change"] == -2
    # السلسلة كاملة متصلة والرصيد النهائي = الحالي
    ordered = hv["items"]
    assert ordered[0]["balance_after"] == 8
    for newer, older in zip(ordered, ordered[1:]):
        assert newer["balance_before"] == older["balance_after"]
    assert ordered[-1]["balance_before"] == hv["opening_balance"] == 10


def test_reservations_and_quotes_are_not_movements_until_completed(admin):
    w1, w2, a1, a2, b1, p = _world(admin)
    quote = _stock_out(admin, p["id"], 1, a1["id"], status="quote")
    res = _stock_out(admin, p["id"], 4, a1["id"], status="reserved")
    assert hist(admin, p["id"])["total"] == 0
    # حجز اتلغى من غير ما يتنفذ: مش حركة، حتى مع include_voided
    assert admin.post(f"/api/stock/out/{res['id']}/void", json={"reason": "العميل ألغى"}).status_code == 200
    assert hist(admin, p["id"], include_voided=1)["total"] == 0
    # عرض سعر اتحوّل وتنفّذ: حركة صادر واحدة
    assert admin.post(f"/api/stock/out/{quote['id']}/reserve").status_code == 200
    assert hist(admin, p["id"])["total"] == 0                       # الحجز لسه مش حركة فعلية
    done = admin.post(f"/api/stock/out/{quote['id']}/complete").json()
    h = hist(admin, p["id"])
    assert h["total"] == 1 and h["items"][0]["kind"] == "out" and h["items"][0]["balance_after"] == 9
    assert h["items"][0]["date"].startswith(done["date"][:19])    # تاريخ الحركة = وقت الإتمام


def test_voided_completed_stock_out_shows_both_movements_when_requested(admin):
    w1, w2, a1, a2, b1, p = _world(admin)
    out = _stock_out(admin, p["id"], 3, a1["id"])
    assert admin.post(f"/api/stock/out/{out['id']}/void", json={"reason": "مرتجع"}).status_code == 200
    assert hist(admin, p["id"])["total"] == 0
    hv = hist(admin, p["id"], include_voided=1)
    assert sorted((r["delta"], r["is_reversal"]) for r in hv["items"]) == [(-3, False), (3, True)]
    assert get_product(admin, p["id"])["quantity"] == 10 == hv["current_quantity"]


# ---------------- الفلاتر والترتيب والـ Pagination ----------------
def _busy_product(c):
    w1, w2, a1, a2, b1, p = _world(c)
    for q in (1, 2, 3, 4, 5):
        _stock_in(c, p["id"], q, a1["id"])
    _stock_out(c, p["id"], 2, a1["id"])
    _stock_out(c, p["id"], 1, a1["id"])
    _transfer(c, p["id"], a1["id"], a2["id"], 6)
    _transfer(c, p["id"], a2["id"], b1["id"], 3)
    _stock_in(c, p["id"], 7, b1["id"])
    return w1, w2, a1, a2, b1, p


def test_filters_work_together(admin):
    w1, w2, a1, a2, b1, p = _busy_product(admin)
    allr = hist(admin, p["id"], page_size=100)
    assert allr["total"] == 10
    assert hist(admin, p["id"], kind="in")["total"] == 6
    assert hist(admin, p["id"], kind="out")["total"] == 2
    assert hist(admin, p["id"], kind="transfer")["total"] == 2
    # مخزن
    assert {r["key"] for r in hist(admin, p["id"], warehouse_id=w2["id"], page_size=100)["items"]} == \
           {r["key"] for r in allr["items"] if (r["location"] or {}).get("warehouse_id") == w2["id"] or (r["from"] or {}).get("warehouse_id") == w2["id"] or (r["to"] or {}).get("warehouse_id") == w2["id"]}
    assert hist(admin, p["id"], warehouse_id=w2["id"])["total"] == 2  # وارد 7 في B1 + نقل لـ B1
    # موقع + نوع
    assert hist(admin, p["id"], location_id=a1["id"], kind="in")["total"] == 5
    assert hist(admin, p["id"], location_id=a2["id"])["total"] == 2          # النقلتين (مصدر/وجهة)
    assert hist(admin, p["id"], warehouse_id=w1["id"], kind="out")["total"] == 2
    assert hist(admin, p["id"], warehouse_id=w2["id"], kind="out")["total"] == 0
    # فترة زمنية: اليوم كله / يوم قديم
    assert hist(admin, p["id"], date_from="2000-01-01", date_to="2999-01-01")["total"] == 10
    assert hist(admin, p["id"], date_from="2999-01-01")["total"] == 0
    assert hist(admin, p["id"], date_to="2000-01-01")["total"] == 0
    # الأرصدة صحيحة حتى مع الفلتر (محسوبة على السجل الكامل)
    only_out = hist(admin, p["id"], kind="out")["items"]
    full = {r["key"]: r for r in allr["items"]}
    for r in only_out:
        assert (r["balance_before"], r["balance_after"]) == (full[r["key"]]["balance_before"], full[r["key"]]["balance_after"])


def test_pagination_is_complete_stable_and_has_no_duplicates(admin):
    w1, w2, a1, a2, b1, p = _busy_product(admin)
    seen, pages = [], 0
    page = 1
    while True:
        h = hist(admin, p["id"], page=page, page_size=3)
        pages += 1
        seen.extend(h["items"])
        assert h["total"] == 10 and h["page"] == page and h["total_pages"] == 4
        if page >= h["total_pages"]:
            break
        page += 1
    keys = [r["key"] for r in seen]
    assert len(keys) == len(set(keys)) == 10 and pages == 4
    for newer, older in zip(seen, seen[1:]):  # السلسلة متصلة عبر حدود الصفحات
        assert newer["balance_before"] == older["balance_after"]
    asc = hist(admin, p["id"], page_size=100, order="asc")["items"]
    assert [r["key"] for r in asc] == keys[::-1]
    assert hist(admin, p["id"], page=99, page_size=3)["items"] == []


def test_bad_params(admin):
    w1, w2, a1, a2, b1, p = _world(admin)
    assert admin.get(f"/api/products/{p['id']}/history", params={"kind": "nope"}).status_code == 400
    assert admin.get(f"/api/products/{p['id']}/history", params={"order": "up"}).status_code == 400
    assert admin.get("/api/products/99999/history").status_code == 404
    assert admin.get(f"/api/products/{p['id']}/history", params={"date_from": "not-a-date"}).status_code in (400, 422)


def test_product_without_any_movement(admin):
    p = make_product(admin, "فاضي")
    h = hist(admin, p["id"])
    assert h["items"] == [] and h["total"] == 0 and h["total_pages"] == 1 and h["current_quantity"] == 0 and h["opening_balance"] == 0


def test_other_products_movements_never_leak(admin):
    w1, w2, a1, a2, b1, p = _world(admin)
    other = make_product(admin, "تاني", locations=[(a1["id"], 1)])
    _stock_in(admin, other["id"], 9, a1["id"])
    _transfer(admin, other["id"], a1["id"], a2["id"], 1)
    assert hist(admin, p["id"])["total"] == 0
    assert hist(admin, other["id"])["total"] == 2


# ---------------- الصلاحيات ----------------
def test_each_kind_is_gated_by_its_own_permission(admin, login):
    w1, w2, a1, a2, b1, p = _world(admin)
    _stock_in(admin, p["id"], 5, a1["id"])
    _transfer(admin, p["id"], a1["id"], a2["id"], 2)
    admin.post(f"/api/products/{p['id']}/adjust-quantity", json={"location_id": a1["id"], "new_quantity": 1, "reason": "x"})
    kinds = lambda c: sorted({r["kind"] for r in hist(c, p["id"], page_size=100)["items"]})  # noqa: E731
    assert kinds(admin) == ["adjustment", "in", "transfer"]
    inv_only = login("invonly", ["invoices.view"])
    assert kinds(inv_only) == ["in"] and hist(inv_only, p["id"])["balances_available"] is True
    tr_only = login("tronly", ["transfers.view"])
    h = hist(tr_only, p["id"])
    assert kinds(tr_only) == ["transfer"] and h["balances_available"] is False
    assert all(r["balance_before"] is None and r["balance_after"] is None for r in h["items"]) and h["opening_balance"] is None
    # طلب نوع مش مسموح بيه = نتيجة فاضية (مش تسريب)
    assert hist(inv_only, p["id"], kind="transfer")["total"] == 0
    assert login("reporter", ["reports.view"]).get(f"/api/products/{p['id']}/history").status_code == 200
    assert login("nobody", ["products.view"]).get(f"/api/products/{p['id']}/history").status_code == 403


def test_anonymous_is_rejected(anon):
    assert anon.get("/api/products/1/history").status_code == 401


# ---------------- اختبار عشوائي: الأرصدة لازم دايمًا تتجمع صح ----------------
@pytest.mark.parametrize("seed", [1, 2, 3])
def test_random_operations_keep_ledger_consistent(admin, seed):
    rnd = random.Random(seed)
    w1, w2, a1, a2, b1, p = _world(admin)
    locs = [a1["id"], a2["id"], b1["id"]]
    pid = p["id"]
    open_docs = []
    for _ in range(40):
        op = rnd.choice(["in", "in", "out", "transfer", "adjust", "void", "reserve", "complete"])
        qty_by_loc = {l["location_id"]: l["quantity"] for l in get_product(admin, pid)["locations"]}
        try:
            if op == "in":
                open_docs.append(("in", _stock_in(admin, pid, rnd.randint(1, 9), rnd.choice(locs))["id"]))
            elif op == "out":
                loc = max(qty_by_loc, key=qty_by_loc.get) if qty_by_loc else a1["id"]
                if qty_by_loc.get(loc, 0) >= 1:
                    open_docs.append(("out", _stock_out(admin, pid, rnd.randint(1, int(qty_by_loc[loc])), loc)["id"]))
            elif op == "transfer":
                src = max(qty_by_loc, key=qty_by_loc.get) if qty_by_loc else None
                if src and qty_by_loc[src] >= 1:
                    dst = rnd.choice([l for l in locs if l != src])
                    _transfer(admin, pid, src, dst, rnd.randint(1, int(qty_by_loc[src])))
            elif op == "adjust":
                admin.post(f"/api/products/{pid}/adjust-quantity", json={"location_id": rnd.choice(locs), "new_quantity": rnd.randint(0, 12), "reason": "r"})
            elif op == "void" and open_docs:
                kind, did = open_docs.pop(rnd.randrange(len(open_docs)))
                admin.post(f"/api/stock/{kind}/{did}/void", json={"reason": "random"})
            elif op == "reserve":
                loc = max(qty_by_loc, key=qty_by_loc.get) if qty_by_loc else a1["id"]
                if qty_by_loc.get(loc, 0) >= 1:
                    inv = _stock_out(admin, pid, 1, loc, status="reserved")
                    if rnd.random() < 0.5:
                        admin.post(f"/api/stock/out/{inv['id']}/complete")
                        open_docs.append(("out", inv["id"]))
        except AssertionError:
            pass  # عملية اترفضت لسبب مشروع (مثلًا كمية غير كافية): مش المطلوب اختباره هنا

        current = get_product(admin, pid)["quantity"]
        for flag in (0, 1):
            h = hist(admin, pid, page_size=100, include_voided=flag)
            rows = h["items"]
            assert h["current_quantity"] == current
            keys = [r["key"] for r in rows]
            assert len(keys) == len(set(keys))                                   # مفيش تكرار
            assert h["opening_balance"] + sum(r["delta"] for r in hist(admin, pid, page_size=100, include_voided=1)["items"]) == pytest.approx(current)
        full = hist(admin, pid, page_size=100, include_voided=1)["items"]
        if full:
            assert full[0]["balance_after"] == pytest.approx(current)
            for newer, older in zip(full, full[1:]):
                assert newer["balance_before"] == pytest.approx(older["balance_after"])

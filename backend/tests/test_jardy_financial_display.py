"""القيم المالية في المستندات وسجل الحركات: قبل الخصم / الخصم / الصافي / الضريبة / الإجمالي النهائي (4 حالات)."""
import pytest

from app import models

from .helpers import make_location, make_product, make_warehouse


def _setup(c):
    wh = make_warehouse(c)
    a = make_location(c, warehouse_id=wh["id"], code="A1")
    pa = make_product(c, "صنف-أ", price=500, locations=[(a["id"], 5)])
    pb = make_product(c, "صنف-ب", price=405, locations=[(a["id"], 5)])
    items = [{"product_id": pa["id"], "quantity": 1, "location_id": a["id"]},
             {"product_id": pb["id"], "quantity": 2, "location_id": a["id"]}]   # 500 + 810 = 1310
    return items


CASES = {
    "no_adjustments": ({}, (1310, 0, 1310, 0, 1310)),
    "discount_only": ({"discount_type": "amount", "discount_value": 50}, (1310, 50, 1260, 0, 1260)),
    "tax_only": ({"tax_type": "percent", "tax_value": 14}, (1310, 0, 1310, 183.4, 1493.4)),
    "discount_and_tax": ({"discount_type": "amount", "discount_value": 50, "tax_type": "percent", "tax_value": 14}, (1310, 50, 1260, 176.4, 1436.4)),
}


@pytest.mark.parametrize("name", list(CASES))
def test_out_document_values_are_consistent_everywhere(admin, name):
    extra, (subtotal, discount, net, tax, grand) = CASES[name]
    items = _setup(admin)
    r = admin.post("/api/stock/out", json={"items": items, "status": "completed", **extra})
    assert r.status_code == 200, r.text
    doc = r.json()
    assert (doc["subtotal"], doc["discount_amount"], doc["net_amount"], doc["tax_amount"], doc["grand_total"]) == (subtotal, discount, net, tax, grand)

    # سجل الحركات: نفس الأرقام على مستوى الصف (والإجمالي النهائي هو اللي المفروض يتعرض كـ "القيمة")
    log = admin.get("/api/stock/log", params={"page_size": 50}).json()
    row = next(x for x in log["items"] if x.get("kind") == "out")
    assert (row["total_value"], row["discount_amount"], row["net_amount"], row["tax_amount"], row["grand_total"]) == (subtotal, discount, net, tax, grand)
    t = log["totals"]
    assert (t["out_total_value"], t["out_discount_total"], t["out_net_value"], t["out_tax_total"], t["out_grand_total"]) == (subtotal, discount, net, tax, grand)

    # قايمة المستندات بترجّع نفس الإجماليات
    lst = admin.get("/api/stock/out", params={"page_size": 50}).json()
    item = (lst["items"] if isinstance(lst, dict) else lst)[0]
    assert (item["subtotal"], item["discount_amount"], item["net_amount"], item["tax_amount"], item["grand_total"]) == (subtotal, discount, net, tax, grand)


def test_totals_only_count_valid_documents(admin):
    items = _setup(admin)
    done = {"discount_type": "amount", "discount_value": 50, "tax_type": "percent", "tax_value": 14}
    admin.post("/api/stock/out", json={"items": items, "status": "completed", **done})
    assert admin.post("/api/stock/out", json={"items": items, "status": "quote", **done}).status_code == 200      # عرض سعر: مش محسوب
    gone = admin.post("/api/stock/out", json={"items": items, "status": "completed"}).json()
    assert admin.post(f"/api/stock/out/{gone['id']}/void", json={"reason": "ملغي للتجربة"}).status_code == 200  # ملغي: مش محسوب
    t = admin.get("/api/stock/log", params={"page_size": 50}).json()["totals"]
    assert (t["out_invoices_count"], t["out_total_value"], t["out_discount_total"], t["out_tax_total"], t["out_grand_total"]) == (1, 1310, 50, 176.4, 1436.4)


def test_legacy_documents_without_stored_totals_fall_back(admin, db):
    """مستندات قديمة (grand_total / subtotal = NULL) لازم تتعرض بإجمالي صح مش None."""
    items = _setup(admin)
    admin.post("/api/stock/out", json={"items": items, "status": "completed"})
    db.query(models.StockOutInvoice).update({"subtotal_amount": None, "discount_amount": None, "tax_amount": None, "grand_total": None})
    db.commit()
    log = admin.get("/api/stock/log", params={"page_size": 50}).json()
    row = next(x for x in log["items"] if x.get("kind") == "out")
    assert row["grand_total"] == 1310 and row["tax_amount"] == 0
    assert log["totals"]["out_grand_total"] == 1310 and log["totals"]["out_tax_total"] == 0

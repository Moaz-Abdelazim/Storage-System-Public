"""تقنيع القيم المالية (finance.view) والصلاحيات الشرطية (سعر، كمية ابتدائية، خصم، استيراد)."""
import io

import pandas as pd
import pytest

from .helpers import get_product, make_location, make_product, make_warehouse


@pytest.fixture()
def data(admin):
    """وارد بسعر شراء 7 + صادر + نقل، كلهم بواسطة الأدمن."""
    wh = make_warehouse(admin)
    a = make_location(admin, warehouse_id=wh["id"], code="A1")
    b = make_location(admin, warehouse_id=wh["id"], code="B1")
    sup = admin.post("/api/suppliers", json={"name": "مورد"}).json()
    p = make_product(admin, "صنف", price=10)
    inv_in = admin.post("/api/stock/in", json={"supplier_id": sup["id"], "items": [{"product_id": p["id"], "quantity": 10, "unit_price": 7, "location_id": a["id"]}]}).json()
    inv_out = admin.post("/api/stock/out", json={"items": [{"product_id": p["id"], "quantity": 2, "location_id": a["id"]}]}).json()
    admin.post("/api/locations/transfer", json={"product_id": p["id"], "from_location_id": a["id"], "to_location_id": b["id"], "quantity": 1})
    return {"p": p, "sup": sup, "in": inv_in, "out": inv_out, "a": a, "b": b}


def _viewer(login, with_finance=False, extra=()):
    perms = ["invoices.view", "invoices.print", "transfers.view", "suppliers.view", "products.view", "locations.view", *extra]
    if with_finance:
        perms.append("finance.view")
    return login("viewer_fin" if with_finance else "viewer_nofin", perms)


def test_in_invoice_prices_hidden_without_finance(admin, login, data):
    v = _viewer(login)
    inv = v.get(f"/api/stock/in/{data['in']['id']}").json()
    assert inv["total_value"] is None and inv["items"][0]["unit_price"] is None
    assert v.get("/api/stock/in").json()["items"][0]["total_value"] is None
    full = admin.get(f"/api/stock/in/{data['in']['id']}").json()
    assert full["total_value"] == 70 and full["items"][0]["unit_price"] == 7


def test_in_invoice_prices_visible_with_finance(login, data):
    v = _viewer(login, with_finance=True)
    assert v.get(f"/api/stock/in/{data['in']['id']}").json()["total_value"] == 70


def test_stock_in_creation_response_is_masked_without_finance(login, data):
    c = login("recv1", ["stock_in.create", "locations.view", "products.view"])
    r = c.post("/api/stock/in", json={"items": [{"product_id": data["p"]["id"], "quantity": 1, "unit_price": 5, "location_id": data["a"]["id"]}]})
    assert r.status_code == 200 and r.json()["total_value"] is None


def test_stock_log_masks_in_values_and_limits_kinds(admin, login, data):
    v = _viewer(login)
    log = v.get("/api/stock/log").json()
    assert log["totals"]["in_total_value"] is None
    assert all(i["total_value"] is None for i in log["items"] if i["kind"] == "in")
    assert log["totals"]["out_invoices_count"] == 1 and log["totals"]["transfers_count"] == 1
    assert admin.get("/api/stock/log").json()["totals"]["in_total_value"] == 70


def test_stock_log_only_transfers_without_invoices_view(login, data):
    c = login("mover1", ["transfers.view"])
    log = c.get("/api/stock/log").json()
    assert {i["kind"] for i in log["items"]} == {"transfer"}
    assert log["totals"]["in_invoices_count"] == 0 and log["totals"]["out_invoices_count"] == 0 and log["total"] == 1
    only_inv = login("inv1", ["invoices.view"]).get("/api/stock/log").json()
    assert {i["kind"] for i in only_inv["items"]} == {"in", "out"} and only_inv["totals"]["transfers_count"] == 0


def test_print_masks_in_prices_but_not_out(login, data):
    v = _viewer(login)
    pin = v.get(f"/api/stock/in/{data['in']['id']}/print").json()
    assert pin["items"][0]["unit_price"] is None and pin["items"][0]["line_total"] is None
    assert pin["totals"]["subtotal"] is None and pin["totals"]["grand_total"] is None
    pout = v.get(f"/api/stock/out/{data['out']['id']}/print").json()
    assert pout["items"][0]["unit_price"] == 10 and pout["totals"]["grand_total"] == 20
    fin = _viewer(login, with_finance=True).get(f"/api/stock/in/{data['in']['id']}/print").json()
    assert fin["items"][0]["unit_price"] == 7 and fin["totals"]["grand_total"] == 70


def test_product_movements_mask_purchase_price(login, data):
    v = _viewer(login)
    pid = data["p"]["id"]
    assert v.get(f"/api/products/{pid}/movements", params={"kind": "in"}).json()["items"][0]["unit_price"] is None
    assert v.get(f"/api/products/{pid}/movements", params={"kind": "out"}).json()["items"][0]["unit_price"] == 10
    old = v.get(f"/api/reports/product-movements/{pid}").json()
    assert old["stock_in"][0]["unit_price"] is None and old["stock_out"][0]["unit_price"] == 10
    assert v.get("/api/reports/product-movements/9999").status_code == 404  # كان 200 + {"error"}


def test_supplier_product_invoices_mask_price(login, data):
    v = _viewer(login)
    r = v.get(f"/api/reports/supplier-product-invoices/{data['sup']['id']}/{data['p']['id']}").json()
    assert r["items"][0]["unit_price"] is None
    r = _viewer(login, with_finance=True).get(f"/api/reports/supplier-product-invoices/{data['sup']['id']}/{data['p']['id']}").json()
    assert r["items"][0]["unit_price"] == 7


def test_summary_and_dashboard_hide_money_without_finance(admin, login, data):
    v = _viewer(login)
    assert v.get("/api/reports/summary").json()["total_stock_value"] is None
    assert admin.get("/api/reports/summary").json()["total_stock_value"] is not None
    dash = v.get("/api/dashboard/overview").json()
    assert dash["admin"] is None
    assert all(r.get("total_value") is None for r in dash["recent"])
    assert all("in_value" not in d for d in v.get("/api/dashboard/flow").json()["days"])
    assert admin.get("/api/dashboard/overview").json()["admin"] is not None


def test_dashboard_recent_respects_kind_permissions(login, data):
    only_tr = login("tr1", ["transfers.view"]).get("/api/dashboard/overview").json()["recent"]
    assert {r["kind"] for r in only_tr} == {"transfer"}
    only_inv = login("iv1", ["invoices.view"]).get("/api/dashboard/overview").json()["recent"]
    assert {r["kind"] for r in only_inv} == {"in", "out"}


def test_locations_stock_report_value_masked(admin, login, data):
    masked = _viewer(login).get("/api/locations/reports/stock").json()
    assert masked["totals"]["value"] is None
    assert all(r["value"] is None for r in masked["items"])
    full = admin.get("/api/locations/reports/stock").json()
    assert full["totals"]["value"] is not None and full["totals"]["value"] > 0


def _read_xlsx(resp):
    assert resp.status_code == 200, resp.text
    return pd.read_excel(io.BytesIO(resp.content), sheet_name=None)


def test_movements_export_masks_prices_and_respects_dates(admin, login, data):
    sheets = _read_xlsx(_viewer(login).get("/api/excel/movements/export"))
    assert "سعر الوحدة" not in sheets["الوارد"].columns and "سعر الوحدة" not in sheets["الصادر"].columns
    assert len(sheets["الوارد"]) == 1 and len(sheets["الصادر"]) == 1 and len(sheets["النقل بين المواقع"]) == 1
    full = _read_xlsx(admin.get("/api/excel/movements/export"))
    assert list(full["الوارد"]["سعر الوحدة"]) == [7]
    empty = _read_xlsx(admin.get("/api/excel/movements/export", params={"date_from": "2000-01-01", "date_to": "2000-01-02"}))
    assert len(empty["الوارد"]) == 0 and len(empty["الصادر"]) == 0
    today = _read_xlsx(admin.get("/api/excel/movements/export", params={"date_from": "2000-01-01"}))
    assert len(today["الوارد"]) == 1
    assert admin.get("/api/excel/movements/export", params={"date_from": "garbage"}).status_code == 400


def test_movements_export_without_transfers_view_has_no_transfer_sheet(login, data):
    sheets = _read_xlsx(login("inv2", ["invoices.view"]).get("/api/excel/movements/export"))
    assert "النقل بين المواقع" not in sheets


# ------------------------------------------------ صلاحيات شرطية
def test_create_product_with_quantity_needs_adjust(admin, login, data):
    c = login("creator1", ["products.create"])
    assert c.post("/api/products", json={"name": "جديد", "price": 1}).status_code == 200
    r = c.post("/api/products", json={"name": "بكمية", "price": 1, "quantity": 5, "locations": [{"location_id": data["a"]["id"], "quantity": 5}]})
    assert r.status_code == 403 and "الكميات" in r.json()["detail"]
    c2 = login("creator2", ["products.create", "products.adjust", "locations.view"])
    assert c2.post("/api/products", json={"name": "بكمية", "price": 1, "quantity": 5, "locations": [{"location_id": data["a"]["id"], "quantity": 5}]}).status_code == 200


def test_price_change_needs_products_price(admin, login, data):
    pid = data["p"]["id"]
    editor = login("editor1", ["products.edit"])
    assert editor.put(f"/api/products/{pid}", json={"name": "اسم جديد"}).status_code == 200
    assert editor.put(f"/api/products/{pid}", json={"name": "اسم", "price": 10}).status_code == 200  # نفس السعر = مش تغيير
    r = editor.put(f"/api/products/{pid}", json={"price": 11})
    assert r.status_code == 403 and "سعر" in r.json()["detail"]
    assert get_product(admin, pid)["price"] == 10
    pricer = login("pricer1", ["products.price"])
    assert pricer.put(f"/api/products/{pid}", json={"price": 11}).status_code == 200


def test_discount_and_tax_need_discount_permission(admin, login, data):
    item = {"product_id": data["p"]["id"], "quantity": 1, "location_id": data["a"]["id"]}
    seller = login("seller1", ["stock_out.create", "products.view"])
    assert seller.post("/api/stock/out", json={"items": [item]}).status_code == 200
    r = seller.post("/api/stock/out", json={"items": [item], "discount_type": "percent", "discount_value": 10})
    assert r.status_code == 403 and "خصم" in r.json()["detail"]
    assert seller.post("/api/stock/out", json={"items": [item], "tax_type": "percent", "tax_value": 14}).status_code == 403
    assert seller.post("/api/stock/out", json={"items": [item], "discount_type": "percent", "discount_value": 0}).status_code == 200  # صفر = مفيش خصم
    boss = login("seller2", ["stock_out.discount"])
    assert boss.post("/api/stock/out", json={"items": [item], "discount_type": "percent", "discount_value": 10}).status_code == 200


def test_held_invoice_edit_with_discount_needs_permission(admin, login, data):
    item = {"product_id": data["p"]["id"], "quantity": 1, "location_id": data["a"]["id"]}
    held = admin.post("/api/stock/out", json={"items": [item], "status": "held"}).json()
    mgr = login("holder1", ["stock_out.manage_held"])
    assert mgr.put(f"/api/stock/out/{held['id']}", json={"items": [item], "discount_type": "amount", "discount_value": 1}).status_code == 403
    assert mgr.put(f"/api/stock/out/{held['id']}", json={"items": [item]}).status_code == 200
    assert mgr.post(f"/api/stock/out/{held['id']}/complete").status_code == 200


def _xlsx(rows):
    buf = io.BytesIO()
    pd.DataFrame(rows).to_excel(buf, index=False)
    return buf.getvalue()


def _import(c, rows):
    return c.post("/api/excel/products/import", files={"file": ("p.xlsx", _xlsx(rows), "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")})


def test_import_without_adjust_rejects_quantity_rows(admin, login, data):
    c = login("imp1", ["products.import"])
    r = _import(c, [{"الاسم": "جديد1", "السعر": 3}, {"الاسم": "بكمية", "السعر": 3, "الموقع": "W1-A1", "الكمية": 4}])
    body = r.json()
    assert r.status_code == 200 and body["created"] == 1
    assert any("صلاحية تعديل الكميات" in e for e in body["errors"])
    full = login("imp2", ["products.import", "products.adjust", "products.price"])
    ok = _import(full, [{"الاسم": "بكمية", "السعر": 3, "الموقع": "W1-A1", "الكمية": 4}]).json()
    assert ok["created"] == 1 and ok["errors"] == []


def test_import_without_price_permission_rejects_price_change(admin, login, data):
    pid = data["p"]["id"]
    c = login("imp3", ["products.import"])
    body = _import(c, [{"ID": pid, "الاسم": "صنف", "السعر": 99}]).json()
    assert body["updated"] == 0 and any("سعر" in e for e in body["errors"])
    assert get_product(admin, pid)["price"] == 10
    same = _import(c, [{"ID": pid, "الاسم": "صنف", "السعر": 10}]).json()
    assert same["updated"] == 1


def test_import_only_accepts_xlsx_and_hides_internal_errors(admin):
    r = admin.post("/api/excel/products/import", files={"file": ("p.xls", b"x", "application/vnd.ms-excel")})
    assert r.status_code == 400
    r = admin.post("/api/excel/products/import", files={"file": ("p.xlsx", b"not a real xlsx", "application/octet-stream")})
    assert r.status_code == 400 and "Traceback" not in r.text

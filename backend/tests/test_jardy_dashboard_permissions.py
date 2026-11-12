"""الملخص المالي في الصفحة الرئيسية: بصلاحية القيم المالية (finance.view) بس، ومن السيرفر مش من الواجهة."""
import pytest

from app import models

from .helpers import make_location, make_product, make_warehouse


@pytest.fixture()
def sales(admin):
    wh = make_warehouse(admin)
    a = make_location(admin, warehouse_id=wh["id"], code="A1")
    p = make_product(admin, "صنف", price=10, locations=[(a["id"], 10)])
    assert admin.post("/api/stock/out", json={"items": [{"product_id": p["id"], "quantity": 2, "location_id": a["id"]}]}).status_code == 200
    return p


def _overview(c):
    r = c.get("/api/dashboard/overview")
    assert r.status_code == 200, r.text
    return r.json()


def test_admin_sees_financial_block(admin, sales):
    d = _overview(admin)
    assert d["finance_visible"] is True and d["admin"] is not None and d["admin"]["total_stock_value"] == 80


def test_role_with_finance_view_sees_financial_block(login, sales):
    d = _overview(login("fin", ["finance.view"]))
    assert d["finance_visible"] is True and d["admin"]["total_stock_value"] == 80


def test_reports_role_sees_financial_block(login, sales):
    assert _overview(login("rep", ["reports.view"]))["finance_visible"] is True      # reports.view بيتقفل على finance.view


@pytest.mark.parametrize("perms", [["invoices.view"], ["products.view", "locations.view"], ["stock_out.create"], ["stocktake.manage"]])
def test_role_without_finance_view_gets_no_financial_data(login, sales, perms):
    c = login("plain", perms)
    d = _overview(c)
    assert d["finance_visible"] is False and d["admin"] is None
    assert all("value" not in k for k in d["flow"][-1])                              # مفيش قيم في رسم الحركة
    assert c.get("/api/reports/out-totals").status_code == 403
    for ep in ("kpis", "stock-valuation", "stocktake-accuracy", "executive-report"):
        assert c.get(f"/api/analytics/{ep}").status_code == 403, ep


def test_permission_change_takes_effect_on_next_request(admin, login, sales, db):
    c = login("fin", ["finance.view"])
    assert _overview(c)["finance_visible"] is True
    role = db.query(models.Role).filter(models.Role.name != "admin").order_by(models.Role.id.desc()).first()
    db.query(models.RolePermission).filter_by(role_id=role.id, permission="finance.view").delete()
    db.commit()
    assert _overview(c)["finance_visible"] is False

"""الجرد، الإعداد الأول، العنوان العام، اللوجو، واستيراد Excel المحسّن."""
import io
import time

import pandas as pd

from app import models

from .helpers import get_product, location_qty, make_location, make_product, make_warehouse

XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


def _wh(c):
    wh = make_warehouse(c)
    return wh, make_location(c, warehouse_id=wh["id"], code="A1"), make_location(c, warehouse_id=wh["id"], code="B1", name="رف ب")


def _lines(c, sid, **params):
    return c.get(f"/api/stocktakes/{sid}", params={"page_size": 200, **params}).json()


# ------------------------------------------------------------------ الجرد
def test_stocktake_full_cycle_adjusts_stock_and_logs(admin):
    wh, a, b = _wh(admin)
    p1 = make_product(admin, "جرد-1", price=10, locations=[(a["id"], 10)])
    p2 = make_product(admin, "جرد-2", price=4, locations=[(a["id"], 5), (b["id"], 5)])
    st = admin.post("/api/stocktakes", json={"warehouse_id": wh["id"], "note": "جرد شهري"}).json()
    assert st["status"] == "draft" and st["lines_total"] == 3
    data = _lines(admin, st["id"])
    by = {(l["product_name"], l["location_code"]): l["line_id"] for l in data["items"]}
    counts = [{"line_id": by[("جرد-1", "W1-A1")], "counted_qty": 7},    # عجز 3
              {"line_id": by[("جرد-2", "W1-A1")], "counted_qty": 6}]    # زيادة 1
    assert admin.put(f"/api/stocktakes/{st['id']}/counts", json={"counts": counts}).json()["status"] == "counting"
    # الحركة أثناء العد متضيعش: بيع 2 من جرد-1 وقت العد
    assert admin.post("/api/stock/out", json={"items": [{"product_id": p1["id"], "quantity": 2, "location_id": a["id"]}]}).status_code == 200
    assert admin.post(f"/api/stocktakes/{st['id']}/approve").status_code == 409  # لسه مش في المراجعة
    assert admin.post(f"/api/stocktakes/{st['id']}/submit").json()["status"] == "review"
    summ = _lines(admin, st["id"])["summary"]
    assert (summ["counted"], summ["uncounted"], summ["diff_lines"], summ["surplus_qty"], summ["shortage_qty"], summ["net_value"]) == (2, 1, 2, 1, 3, -26)
    assert admin.post(f"/api/stocktakes/{st['id']}/approve").status_code == 200
    g1, g2 = get_product(admin, p1["id"]), get_product(admin, p2["id"])
    assert g1["quantity"] == 5 and location_qty(g1, a["id"]) == 5            # 10 - 2 (بيع) - 3 (عجز)
    assert g2["quantity"] == 11 and location_qty(g2, a["id"]) == 6 and location_qty(g2, b["id"]) == 5  # السطر غير المعدود ما اتلمسش
    # اعتماد مزدوج ممنوع، والتسوية اتسجلت + التدقيق
    assert admin.post(f"/api/stocktakes/{st['id']}/approve").status_code == 409
    audit = admin.get("/api/audit-log", params={"entity_type": "stocktake", "entity_id": st["id"]}).json()["items"]
    assert sorted(r["action"] for r in audit) == ["approve", "create", "submit"]


def test_stocktake_adjustments_recorded(admin, db):
    wh, a, _b = _wh(admin)
    p = make_product(admin, locations=[(a["id"], 10)])
    st = admin.post("/api/stocktakes", json={"location_id": a["id"]}).json()
    line = _lines(admin, st["id"])["items"][0]["line_id"]
    admin.put(f"/api/stocktakes/{st['id']}/counts", json={"counts": [{"line_id": line, "counted_qty": 4}]})
    admin.post(f"/api/stocktakes/{st['id']}/submit")
    admin.post(f"/api/stocktakes/{st['id']}/approve")
    row = db.query(models.QuantityAdjustment).filter_by(product_id=p["id"]).one()
    assert (row.old_quantity, row.new_quantity, row.difference, row.reason) == (10, 4, -6, f"جرد #{st['id']}")


def test_stocktake_approval_blocked_below_reserved_and_atomic(admin):
    wh, a, _b = _wh(admin)
    p = make_product(admin, "محجوز", locations=[(a["id"], 10)])
    q = make_product(admin, "عادي", locations=[(a["id"], 10)])
    st = admin.post("/api/stocktakes", json={"warehouse_id": wh["id"]}).json()
    items = {l["product_name"]: l["line_id"] for l in _lines(admin, st["id"])["items"]}
    admin.put(f"/api/stocktakes/{st['id']}/counts", json={"counts": [{"line_id": items["محجوز"], "counted_qty": 2}, {"line_id": items["عادي"], "counted_qty": 1}]})
    admin.post(f"/api/stocktakes/{st['id']}/submit")
    assert admin.post("/api/stock/out", json={"status": "reserved", "items": [{"product_id": p["id"], "quantity": 8, "location_id": a["id"]}]}).status_code == 200
    r = admin.post(f"/api/stocktakes/{st['id']}/approve")
    assert r.status_code == 409 and "محجوز" in r.json()["detail"] and "المحجوز" in r.json()["detail"]
    assert get_product(admin, q["id"])["quantity"] == 10 and get_product(admin, p["id"])["quantity"] == 10  # ولا حاجة اتغيرت
    assert admin.get(f"/api/stocktakes/{st['id']}").json()["stocktake"]["status"] == "review"  # الحالة كمان رجعت


def test_stocktake_excel_roundtrip_and_errors(admin):
    wh, a, _b = _wh(admin)
    p = make_product(admin, locations=[(a["id"], 10)])
    st = admin.post("/api/stocktakes", json={"warehouse_id": wh["id"]}).json()
    exp = admin.get(f"/api/stocktakes/{st['id']}/export")
    df = pd.read_excel(io.BytesIO(exp.content))
    assert list(df.columns)[:2] == ["رقم السطر", "المنتج"] and len(df) == 1
    good = pd.DataFrame({"رقم السطر": [int(df["رقم السطر"][0]), 9999, int(df["رقم السطر"][0])], "الكمية المعدودة": [3, 1, -1]})
    buf = io.BytesIO(); good.to_excel(buf, index=False)
    out = admin.post(f"/api/stocktakes/{st['id']}/import", files={"file": ("s.xlsx", buf.getvalue(), XLSX)}).json()
    assert out["applied"] == 1 and out["errors_total"] == 2 and out["status"] == "counting"
    assert all(set(e) == {"row", "column", "message"} for e in out["errors"])
    assert _lines(admin, st["id"])["items"][0]["counted_qty"] == 3 and p


def test_stocktake_found_item_and_cancel_and_permissions(admin, login):
    wh, a, _b = _wh(admin)
    p = make_product(admin, "لقيته")
    st = admin.post("/api/stocktakes", json={"location_id": a["id"]}).json()
    assert st["lines_total"] == 0
    assert admin.post(f"/api/stocktakes/{st['id']}/submit").status_code == 400  # مفيش عدّ
    assert admin.post(f"/api/stocktakes/{st['id']}/lines", json={"product_id": p["id"], "location_id": a["id"], "counted_qty": 3}).status_code == 201
    assert admin.post(f"/api/stocktakes/{st['id']}/lines", json={"product_id": p["id"], "location_id": a["id"], "counted_qty": 3}).status_code == 409
    manager, approver = login("stm", ["stocktake.manage"]), login("sta", ["stocktake.approve"])
    admin.post(f"/api/stocktakes/{st['id']}/submit")
    assert manager.post(f"/api/stocktakes/{st['id']}/approve").status_code == 403
    assert approver.post(f"/api/stocktakes/{st['id']}/approve").status_code == 200  # approve بتطلب manage بالـ closure
    assert get_product(admin, p["id"])["quantity"] == 3
    st2 = manager.post("/api/stocktakes", json={"warehouse_id": wh["id"]}).json()
    assert manager.post(f"/api/stocktakes/{st2['id']}/cancel").json()["status"] == "cancelled"
    assert manager.put(f"/api/stocktakes/{st2['id']}/counts", json={"counts": []}).status_code == 409
    assert admin.post("/api/stocktakes", json={}).status_code == 400


def test_stocktake_hides_values_without_finance_view(admin, login):
    wh, a, _b = _wh(admin)
    make_product(admin, price=10, locations=[(a["id"], 5)])
    st = admin.post("/api/stocktakes", json={"warehouse_id": wh["id"]}).json()
    plain = login("stplain", ["stocktake.manage"])
    d = _lines(plain, st["id"])
    assert d["items"][0]["unit_price"] is None and d["summary"]["net_value"] is None
    assert _lines(admin, st["id"])["items"][0]["unit_price"] == 10


# ------------------------------------------------------------------ الإعداد الأول
SETUP = {"username": "owner", "password": "Str0ng-Pass!", "full_name": "المالك", "company_name": "شركتي",
         "warehouse_name": "المخزن الرئيسي", "warehouse_code": "main"}


def test_first_run_setup_flow_and_closes_itself(anon):
    assert anon.get("/api/setup/status").json() == {"needs_setup": True}
    r = anon.post("/api/setup", json={**SETUP, "password": "123"})
    assert r.status_code == 422
    r = anon.post("/api/setup", json=SETUP)
    assert r.status_code == 201, r.text
    assert anon.get("/api/setup/status").json() == {"needs_setup": False}
    assert anon.post("/api/setup", json={**SETUP, "username": "second"}).status_code == 409
    me = anon.post("/api/auth/login", json={"username": "owner", "password": SETUP["password"]})
    assert me.status_code == 200
    wh = anon.get("/api/locations/warehouses").json()
    wh = wh if isinstance(wh, list) else wh.get("items", wh)
    assert wh[0]["code"] == "MAIN" and wh[0]["default_location_code"] == "MAIN-RCV"
    assert anon.get("/api/company").json()["name"] == "شركتي"


def test_setup_refused_when_users_exist(admin, anon):
    assert anon.get("/api/setup/status").json()["needs_setup"] is False
    assert anon.post("/api/setup", json=SETUP).status_code == 409


def test_setup_race_creates_a_single_admin(anon, session_factory):
    import threading
    from fastapi.testclient import TestClient
    from app.main import app
    from .conftest import CSRF

    out = []
    bar = threading.Barrier(4)

    def go(i):
        with TestClient(app, base_url="http://testserver", headers=CSRF, raise_server_exceptions=False) as c:
            bar.wait()
            out.append(c.post("/api/setup", json={**SETUP, "username": f"owner{i}"}).status_code)

    ts = [threading.Thread(target=go, args=(i,)) for i in range(4)]
    [t.start() for t in ts]; [t.join() for t in ts]
    assert len(out) == 4, out   # ولا thread مات بخطأ غير متوقع (500)
    assert sorted(out).count(201) == 1 and set(out) <= {201, 409}, out
    s = session_factory()
    try:
        assert s.query(models.User).count() == 1
    finally:
        s.close()


# ------------------------------------------------------------------ العنوان العام + اللوجو
def test_public_url_setting_https_only_and_used_in_print(admin, login):
    assert admin.get("/api/settings/public-url").json()["source"] == "none"
    for bad in ("http://x.com", "https://x.com/app", "https://x.com?a=1", "https://user@x.com", "ftp://x.com", "x.com", "https://x .com", "https://x.com#f"):
        assert admin.put("/api/settings/public-url", json={"public_base_url": bad}).status_code == 400, bad
    ok = admin.put("/api/settings/public-url", json={"public_base_url": "HTTPS://Stock.Example.com/"}).json()
    assert ok == {"public_base_url": "https://stock.example.com", "source": "db"}
    wh, a, _b = _wh(admin)
    p = make_product(admin, locations=[(a["id"], 3)])
    d = admin.post("/api/stock/out", json={"items": [{"product_id": p["id"], "quantity": 1, "location_id": a["id"]}]}).json()
    assert admin.get(f"/api/stock/out/{d['id']}/print").json()["public_base_url"] == "https://stock.example.com"
    assert login("nocompany", ["products.view"]).get("/api/settings/public-url").status_code == 403
    assert admin.put("/api/settings/public-url", json={"public_base_url": ""}).json()["source"] == "none"
    rows = admin.get("/api/audit-log", params={"entity_type": "settings"}).json()["items"]
    assert len(rows) == 2


PNG = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR" + b"\x00" * 40


def test_logo_png_jpg_only_max_1mb(admin):
    up = lambda data, name="l.png": admin.post("/api/company/logo", files={"file": (name, data, "image/png")})
    assert up(PNG).status_code == 200
    assert up(b"\xff\xd8\xff\xe0" + b"0" * 30, "l.jpg").status_code == 200
    webp = b"RIFF\x00\x00\x00\x00WEBPVP8 " + b"0" * 30
    assert up(webp, "l.webp").status_code == 400
    assert up(b"<svg xmlns='http://www.w3.org/2000/svg'/>", "l.svg").status_code == 400
    assert up(PNG + b"0" * (1024 * 1024)).status_code == 413


# ------------------------------------------------------------------ استيراد Excel
def _import(c, rows):
    buf = io.BytesIO()
    pd.DataFrame(rows).to_excel(buf, index=False)
    return c.post("/api/excel/products/import", files={"file": ("p.xlsx", buf.getvalue(), XLSX)})


def test_import_5000_rows_in_batches(admin, db):
    wh, a, _b = _wh(admin)
    rows = [{"الاسم": f"منتج {i}", "SKU": f"SK{i}", "السعر": 5, "الموقع": "W1-A1", "الكمية": 2} for i in range(5000)]
    t0 = time.time()
    r = _import(admin, rows)
    took = time.time() - t0
    out = r.json()
    assert r.status_code == 200 and out["created"] == 5000 and out["errors"] == [] and out["errors_total"] == 0, out["message"]
    assert took < 120, took
    assert db.query(models.Product).count() == 5000
    assert round(float(db.query(models.Product).with_entities(__import__("sqlalchemy").func.sum(models.Product.quantity)).scalar()), 3) == 10000
    # إعادة الاستيراد بنفس الملف = تحديث (مطابقة SKU) من غير تكرار
    again = _import(admin, rows[:50]).json()
    assert again["created"] == 0 and again["updated"] == 50


def test_import_errors_are_structured_and_capped(admin):
    _wh(admin)
    rows = [{"الاسم": f"صف {i}", "الكمية": 5} for i in range(600)]   # كمية من غير موقع = خطأ
    rows.append({"الاسم": "سليم", "الكمية": 0})
    out = _import(admin, rows).json()
    assert out["created"] == 1 and out["errors_total"] == 600 and len(out["errors"]) == 500 and out["errors_truncated"] is True
    e = out["errors"][0]
    assert set(e) == {"row", "column", "message"} and e["row"] == 2 and e["column"] == "الموقع"


def test_import_name_matching_only_inside_file(admin):
    _, a, _b = _wh(admin)
    make_product(admin, "مكرر", price=1)
    out = _import(admin, [{"الاسم": "مكرر", "السعر": 2}]).json()
    assert out["created"] == 1 and out["updated"] == 0  # منتج بلا ID/SKU/باركود = جديد حتى لو الاسم موجود
    out = _import(admin, [{"الاسم": "ملفي", "الموقع": "W1-A1", "الكمية": 1}, {"الاسم": "ملفي", "الموقع": "W1-B1", "الكمية": 2}]).json()
    assert out["created"] == 1 and out["errors_total"] == 0  # نفس الاسم جوه الملف = منتج واحد بمكانين


def test_import_audited_and_price_change_logged(admin):
    _, a, _b = _wh(admin)
    p = make_product(admin, "سعر", price=10, sku="PX")
    _import(admin, [{"الاسم": "سعر", "SKU": "PX", "السعر": 12}])
    acts = [(r["entity_type"], r["action"]) for r in admin.get("/api/audit-log", params={"page_size": 50}).json()["items"]]
    assert ("import", "excel_import") in acts and ("product", "price_change") in acts and p

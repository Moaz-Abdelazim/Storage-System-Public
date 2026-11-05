"""طباعة مستند كبير (500 صنف): الـ API بيرجّع كل الأسطر في طلب واحد وبسرعة، والعناوين/العبارات من السيرفر."""
import time

from .helpers import make_location, make_product, make_warehouse


def test_print_payload_with_500_lines_and_labels(admin):
    wh = make_warehouse(admin)
    loc = make_location(admin, warehouse_id=wh["id"], code="A1")
    p = make_product(admin, "صنف", price=2, locations=[(loc["id"], 1000)])
    items = [{"product_id": p["id"], "quantity": 1, "location_id": loc["id"]} for _ in range(500)]
    d = admin.post("/api/stock/out", json={"items": items, "status": "reserved", "discount_type": "percent", "discount_value": 10, "tax_type": "percent", "tax_value": 14}).json()
    t0 = time.time()
    r = admin.get(f"/api/stock/out/{d['id']}/print")
    took = time.time() - t0
    assert r.status_code == 200 and took < 3, took
    body = r.json()
    assert len(body["items"]) == 500 and body["totals"]["items_count"] == 500
    t = body["totals"]
    assert (t["subtotal"], t["net_amount"], t["grand_total"]) == (1000, 900, 1026)   # 1000 - 10% = 900، + 14% = 1026
    assert body["labels"] == {"title": "إذن حجز", "banner": "إذن حجز — الكميات محجوزة ولم تخرج من المخزن", "notice": "مستند مخزني غير ضريبي"}
    assert body["items"][0]["location_code"] == "W1-A1" and body["items"][0]["warehouse_name"]
    admin.post(f"/api/stock/out/{d['id']}/void", json={"reason": "اختبار الطباعة"})
    v = admin.get(f"/api/stock/out/{d['id']}/print").json()
    assert v["labels"]["banner"] == "ملغاة" and v["invoice"]["status"] == "voided" and v["invoice"]["void_reason"] == "اختبار الطباعة"

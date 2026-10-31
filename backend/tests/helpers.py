"""دوال مساعدة لبناء بيانات الاختبار عن طريق الـ API نفسه."""


def make_warehouse(c, code="W1", name="مخزن رئيسي"):
    r = c.post("/api/locations/warehouses", json={"name": name, "code": code})
    assert r.status_code == 200, r.text
    return r.json()


def make_location(c, warehouse_id=None, parent_id=None, code="A1", name="رف"):
    body = {"name": name, "code": code}
    if parent_id:
        body["parent_id"] = parent_id
    else:
        body["warehouse_id"] = warehouse_id
    r = c.post("/api/locations", json=body)
    assert r.status_code == 200, r.text
    return r.json()


def make_product(c, name="منتج", price=10, locations=None, **extra):
    body = {"name": name, "price": price, **extra}
    if locations:
        body["locations"] = [{"location_id": lid, "quantity": q} for lid, q in locations]
        body["quantity"] = sum(q for _, q in locations)
    r = c.post("/api/products", json=body)
    assert r.status_code == 200, r.text
    return r.json()


def get_product(c, pid):
    r = c.get(f"/api/products/{pid}")
    assert r.status_code == 200, r.text
    return r.json()


def location_qty(product_json, location_id):
    return next((l["quantity"] for l in product_json["locations"] if l["location_id"] == location_id), 0)

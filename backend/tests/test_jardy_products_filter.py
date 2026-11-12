"""فلتر المنتجات بالمخزن والموقع (هرمي): من غير تكرار منتج، والكمية جوه النطاق."""
from .helpers import make_location, make_product, make_warehouse


def _get(c, **params):
    return c.get("/api/products", params={"page": 1, "page_size": 100, **params})


def _ids(r):
    assert r.status_code == 200, r.text
    return sorted(p["name"] for p in r.json()["items"])


def _tree(c):
    w1, w2 = make_warehouse(c, "W1", "مخزن 1"), make_warehouse(c, "W2", "مخزن 2")
    a = make_location(c, warehouse_id=w1["id"], code="A")
    a1 = make_location(c, parent_id=a["id"], code="A1", name="رف 1")
    a2 = make_location(c, parent_id=a["id"], code="A2", name="رف 2")
    b = make_location(c, warehouse_id=w2["id"], code="B")
    make_product(c, "في-A1-بس", locations=[(a1["id"], 4)])
    make_product(c, "في-A1-و-A2", locations=[(a1["id"], 2), (a2["id"], 3)])   # لازم يظهر مرة واحدة بس
    make_product(c, "في-مخزن2", locations=[(b["id"], 7)])
    make_product(c, "من-غير-مكان")
    return w1, w2, a, a1, a2, b


def test_filter_by_warehouse_no_duplicates(admin):
    w1, w2, *_ = _tree(admin)
    assert _ids(_get(admin, **{"warehouse_id": w1["id"]})) == sorted(["في-A1-و-A2", "في-A1-بس"])
    assert _ids(_get(admin, **{"warehouse_id": w2["id"]})) == ["في-مخزن2"]
    assert len(_ids(_get(admin))) == 4


def test_filter_by_location_includes_descendants_and_scoped_quantity(admin):
    w1, w2, a, a1, a2, b = _tree(admin)
    r = _get(admin, **{"warehouse_id": w1["id"], "location_id": a["id"]}).json()["items"]
    assert sorted(p["name"] for p in r) == sorted(["في-A1-و-A2", "في-A1-بس"])               # الأب بيشمل الأبناء
    assert {p["name"]: p["scoped_quantity"] for p in r} == {"في-A1-و-A2": 5, "في-A1-بس": 4}
    r = _get(admin, **{"location_id": a2["id"]}).json()["items"]
    assert [(p["name"], p["scoped_quantity"]) for p in r] == [("في-A1-و-A2", 3)]    # مستوى أعمق: الكمية في الموقع ده بس
    assert _ids(_get(admin, **{"location_id": a1["id"], "stock": "ok"})) == sorted(["في-A1-و-A2", "في-A1-بس"])


def test_filter_combines_with_search_and_pagination(admin):
    w1, *_ = _tree(admin)
    assert _ids(_get(admin, **{"warehouse_id": w1["id"], "search": "A2"})) == ["في-A1-و-A2"]
    r = _get(admin, **{"warehouse_id": w1["id"], "page_size": 1, "page": 2}).json()
    assert r["total"] == 2 and len(r["items"]) == 1


def test_unknown_warehouse_or_location_returns_empty_or_error_not_everything(admin):
    _tree(admin)
    r = _get(admin, **{"warehouse_id": 9999})
    assert r.status_code in (200, 400, 404)
    if r.status_code == 200:
        assert r.json()["items"] == []

from app import models


def _role(admin, name="مشرف", perms=("products.view",), **kw):
    return admin.post("/api/roles", json={"name": name, "permissions": list(perms), **kw})


def test_create_list_update_delete_role(admin):
    r = _role(admin, "  مشرف   المخزن ", ["products.view", "products.edit"], description="وصف")
    assert r.status_code == 200, r.text
    role = r.json()
    assert role["name"] == "مشرف المخزن" and role["users_count"] == 0 and role["is_system"] is False
    assert role["permissions"] == ["products.edit", "products.view"]

    listing = admin.get("/api/roles").json()
    assert listing[0]["is_system"] is True and listing[0]["name"] == "مدير النظام"
    assert len(listing[0]["permissions"]) > 30  # كل الصلاحيات ضمنيًا

    upd = admin.put(f"/api/roles/{role['id']}", json={"name": "مشرف", "permissions": ["products.view"]})
    assert upd.status_code == 200 and upd.json()["permissions"] == ["products.view"]
    assert admin.delete(f"/api/roles/{role['id']}").status_code == 200
    assert all(x["id"] != role["id"] for x in admin.get("/api/roles").json())


def test_role_name_unique_case_insensitive_and_validated(admin):
    assert _role(admin, "Auditor").status_code == 200
    assert _role(admin, "auditor").status_code == 400
    assert _role(admin, "مدير النظام").status_code == 400
    assert admin.post("/api/roles", json={"name": " ", "permissions": []}).status_code == 422
    assert admin.post("/api/roles", json={"name": "x", "permissions": []}).status_code == 422


def test_role_permissions_must_be_known_and_closed(admin):
    r = _role(admin, "غلط", ["nope.x"])
    assert r.status_code == 400 and "غير معروفة" in r.json()["detail"]
    r = _role(admin, "ناقص", ["products.price"])
    assert r.status_code == 400 and "محتاجة" in r.json()["detail"]
    assert _role(admin, "تقارير", ["reports.view"]).status_code == 400  # محتاجة finance.view
    assert _role(admin, "كامل", ["reports.view", "finance.view"]).status_code == 200


def test_system_role_is_immutable(admin, db):
    sid = db.query(models.Role).filter_by(is_system=True).one().id
    assert admin.put(f"/api/roles/{sid}", json={"name": "جديد", "permissions": []}).status_code == 400
    assert admin.patch(f"/api/roles/{sid}/active", json={"is_active": False}).status_code == 400
    assert admin.delete(f"/api/roles/{sid}").status_code == 400


def test_delete_role_with_users_is_409_with_count(admin, make_user, make_role):
    rid = make_role("موظفين", ["products.view"])
    make_user("u1", rid)
    make_user("u2", rid)
    r = admin.delete(f"/api/roles/{rid}")
    assert r.status_code == 409 and "2" in r.json()["detail"]


def test_unknown_role_is_404(admin):
    assert admin.put("/api/roles/9999", json={"name": "xx", "permissions": []}).status_code == 404
    assert admin.delete("/api/roles/9999").status_code == 404


def test_permission_catalog_endpoint(admin):
    groups = admin.get("/api/roles/permissions").json()["groups"]
    assert any(g["group"] == "المنتجات" for g in groups)
    flat = {p["key"]: p for g in groups for p in g["permissions"]}
    assert flat["products.price"]["requires"] == ["products.edit"]


def test_permission_change_takes_effect_immediately(admin, login, make_role):
    rid = make_role("مؤقت", ["products.view"])
    user = login("worker1", rid)
    assert user.get("/api/products").status_code == 200
    assert user.post("/api/categories", json={"name": "x"}).status_code == 403
    admin.put(f"/api/roles/{rid}", json={"name": "مؤقت", "permissions": ["products.view", "catalog.manage"]})
    assert user.post("/api/categories", json={"name": "x"}).status_code == 200  # نفس الجلسة، من غير إعادة دخول
    admin.put(f"/api/roles/{rid}", json={"name": "مؤقت", "permissions": []})
    assert user.get("/api/products").status_code == 403


def test_disabled_role_blocks_login_and_existing_session(admin, login, make_role, anon):
    rid = make_role("موقوف", ["products.view"])
    user = login("worker1", rid)
    assert user.get("/api/products").status_code == 200
    assert admin.patch(f"/api/roles/{rid}/active", json={"is_active": False}).status_code == 200
    # الجلسة المفتوحة اتقفلت فورًا
    assert user.get("/api/products").status_code == 401
    r = anon.post("/api/auth/login", json={"username": "worker1", "password": "Passw0rd!x"})
    assert r.status_code == 403 and "الدور" in r.json()["detail"]
    admin.patch(f"/api/roles/{rid}/active", json={"is_active": True})
    assert anon.post("/api/auth/login", json={"username": "worker1", "password": "Passw0rd!x"}).status_code == 200


def test_role_disabled_after_login_gives_403_but_me_and_logout_work(admin, login, make_role, db):
    rid = make_role("هيتوقف", ["products.view"])
    user = login("worker1", rid)
    db.query(models.Role).filter_by(id=rid).update({"is_active": False})  # تعطيل مباشر من غير قفل الجلسات
    db.commit()
    r = user.get("/api/products")
    assert r.status_code == 403 and "موقوف" in r.json()["detail"]
    me = user.get("/api/auth/me")
    assert me.status_code == 200 and me.json()["permissions"] == [] and me.json()["role_active"] is False
    assert user.post("/api/auth/logout").status_code == 200


def test_me_and_login_expose_permissions(admin, login, make_role, anon):
    me = admin.get("/api/auth/me").json()
    assert me["is_admin"] is True and "users.manage" in me["permissions"] and me["role"] == "مدير النظام"
    rid = make_role("موظف مبيعات", ["stock_out.create"])
    c = login("sales1", rid)
    me = c.get("/api/auth/me").json()
    assert me["permissions"] == ["stock_out.create"] and me["is_admin"] is False and me["role_id"] == rid
    r = anon.post("/api/auth/login", json={"username": "sales1", "password": "Passw0rd!x"}).json()
    assert r["permissions"] == ["stock_out.create"] and r["role"] == "موظف مبيعات"


def test_roles_lite_lists_only_active_roles(admin, make_role):
    make_role("فعّال", [])
    make_role("موقوف", [], is_active=False)
    names = [r["name"] for r in admin.get("/api/auth/roles-lite").json()]
    assert "فعّال" in names and "موقوف" not in names and "مدير النظام" in names

import threading

from app import models


def _uid(db, username):
    return db.query(models.User).filter_by(username=username).one().id


def _new_user(c, username, role_id, **kw):
    return c.post("/api/auth/users", json={"username": username, "password": "Passw0rd!x", "role_id": role_id, **kw})


def _system_role_id(db):
    from app.services.roles import ensure_system_role

    return ensure_system_role(db).id


# ------------------------------------------------ آخر أدمن
def test_sole_admin_cannot_demote_disable_or_delete_self(admin, db, make_role):
    me = _uid(db, "admin1")
    rid = make_role("عادي", [])
    r = admin.put(f"/api/auth/users/{me}", json={"role_id": rid})
    assert r.status_code == 400 and "آخر أدمن" in r.json()["detail"]
    assert admin.put(f"/api/auth/users/{me}", json={"is_active": False}).status_code == 400
    assert admin.delete(f"/api/auth/users/{me}").status_code == 400


def test_admin_can_demote_another_admin_when_not_last(admin, make_user, db, make_role):
    make_user("admin2", "admin")
    rid = make_role("عادي", [])
    other = _uid(db, "admin2")
    assert admin.put(f"/api/auth/users/{other}", json={"role_id": rid}).status_code == 200
    assert admin.put(f"/api/auth/users/{_uid(db, 'admin1')}", json={"role_id": rid}).status_code == 400  # بقى آخر أدمن


def test_two_admins_disabling_each_other_concurrently_never_leaves_zero(login, db):
    a, b = login("adminA", "admin"), login("adminB", "admin")
    ida, idb = _uid(db, "adminA"), _uid(db, "adminB")
    barrier, codes = threading.Barrier(2), []

    def go(client, target):
        barrier.wait()
        codes.append(client.put(f"/api/auth/users/{target}", json={"is_active": False}).status_code)

    ts = [threading.Thread(target=go, args=(a, idb)), threading.Thread(target=go, args=(b, ida))]
    [t.start() for t in ts]
    [t.join() for t in ts]
    db.expire_all()
    active_admins = db.query(models.User).join(models.Role).filter(models.Role.is_system.is_(True), models.User.is_active.is_(True)).count()
    assert active_admins >= 1, codes


# ------------------------------------------------ مستخدم عادي معاه users.manage
def _manager(login, make_role, perms=("users.manage", "products.view")):
    return login("manager1", make_role("مدير مستخدمين", list(perms)))


def test_non_admin_cannot_touch_admin_accounts(login, make_user, make_role, db):
    mgr = _manager(login, make_role)
    make_user("boss", "admin")
    boss = _uid(db, "boss")
    assert mgr.put(f"/api/auth/users/{boss}", json={"full_name": "x"}).status_code == 403
    assert mgr.put(f"/api/auth/users/{boss}", json={"password": "N3wPassw0rd!"}).status_code == 403
    assert mgr.put(f"/api/auth/users/{boss}", json={"is_active": False}).status_code == 403
    assert mgr.delete(f"/api/auth/users/{boss}").status_code == 403


def test_non_admin_cannot_assign_system_or_bigger_roles(login, make_role, db):
    mgr = _manager(login, make_role)
    assert _new_user(mgr, "usr1", _system_role_id(db)).status_code == 403
    bigger = make_role("أكبر", ["products.view", "catalog.manage"])
    r = _new_user(mgr, "usr2", bigger)
    assert r.status_code == 403 and "أكبر" in r.json()["detail"]


def test_non_admin_can_assign_subset_role(login, make_role, db):
    mgr = _manager(login, make_role)
    small = make_role("أصغر", ["products.view"])
    r = _new_user(mgr, "usr3", small)
    assert r.status_code == 200 and r.json()["role_name"] == "أصغر" and r.json()["is_admin"] is False


def test_non_admin_cannot_change_own_role_or_disable_or_delete_self(login, make_role, db):
    mgr = _manager(login, make_role)
    me = _uid(db, "manager1")
    small = make_role("أصغر", ["products.view"])
    assert mgr.put(f"/api/auth/users/{me}", json={"role_id": small}).status_code == 403
    assert mgr.put(f"/api/auth/users/{me}", json={"is_active": False}).status_code == 400
    assert mgr.delete(f"/api/auth/users/{me}").status_code == 400


def test_non_admin_cannot_promote_existing_user_to_bigger_role(login, make_role, make_user, db):
    mgr = _manager(login, make_role)
    small = make_role("أصغر", ["products.view"])
    bigger = make_role("أكبر", ["products.view", "catalog.manage"])
    make_user("emp1", small)
    assert mgr.put(f"/api/auth/users/{_uid(db, 'emp1')}", json={"role_id": bigger}).status_code == 403


# ------------------------------------------------ أدوار مستخدم
def test_cannot_assign_inactive_or_unknown_role(admin, make_role):
    dead = make_role("ميت", [], is_active=False)
    assert _new_user(admin, "usr1", dead).status_code == 400
    assert _new_user(admin, "usr2", 9999).status_code == 404


def test_changing_role_applies_immediately_without_closing_session(admin, login, make_user, make_role, db):
    r1 = make_role("ر1", ["products.view"])
    r2 = make_role("ر2", ["catalog.manage"])
    emp = login("emp1", r1)
    assert emp.get("/api/products").status_code == 200
    assert admin.put(f"/api/auth/users/{_uid(db, 'emp1')}", json={"role_id": r2}).status_code == 200
    assert emp.get("/api/products").status_code == 403
    assert emp.post("/api/categories", json={"name": "n"}).status_code == 200  # نفس الجلسة


def test_users_list_filter_and_stats(admin, make_user, make_role, db):
    rid = make_role("فني", [])
    make_user("t1", rid)
    make_user("t2", rid, is_active=False)
    rows = admin.get("/api/auth/users", params={"role_id": rid}).json()
    assert {r["username"] for r in rows} == {"t1", "t2"} and rows[0]["role_name"] == "فني"
    stats = admin.get("/api/auth/users/stats").json()
    assert stats["total"] == 3 and stats["admins"] == 1 and stats["inactive"] == 1
    assert {"role_id": rid, "name": "فني", "count": 2} in stats["by_role"]


def test_delete_user_with_transfers_and_sessions_works(admin, login, make_role, db):
    """ريجريشن: حذف مستخدم عمل نقل/أنهى جلسات كان بيكسر على FK في MySQL."""
    from .helpers import make_location, make_product, make_warehouse

    rid = make_role("ناقل", ["transfers.create", "products.view", "locations.view"])
    mover = login("mover1", rid)
    wh = make_warehouse(admin)
    a, b = make_location(admin, warehouse_id=wh["id"], code="A"), make_location(admin, warehouse_id=wh["id"], code="B")
    p = make_product(admin, locations=[(a["id"], 5)])
    assert mover.post("/api/locations/transfer", json={"product_id": p["id"], "from_location_id": a["id"], "to_location_id": b["id"], "quantity": 1}).status_code == 200
    uid = _uid(db, "mover1")
    sid = next(l["id"] for l in admin.get("/api/auth/login-logs", params={"status": "active"}).json()["items"] if "mover1" in l["username"])
    admin.post(f"/api/auth/login-logs/{sid}/revoke")
    assert admin.delete(f"/api/auth/users/{uid}").status_code == 200
    items = admin.get("/api/locations/transfers").json()
    assert "mover1" in str(items)  # الاسم فضل في السجل كنسخة

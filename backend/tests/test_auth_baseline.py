"""اختبارات خط الأساس (characterization) للـ auth والأمان الأساسي - بتثبّت السلوك الحالي قبل أي تعديل."""
import pytest

from app import models


def test_health_is_open(anon):
    r = anon.get("/api/health")
    assert r.status_code == 200 and r.json()["status"] == "ok"


def test_protected_route_requires_login(anon):
    assert anon.get("/api/products").status_code == 401


def test_login_me_logout_cycle(login):
    c = login("admin1", "admin")
    me = c.get("/api/auth/me")
    assert me.status_code == 200 and me.json()["username"] == "admin1"
    assert c.post("/api/auth/logout").status_code == 200
    # الجلسة اتقفلت في الـ DB: حتى لو الكوكي لسه معانا، الطلب لازم يتحرف
    assert c.get("/api/auth/me").status_code == 401


def test_wrong_password_and_unknown_user_same_message(anon, make_user):
    make_user("admin1", "admin")
    a = anon.post("/api/auth/login", json={"username": "admin1", "password": "wrong-pass"})
    b = anon.post("/api/auth/login", json={"username": "nobody", "password": "wrong-pass"})
    assert a.status_code == b.status_code == 401
    assert a.json()["detail"] == b.json()["detail"]


def test_inactive_user_cannot_login(anon, make_user):
    make_user("off1", "staff", is_active=False)
    r = anon.post("/api/auth/login", json={"username": "off1", "password": "Passw0rd!x"})
    assert r.status_code == 403


def test_admin_revokes_session_takes_effect_immediately(login, admin):
    victim = login("staff1", "staff")
    assert victim.get("/api/auth/me").status_code == 200
    logs = admin.get("/api/auth/login-logs", params={"status": "active"}).json()["items"]
    sid = next(l["id"] for l in logs if l["username"].startswith("staff1"))
    assert admin.post(f"/api/auth/login-logs/{sid}/revoke").status_code == 200
    assert victim.get("/api/auth/me").status_code == 401


def test_staff_cannot_use_admin_endpoints(staff):
    assert staff.get("/api/auth/users").status_code == 403
    assert staff.get("/api/auth/login-logs").status_code == 403
    assert staff.get("/api/analytics/kpis").status_code == 403


def test_brute_force_limiter_locks_after_failures(anon, make_user):
    make_user("admin1", "admin")
    codes = [anon.post("/api/auth/login", json={"username": "admin1", "password": "bad-pass-x"}).status_code for _ in range(6)]
    assert codes[:5] == [401] * 5 and codes[5] == 429
    # حتى الباسورد الصح بيتحظر وقت القفل
    assert anon.post("/api/auth/login", json={"username": "admin1", "password": "Passw0rd!x"}).status_code == 429


def test_csrf_header_required_for_unsafe_methods(make_user):
    from fastapi.testclient import TestClient
    from app.main import app

    make_user("admin1", "admin")
    with TestClient(app, base_url="http://testserver") as c:  # من غير هيدر CSRF
        r = c.post("/api/auth/login", json={"username": "admin1", "password": "Passw0rd!x"})
        assert r.status_code == 403


def test_foreign_origin_rejected(anon):
    r = anon.post("/api/auth/login", json={"username": "x", "password": "y"}, headers={"Origin": "https://evil.example"})
    assert r.status_code == 403


def test_disallowed_host_rejected(make_user):
    from fastapi.testclient import TestClient
    from app.main import app

    with TestClient(app, base_url="http://evil.example") as c:
        assert c.get("/api/health").status_code == 400


def test_user_deletion_keeps_history_name(admin, login, db):
    """حذف مستخدم بسجلات مرتبطة مش بيكسر - والاسم بيفضل في سجل الدخول."""
    login("temp1", "staff")
    uid = db.query(models.User).filter_by(username="temp1").one().id
    assert admin.delete(f"/api/auth/users/{uid}").status_code == 200
    logs = admin.get("/api/auth/login-logs").json()["items"]
    assert any("temp1" in l["username"] for l in logs)


def test_excel_template_requires_auth(anon):
    assert anon.get("/api/excel/products/template").status_code == 401

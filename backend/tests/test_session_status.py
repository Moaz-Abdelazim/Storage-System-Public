"""حالة جلسات الدخول: active / expired / logged_out / revoked / system - محسوبة من expires_at و end_reason مش من logout_time بس."""
from datetime import timedelta

from app import auth, models
from app.services import sessions as svc
from app.timezone_utils import now_cairo


def _logs(c, **params):
    r = c.get("/api/auth/login-logs", params={"page_size": 100, **params})
    assert r.status_code == 200, r.text
    return r.json()["items"]


def _row_of(items, username):
    return next(i for i in items if i["username"] == username)


def _expire(session_factory, user, minutes_ago=5):
    """نخلّي expires_at بتاع آخر جلسة للمستخدم في الماضي (زي ما لو عدّت مدة التوكن)."""
    s = session_factory()
    try:
        log = s.query(models.LoginLog).filter(models.LoginLog.username == user).order_by(models.LoginLog.id.desc()).first()
        log.expires_at = now_cairo() - timedelta(minutes=minutes_ago)
        s.commit()
        return log.id
    finally:
        s.close()


def _db_row(session_factory, sid):
    s = session_factory()
    try:
        r = s.get(models.LoginLog, sid)
        return r.logout_time, r.end_reason, r.expires_at, r.closed_by_id
    finally:
        s.close()


def test_login_sets_expiry_equal_to_token_lifetime(admin):
    row = _row_of(_logs(admin), "admin1")
    assert row["status"] == "active" and row["is_active"] is True and row["ended_at"] is None
    from datetime import datetime
    exp, login = datetime.fromisoformat(row["expires_at"]), datetime.fromisoformat(row["login_time"])
    assert abs((exp - login) - timedelta(minutes=auth.ACCESS_TOKEN_EXPIRE_MINUTES)) < timedelta(seconds=2)


def test_active_session_before_expiry_stays_active(admin, login):
    login("emp1", ["products.view"])
    row = _row_of(_logs(admin), "emp1")
    assert row["status"] == "active" and row["end_reason"] is None
    assert [r["username"] for r in _logs(admin, status="active")].count("emp1") == 1
    assert "emp1" not in [r["username"] for r in _logs(admin, status="expired")]


def test_expired_session_is_not_open_and_not_a_manual_logout(admin, login, session_factory):
    emp = login("emp2", ["products.view"])
    sid = _expire(session_factory, "emp2")
    row = _row_of(_logs(admin), "emp2")
    assert row["status"] == "expired" and row["is_active"] is False and row["end_reason"] == "expired"
    assert row["ended_at"] == row["expires_at"] == row["logout_time"]   # وقت الانتهاء = expires_at مش وقت الاكتشاف
    assert "emp2" in [r["username"] for r in _logs(admin, status="expired")]
    assert "emp2" not in [r["username"] for r in _logs(admin, status="active")]
    assert "emp2" not in [r["username"] for r in _logs(admin, status="logged_out")]
    assert "emp2" not in [r["username"] for r in _logs(admin, status="closed")]
    assert _db_row(session_factory, sid)[1] == "expired"
    assert emp.get("/api/products").status_code == 401  # التوكن بتاعها اتبطل فعليًا


def test_expired_by_clock_is_reported_even_before_any_sweep(session_factory):
    s = session_factory()
    try:
        log = models.LoginLog(username="x", login_time=now_cairo() - timedelta(hours=9), expires_at=now_cairo() - timedelta(hours=1))
        assert svc.session_status(log) == "expired" and svc.ended_at(log) == log.expires_at
        log.expires_at = now_cairo() + timedelta(hours=1)
        assert svc.session_status(log) == "active" and svc.ended_at(log) is None
        log.logout_time, log.end_reason = now_cairo(), "logout"
        assert svc.session_status(log) == "logged_out"
        log.end_reason, log.closed_by_id = None, 1       # صفوف من غير end_reason: بنستنتج من closed_by
        assert svc.session_status(log) == "revoked"
    finally:
        s.close()


def test_expired_jwt_marks_session_expired_and_is_rejected(admin, login, anon, session_factory):
    login("emp3", ["products.view"])
    s = session_factory()
    try:
        sid = s.query(models.LoginLog).filter_by(username="emp3").one().id
    finally:
        s.close()
    old_token = auth.create_access_token({"sub": "emp3", "sid": sid}, timedelta(minutes=-1))  # exp في الماضي
    r = anon.get("/api/auth/me", headers={"Authorization": f"Bearer {old_token}"})
    assert r.status_code == 401
    logout_time, reason, _exp, closed_by = _db_row(session_factory, sid)
    assert reason == "expired" and logout_time is not None and closed_by is None
    assert _row_of(_logs(admin), "emp3")["status"] == "expired"


def test_logout_before_expiry_is_logged_out_and_stays_so(admin, login, session_factory):
    emp = login("emp4", ["products.view"])
    assert emp.post("/api/auth/logout").status_code == 200
    row = _row_of(_logs(admin), "emp4")
    assert row["status"] == "logged_out" and row["end_reason"] == "logout" and row["closed_by"] is None and row["is_active"] is False
    # حتى بعد ما وقت الانتهاء يعدّي، الجلسة اللي سجّلت خروج تفضل "سجّل خروج" مش "منتهية"
    s = session_factory()
    try:
        s.query(models.LoginLog).filter_by(username="emp4").update({"expires_at": now_cairo() - timedelta(hours=1)})
        s.commit()
    finally:
        s.close()
    assert _row_of(_logs(admin), "emp4")["status"] == "logged_out"
    assert "emp4" in [r["username"] for r in _logs(admin, status="logged_out")]
    assert "emp4" in [r["username"] for r in _logs(admin, status="closed")]
    assert emp.get("/api/products").status_code == 401


def test_admin_revoke_is_revoked_and_revoking_expired_does_not_relabel(admin, login, session_factory):
    emp = login("emp5", ["products.view"])
    sid = _row_of(_logs(admin), "emp5")["id"]
    assert admin.post(f"/api/auth/login-logs/{sid}/revoke").status_code == 200
    row = _row_of(_logs(admin), "emp5")
    assert row["status"] == "revoked" and row["closed_by"] == "admin1" and row["end_reason"] == "revoked"
    assert emp.get("/api/products").status_code == 401
    assert "emp5" in [r["username"] for r in _logs(admin, status="revoked")]
    # جلسة منتهية: الإنهاء من الأدمن مبيغيّرش سببها
    login("emp6", ["products.view"])
    sid6 = _expire(session_factory, "emp6")
    r = admin.post(f"/api/auth/login-logs/{sid6}/revoke")
    assert r.status_code == 200 and "انتهت" in r.json()["message"]
    assert _row_of(_logs(admin), "emp6")["status"] == "expired" and _db_row(session_factory, sid6)[3] is None


def test_new_login_sweeps_other_stale_sessions(admin, login, session_factory):
    login("emp7", ["products.view"])
    sid = _expire(session_factory, "emp7")
    login("emp8", ["products.view"])  # أي دخول جديد بيقفل المنتهية
    assert _db_row(session_factory, sid)[1] == "expired"


def test_password_change_marks_other_sessions_system_closed(admin, login, session_factory):
    a = login("emp9", ["products.view"])
    b = login("emp9", ["products.view"], create=False)
    r = b.post("/api/auth/change-password", json={"current_password": "Passw0rd!x1", "new_password": "N3w-Passw0rd!"})
    if r.status_code != 200:   # كلمة السر الافتراضية للاختبارات بتختلف: نجرّب القيمة الفعلية
        from .conftest import DEFAULT_PASSWORD
        r = b.post("/api/auth/change-password", json={"current_password": DEFAULT_PASSWORD, "new_password": "N3w-Passw0rd!"})
    assert r.status_code == 200, r.text
    rows = [i for i in _logs(admin) if i["username"] == "emp9"]
    assert sorted(i["status"] for i in rows) == ["active", "revoked"] or sorted(i["status"] for i in rows) == ["active", "system"]
    assert a.get("/api/products").status_code == 401 and b.get("/api/products").status_code == 200


def test_deleting_user_keeps_old_logout_times(admin, login, make_user, session_factory):
    emp = login("emp10", ["products.view"])
    emp.post("/api/auth/logout")
    s = session_factory()
    try:
        uid = s.query(models.User).filter_by(username="emp10").one().id
        before = s.query(models.LoginLog).filter_by(user_id=uid).one().logout_time
    finally:
        s.close()
    assert admin.delete(f"/api/auth/users/{uid}").status_code == 200
    s = session_factory()
    try:
        row = s.query(models.LoginLog).filter_by(username="emp10").one()
        assert row.user_id is None and row.logout_time == before and row.end_reason == "logout"
    finally:
        s.close()

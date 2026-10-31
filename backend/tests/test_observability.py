from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from app import cli, models, observability
from app.main import app
from app.timezone_utils import now_cairo

from .conftest import CSRF


def test_health_and_request_id_header(anon):
    r = anon.get("/api/health")
    assert r.status_code == 200 and len(r.headers["x-request-id"]) >= 8
    r2 = anon.get("/api/health", headers={"X-Request-ID": "trace-12345678"})
    assert r2.headers["x-request-id"] == "trace-12345678"  # بيتقبل لو سليم
    r3 = anon.get("/api/health", headers={"X-Request-ID": "bad id with spaces!"})
    assert r3.headers["x-request-id"] != "bad id with spaces!"  # مش بنعكس مدخلات غير سليمة


def test_ready_requires_migrated_schema(engine, anon, monkeypatch):
    # create_all من غير stamp => schema مش على revision
    monkeypatch.setattr("app.main.engine", engine)
    r = anon.get("/api/ready")
    assert r.status_code == 503 and r.json() == {"status": "not_ready", "reason": "schema_not_migrated"}
    with engine.begin() as conn:
        conn.execute(text("CREATE TABLE IF NOT EXISTS alembic_version (version_num VARCHAR(32) NOT NULL)"))
        conn.execute(text("DELETE FROM alembic_version"))
        conn.execute(text("INSERT INTO alembic_version VALUES ('0001')"))
    assert anon.get("/api/ready").json() == {"status": "ready"}
    with engine.begin() as conn:
        conn.execute(text("UPDATE alembic_version SET version_num='old'"))
    assert anon.get("/api/ready").json()["reason"] == "schema_outdated"


def test_ready_reports_unreachable_db_without_details(monkeypatch):
    from sqlalchemy import create_engine

    bad = create_engine("mysql+pymysql://u:secretpw@127.0.0.1:1/none", connect_args={"connect_timeout": 1})
    res = observability.readiness(bad)
    assert res == {"ready": False, "reason": "db_unreachable"} and "secretpw" not in str(res)


def test_unhandled_error_returns_generic_500_with_request_id(login):
    admin = login("admin1", "admin")

    @app.get("/api/_boom")
    def boom():
        raise RuntimeError("internal secret detail")

    try:
        with TestClient(app, base_url="http://testserver", headers=CSRF, raise_server_exceptions=False) as c:
            r = c.get("/api/_boom")
        assert r.status_code == 500 and "internal secret" not in r.text
        assert r.json()["request_id"] == r.headers["x-request-id"]
    finally:
        app.router.routes[:] = [x for x in app.router.routes if getattr(x, "path", "") != "/api/_boom"]


def test_cleanup_retention(db, make_user):
    uid = make_user("u1", "staff")
    old, recent = now_cairo() - timedelta(days=200), now_cairo() - timedelta(days=5)
    db.add_all([
        models.LoginLog(user_id=uid, username="u1", login_time=old, logout_time=old),       # قديم ومقفول: يتمسح
        models.LoginLog(user_id=uid, username="u1", login_time=old, logout_time=None),      # قديم لكن مفتوح: يفضل
        models.LoginLog(user_id=uid, username="u1", login_time=recent, logout_time=recent),  # حديث: يفضل
        models.LoginAttempt(username="u1", ip="1.1.1.1", attempted_at=now_cairo() - timedelta(days=30)),
        models.LoginAttempt(username="u1", ip="1.1.1.1", attempted_at=now_cairo()),
    ])
    db.commit()
    before = db.query(models.LoginLog).count()
    dry = cli.cleanup(db, dry_run=True)
    assert dry["login_logs"] == 1 and dry["login_attempts"] == 1 and db.query(models.LoginLog).count() == before
    res = cli.cleanup(db)
    assert res["login_logs"] == 1 and res["login_attempts"] == 1
    assert db.query(models.LoginLog).count() == before - 1 and db.query(models.LoginAttempt).count() == 1

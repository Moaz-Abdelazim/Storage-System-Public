import io
import os
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

from app import rate_limit, security

BACKEND = Path(__file__).resolve().parent.parent


def _run(code, **env):
    base = {k: v for k, v in os.environ.items() if k not in ("APP_ENV", "SECRET_KEY", "COOKIE_SAMESITE")}
    base.update(DB_ENGINE="sqlite", **env)
    return subprocess.run([sys.executable, "-c", code], cwd=BACKEND, env=base, capture_output=True, text=True, timeout=60)


# ---------------------------------------------------------------- fail-closed
def test_unset_app_env_is_production_and_requires_secret_key():
    res = _run("import app.main")
    assert res.returncode != 0 and "SECRET_KEY" in res.stderr


def test_unset_app_env_closes_docs_and_init_db():
    code = "from app.main import app; print(app.docs_url, app.redoc_url, app.openapi_url)"
    res = _run(code, SECRET_KEY="x" * 48, ALLOWED_HOSTS="example.com")
    assert res.returncode == 0 and res.stdout.split()[:2] == ["None", "None"], res.stderr
    res = _run("import sys; from app import cli; sys.exit(cli.main(['init-db']))")
    assert res.returncode == 1 and "production" in res.stderr


def test_development_must_be_explicit():
    res = _run("from app import auth; print(auth.IS_PRODUCTION)", APP_ENV="development")
    assert res.stdout.strip() == "False"
    for weird in ("prod", "staging", "Production "):
        assert _run("from app import auth; print(auth.IS_PRODUCTION)", APP_ENV=weird, SECRET_KEY="x" * 48).stdout.strip() == "True"


def test_samesite_none_is_rejected():
    res = _run("import app.auth", APP_ENV="development", COOKIE_SAMESITE="none")
    assert res.returncode != 0 and "COOKIE_SAMESITE" in res.stderr


def test_production_rejects_short_or_default_secret():
    res = _run("import app.auth", APP_ENV="production", SECRET_KEY="short")
    assert res.returncode != 0


# ---------------------------------------------------------------- rate limits
def test_rate_limiter_unit():
    for _ in range(3):
        rate_limit.hit("k", 3, 60)
    with pytest.raises(Exception) as e:
        rate_limit.hit("k", 3, 60)
    assert getattr(e.value, "status_code", None) == 429 and "Retry-After" in e.value.headers
    rate_limit.hit("other", 3, 60)  # مفتاح مختلف مش متأثر


def test_change_password_is_rate_limited(login):
    c = login("admin1", "admin")
    codes = [c.post("/api/auth/change-password", json={"current_password": "wrong-one", "new_password": "N3wPassw0rd!"}).status_code for _ in range(11)]
    assert codes[-1] == 429 and 429 not in codes[:10]


# ---------------------------------------------------------------- client IP
class _Req:
    def __init__(self, headers, host="10.0.0.9"):
        self.headers = headers
        self.client = type("C", (), {"host": host})()


def test_client_ip_ignores_forwarded_header_by_default(monkeypatch):
    monkeypatch.setattr(security, "TRUST_PROXY_HEADERS", False, raising=False)
    assert security.client_ip(_Req({"x-forwarded-for": "1.2.3.4"})) == "10.0.0.9"


def test_client_ip_uses_last_trusted_hop(monkeypatch):
    monkeypatch.setattr(security, "TRUST_PROXY_HEADERS", True, raising=False)
    monkeypatch.setattr(security, "TRUSTED_PROXY_COUNT", 1, raising=False)
    assert security.client_ip(_Req({"x-forwarded-for": "6.6.6.6, 1.2.3.4"})) == "1.2.3.4"  # اللي ضافه الـ proxy بتاعنا


# ---------------------------------------------------------------- upload hardening
def _zip(files):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for name, data in files.items():
            z.writestr(name, data)
    return buf.getvalue()


def _post(c, content, name="p.xlsx"):
    return c.post("/api/excel/products/import", files={"file": (name, content, "application/octet-stream")})


def test_zip_bomb_rejected(admin):
    bomb = _zip({"[Content_Types].xml": "<x/>", "xl/big.xml": b"0" * (90 * 1024 * 1024)})
    assert len(bomb) < 1024 * 1024 * 2  # صغير مضغوط، ضخم بعد الفك
    r = _post(admin, bomb)
    assert r.status_code == 400


def test_non_excel_zip_and_garbage_rejected(admin):
    assert _post(admin, _zip({"a.txt": "hi"})).status_code == 400
    assert _post(admin, b"not a zip").status_code == 400
    assert _post(admin, _zip({"../evil.xml": "x", "[Content_Types].xml": "<x/>"})).status_code == 400


def test_oversize_upload_rejected(admin):
    assert _post(admin, b"0" * (10 * 1024 * 1024 + 5)).status_code == 413


# ---------------------------------------------------------------- LIKE escaping
def test_transfer_search_escapes_wildcards(admin):
    from .helpers import make_location, make_product, make_warehouse

    wh = make_warehouse(admin)
    a, b = make_location(admin, warehouse_id=wh["id"], code="A"), make_location(admin, warehouse_id=wh["id"], code="B")
    p = make_product(admin, "صنف عادي", locations=[(a["id"], 5)])
    admin.post("/api/locations/transfer", json={"product_id": p["id"], "from_location_id": a["id"], "to_location_id": b["id"], "quantity": 1})
    assert len(admin.get("/api/locations/transfers", params={"search": "صنف"}).json()["items"]) == 1
    assert admin.get("/api/locations/transfers", params={"search": "%"}).json()["items"] == []
    assert admin.get("/api/locations/transfers", params={"search": "_"}).json()["items"] == []

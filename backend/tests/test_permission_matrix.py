"""
مصفوفة الصلاحيات الحقيقية: بتتولّد من ENDPOINT_PERMS (المرجع الوحيد).
- كل route في التطبيق لازم يكون في الجدول والعكس (مفيش route من غير حماية، مفيش مدخل قديم).
- الـ dependencies الفعلية على الـ route لازم تساوي قاعدة الجدول (بتكشف أي اختلاف بين الكود والمرجع).
- سلوك حقيقي: مستخدم بالصلاحية المطلوبة بس (+requires) بيعدّي، ومستخدم عنده كل حاجة ماعدا الصلاحية دي بياخد 403 برسالة عربي.
"""
import re

import pytest
from fastapi.routing import APIRoute

from app import permissions as perms
from app.main import app

norm = lambda p: re.sub(r"\{[^}]+\}", "{}", p)
TABLE = {}
for _k, _v in perms.ENDPOINT_PERMS.items():
    _m, _, _p = _k.partition(" ")
    TABLE[f"{_m} {norm(_p.rstrip('/'))}"] = (_k, _v)


def _routes():
    """
    كل APIRoute من الـ routers نفسها (مش من app.routes: شكلها بيختلف بين إصدارات FastAPI)، ومتطابقة مع OpenAPI
    للتأكد إن مفيش router متضاف في main.py وناسيه الاختبار.
    """
    import importlib
    import pkgutil

    import app.routers as pkg

    out = {}
    for mod in pkgutil.iter_modules(pkg.__path__):
        router = getattr(importlib.import_module(f"app.routers.{mod.name}"), "router", None)
        if router is None:
            continue
        for r in router.routes:
            if isinstance(r, APIRoute):
                for m in r.methods - {"HEAD", "OPTIONS"}:
                    out[f"{m} {norm(r.path.rstrip('/'))}"] = (r, m)
    for path, ops in app.openapi()["paths"].items():
        for m in ops:
            key = f"{m.upper()} {norm(path.rstrip('/'))}"
            if m.upper() in {"GET", "POST", "PUT", "PATCH", "DELETE"} and key not in out:
                out[key] = None  # route في التطبيق مش متغطي من الـ routers (مثلًا متضاف مباشرة في main.py)
    return out


ROUTES = _routes()


def _collect_rules(dependant, acc):
    for d in dependant.dependencies:
        rule = getattr(d.call, "_perm_rule", None)
        if rule:
            acc.append(rule)
        _collect_rules(d, acc)
    return acc


def test_every_route_is_in_reference_table_and_back():
    assert set(ROUTES) == set(TABLE), {"missing_in_table": sorted(set(ROUTES) - set(TABLE)), "stale": sorted(set(TABLE) - set(ROUTES))}


@pytest.mark.parametrize("key", sorted(ROUTES))
def test_declared_dependencies_match_reference(key):
    if ROUTES[key] is None and key in ("GET /api/health", "GET /api/ready"):
        return  # الوحيد المعرّف مباشرة في main.py ومفتوح عمدًا
    if ROUTES[key] is None:
        pytest.fail(f"route {key} مش في أي router (متضاف في main.py؟) ومحتاج تغطية صريحة")
    route, _ = ROUTES[key]
    ref_key, (kind, keys) = TABLE[key]
    rules = _collect_rules(route.dependant, [])
    strong = [r for r in rules if r[0] in ("all", "any", "admin", "lenient")]
    if kind == "public":
        assert not rules, f"{ref_key} المفروض مفتوح"
    elif kind == "session":
        assert rules and not strong, f"{ref_key}: لازم session بس"
    elif kind == "lenient":
        assert any(r[0] == "lenient" for r in rules)
    elif kind == "admin":
        assert any(r[0] == "admin" for r in rules)
    elif kind == "all":
        assert {k for r in strong if r[0] == "all" for k in r[1]} == set(keys), f"{ref_key}: {strong}"
        assert not [r for r in strong if r[0] == "any"], f"{ref_key}: any-of زيادة"
    elif kind == "any":
        anys = [set(r[1]) for r in strong if r[0] == "any"]
        assert anys == [set(keys)], f"{ref_key}: {strong}"


# ---------------------------------------------------------------- سلوك حقيقي
def _dependents(key):
    return {k for k in perms.PERMISSIONS if key in perms.closure([k])}


def _fill(path):
    return re.sub(r"\{kind\}", "in", re.sub(r"\{[^}]+\}", "1", path))


def _send(client, method, path):
    kwargs = {}
    if method in ("POST", "PUT", "PATCH"):
        kwargs["json"] = {}
    return client.request(method, path, **kwargs)


ENFORCED = sorted((k, v) for k, v in perms.ENDPOINT_PERMS.items() if v[0] in ("all", "any"))


@pytest.mark.parametrize("ref_key,rule", ENFORCED, ids=[k for k, _ in ENFORCED])
def test_permission_matrix(ref_key, rule, login):
    method, _, path = ref_key.partition(" ")
    path = _fill(path)
    kind, keys = rule
    # 1) بالصلاحية المطلوبة بس (+requires): لازم يعدّي الحماية (أي رد غير 401/403/500)
    need = set(keys) if kind == "all" else {keys[0]}
    ok = login("allowed1", sorted(perms.closure(need)))
    resp = _send(ok, method, path)
    assert resp.status_code not in (401, 403) and resp.status_code < 500, (ref_key, resp.status_code, resp.text[:200])
    # 2) كل الصلاحيات ماعدا الصلاحية دي (وأي صلاحية بتتطلبها): لازم 403 برسالة عربي
    lacking = list(keys) if kind == "any" else [keys[0]]
    removed = set()
    for k in lacking:
        removed |= _dependents(k)
    denied = login("denied1", sorted(perms.ALL_KEYS - removed))
    resp = _send(denied, method, path)
    assert resp.status_code == 403, (ref_key, resp.status_code, resp.text[:200])
    assert resp.json()["detail"].startswith("ملكش صلاحية"), resp.text
    # 3) لو الـ all-of فيها أكتر من مفتاح: غياب أي واحد منهم لوحده = 403
    if kind == "all" and len(keys) > 1:
        for k in keys:
            c = login(f"no_{k.replace('.', '_')}", sorted(perms.ALL_KEYS - _dependents(k)))
            assert _send(c, method, path).status_code == 403, (ref_key, k)


ADMIN_ONLY = sorted(k for k, v in perms.ENDPOINT_PERMS.items() if v[0] == "admin")


@pytest.mark.parametrize("ref_key", ADMIN_ONLY)
def test_admin_only_endpoints(ref_key, login):
    method, _, path = ref_key.partition(" ")
    path = _fill(path)
    everything_but_admin = login("almost1", sorted(perms.ALL_KEYS))  # كل الصلاحيات المخزّنة لكن مش is_system
    assert _send(everything_but_admin, method, path).status_code == 403
    admin = login("boss1", "admin")
    assert _send(admin, method, path).status_code not in (401, 403)


SESSION_ONLY = sorted(k for k, v in perms.ENDPOINT_PERMS.items() if v[0] == "session")


@pytest.mark.parametrize("ref_key", SESSION_ONLY)
def test_session_endpoints_need_login_but_no_permission(ref_key, login, anon):
    method, _, path = ref_key.partition(" ")
    path = _fill(path)
    assert _send(anon, method, path).status_code == 401
    nobody = login("nobody1", [])
    assert _send(nobody, method, path).status_code not in (401, 403)


@pytest.mark.parametrize("ref_key", sorted(k for k, v in perms.ENDPOINT_PERMS.items() if v[0] != "public"))
def test_anonymous_gets_401_everywhere(ref_key, anon):
    method, _, path = ref_key.partition(" ")
    assert _send(anon, method, _fill(path)).status_code == 401

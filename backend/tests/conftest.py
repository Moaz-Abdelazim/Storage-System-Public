"""
بنية الاختبارات.

- كل اختبار بياخد DB جديدة: ملف SQLite في tmp (مش :memory: عشان اختبارات الـ threads)،
  أو MySQL 8 لو TEST_DATABASE_URL متحدد (مثال: mysql+pymysql://tester:testpass@127.0.0.1:3307/makhzani_test).
  ⚠ على MySQL الجداول بتتعمل drop_all/create_all مرة في بداية التشغيل وبعدها TRUNCATE لكل اختبار - استخدم DB اختبار فاضية دايمًا.
- الـ env لازم يتظبط قبل أي import لـ app.* (auth.py بيقرا الإعدادات وقت الـ import).
"""
import os

os.environ["APP_ENV"] = "development"
os.environ["ALLOWED_HOSTS"] = "testserver"
os.environ["COOKIE_SECURE"] = "0"
os.environ["DB_ENGINE"] = "sqlite"  # الـ engine بتاع التطبيق نفسه مش بيتستخدم - بنعمل override لـ get_db
os.environ.pop("PUBLIC_BASE_URL", None)

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

from app import auth, models  # noqa: E402
from app.database import Base, get_db  # noqa: E402
from app.main import app  # noqa: E402

# bcrypt بـ 4 rounds في الاختبارات بس (الإنتاج بيستخدم الافتراضي) - بيخلي مئات تسجيلات الدخول في اختبار المصفوفة سريعة
auth.pwd_context.update(bcrypt__rounds=4)

CSRF = {"X-CSRF-Protection": "1"}
DEFAULT_PASSWORD = "Passw0rd!x"
TEST_DATABASE_URL = os.getenv("TEST_DATABASE_URL", "").strip()


def pytest_collection_modifyitems(config, items):
    if TEST_DATABASE_URL.startswith("mysql"):
        return
    skip = pytest.mark.skip(reason="محتاج TEST_DATABASE_URL لـ MySQL 8")
    for item in items:
        if "mysql" in item.keywords:
            item.add_marker(skip)


_MYSQL_ENGINE = {}


def _mysql_engine():
    """MySQL: الجداول بتتعمل مرة واحدة في الـ session، وكل اختبار بيعمل TRUNCATE (أسرع بكتير من drop/create)."""
    from sqlalchemy import text

    eng = _MYSQL_ENGINE.get("e")
    if eng is None:
        eng = _MYSQL_ENGINE["e"] = create_engine(TEST_DATABASE_URL, pool_pre_ping=True, pool_size=20, max_overflow=20)
        Base.metadata.drop_all(eng)
        Base.metadata.create_all(eng)
    with eng.begin() as conn:
        conn.execute(text("SET FOREIGN_KEY_CHECKS=0"))
        for table in reversed(Base.metadata.sorted_tables):
            conn.execute(text(f"TRUNCATE TABLE `{table.name}`"))
        conn.execute(text("SET FOREIGN_KEY_CHECKS=1"))
    return eng


@pytest.fixture()
def engine(tmp_path):
    if TEST_DATABASE_URL:
        yield _mysql_engine()
        return
    eng = create_engine(f"sqlite:///{tmp_path / 'test.db'}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(eng)
    yield eng
    eng.dispose()


@pytest.fixture()
def session_factory(engine):
    return sessionmaker(autocommit=False, autoflush=False, bind=engine)


@pytest.fixture()
def db(session_factory):
    session = session_factory()
    yield session
    session.close()


@pytest.fixture(autouse=True)
def _reset_rate_limits():
    from app import rate_limit

    rate_limit.reset_all()
    yield


@pytest.fixture(autouse=True)
def _override_db(session_factory):
    def _get_db():
        session = session_factory()
        try:
            yield session
        finally:
            session.close()

    app.dependency_overrides[get_db] = _get_db
    yield
    app.dependency_overrides.clear()


STAFF_PERMS = (
    "products.view", "stock_in.create", "stock_out.create", "invoices.view", "invoices.print", "transfers.create",
    "transfers.view", "locations.view", "suppliers.view", "recipients.view",
)


@pytest.fixture()
def make_role(session_factory):
    """make_role(name, perms, is_active=True) -> role_id. الصلاحيات بتتقفل تلقائيًا على requires."""
    from app import permissions

    def _make(name, perms=(), is_active=True):
        session = session_factory()
        try:
            role = models.Role(name=name, is_active=is_active, is_system=False)
            role.permissions = [models.RolePermission(permission=k) for k in sorted(permissions.closure(perms))]
            session.add(role)
            session.commit()
            return role.id
        finally:
            session.close()

    return _make


@pytest.fixture()
def make_user(session_factory, make_role):
    """
    make_user(username, role="admin" | "staff" | [permissions...] | role_id:int, ...) -> user_id
    admin = دور مدير النظام، staff = دور بصلاحيات تشغيل عادية، قايمة = دور جديد بالصلاحيات دي (+ requires).
    """
    from app.services.roles import ensure_system_role

    counter = {"n": 0}

    def _make(username="admin1", role="admin", password=DEFAULT_PASSWORD, is_active=True, full_name=None):
        session = session_factory()
        try:
            if role == "admin":
                role_id = ensure_system_role(session).id
            elif isinstance(role, int):
                role_id = role
            else:
                perms = STAFF_PERMS if role == "staff" else tuple(role)
                name = "موظف" if role == "staff" else f"role-{username}-{counter['n']}"
                counter["n"] += 1
                existing = session.query(models.Role).filter_by(name=name).first()
                role_id = existing.id if existing else make_role(name, perms)
            user = models.User(
                username=username,
                full_name=full_name or username,
                password_hash=auth.get_password_hash(password),
                role_id=role_id,
                is_active=is_active,
            )
            session.add(user)
            session.commit()
            return user.id
        finally:
            session.close()

    return _make


@pytest.fixture()
def anon():
    """عميل من غير تسجيل دخول (بيبعت هيدر CSRF زي الواجهة)."""
    with TestClient(app, base_url="http://testserver", headers=CSRF) as c:
        yield c


@pytest.fixture()
def login(make_user):
    """login(username, role) -> عميل مسجّل دخول (بيعمل المستخدم لو مش موجود)."""
    clients = []

    def _login(username="admin1", role="admin", password=DEFAULT_PASSWORD, create=True):
        if create:
            make_user(username, role, password)
        c = TestClient(app, base_url="http://testserver", headers=CSRF)
        clients.append(c)
        r = c.post("/api/auth/login", json={"username": username, "password": password})
        assert r.status_code == 200, r.text
        return c

    yield _login
    for c in clients:
        c.close()


@pytest.fixture()
def admin(login):
    return login("admin1", "admin")


@pytest.fixture()
def staff(login):
    return login("staff1", "staff")

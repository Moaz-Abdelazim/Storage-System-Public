"""
الـ Alembic baseline: DB فاضية + upgrade head = الـ models بالظبط، تشغيله مرتين آمن، downgrade base شغال.
بيشتغل على SQLite دايمًا، وعلى MySQL 8 لو TEST_MIGRATION_DATABASE_URL متحدد (DB فاضية مخصصة للاختبار ده).
"""
import os

import pytest
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import create_engine, inspect, text

from app import cli, models  # noqa: F401
from app.database import Base

MYSQL_URL = os.getenv("TEST_MIGRATION_DATABASE_URL", "").strip()


def _engines():
    yield pytest.param("sqlite", id="sqlite")
    yield pytest.param("mysql", id="mysql8", marks=pytest.mark.skipif(not MYSQL_URL, reason="محتاج TEST_MIGRATION_DATABASE_URL لـ MySQL 8"))


def _make_engine(kind, tmp_path):
    if kind == "sqlite":
        return create_engine(f"sqlite:///{tmp_path / 'mig.db'}")
    eng = create_engine(MYSQL_URL)
    with eng.begin() as c:
        c.execute(text("SET FOREIGN_KEY_CHECKS=0"))
        for t in inspect(eng).get_table_names():
            c.execute(text(f"DROP TABLE IF EXISTS `{t}`"))
        c.execute(text("SET FOREIGN_KEY_CHECKS=1"))
    return eng


def _diff(conn):
    ctx = MigrationContext.configure(conn, opts={"compare_type": True})
    return compare_metadata(ctx, Base.metadata)


def _tables(eng):
    return set(inspect(eng).get_table_names())


@pytest.fixture(params=list(_engines()))
def eng(request, tmp_path):
    e = _make_engine(request.param, tmp_path)
    yield e
    if request.param == "mysql":
        _make_engine("mysql", tmp_path)  # تنضيف
    e.dispose()


def test_upgrade_head_matches_models(eng):
    with eng.begin() as conn:
        cli.migrate(conn)
    assert _tables(eng) >= set(Base.metadata.tables) | {"alembic_version"}
    with eng.connect() as conn:
        assert _diff(conn) == [], _diff(conn)


def test_upgrade_twice_is_noop(eng):
    with eng.begin() as conn:
        cli.migrate(conn)
    with eng.begin() as conn:
        cli.migrate(conn)
    with eng.connect() as conn:
        assert conn.execute(text("select version_num from alembic_version")).scalars().all() == ["0001"]
        assert _diff(conn) == []


def test_downgrade_base_and_back(eng):
    from alembic import command

    with eng.begin() as conn:
        cli.migrate(conn)
    with eng.begin() as conn:
        command.downgrade(cli._alembic_config(conn), "base")
    assert _tables(eng) <= {"alembic_version"}
    with eng.begin() as conn:
        cli.migrate(conn)
    with eng.connect() as conn:
        assert _diff(conn) == []


def test_migrated_schema_is_usable_and_constraints_hold(eng):
    from sqlalchemy.exc import DBAPIError
    from sqlalchemy.orm import sessionmaker

    with eng.begin() as conn:
        cli.migrate(conn)
    db = sessionmaker(bind=eng)()
    try:
        from app.services.roles import ensure_system_role

        role = ensure_system_role(db)
        db.add(models.User(username="u1", password_hash="x", role_id=role.id))
        db.add(models.Product(name="p", price=1, quantity=0, min_quantity=0, barcode="B1"))
        db.commit()
        db.add(models.Product(name="q", price=1, quantity=0, min_quantity=0, barcode="B1"))
        with pytest.raises(DBAPIError):  # unique(barcode)
            db.commit()
        db.rollback()
        db.add(models.Product(name="neg", price=1, quantity=-1, min_quantity=0))
        with pytest.raises(DBAPIError):  # CHECK quantity >= 0
            db.commit()
        db.rollback()
        # unique على أكواد الإخوة حتى لما parent_id = NULL
        wh = models.Warehouse(code="W", name="W")
        db.add(wh)
        db.flush()
        db.add(models.Location(warehouse_id=wh.id, code="A", name="a"))
        db.commit()
        db.add(models.Location(warehouse_id=wh.id, code="A", name="dup"))
        with pytest.raises(DBAPIError):
            db.commit()
        db.rollback()
    finally:
        db.close()


def test_init_db_stamps_head_so_migrate_is_noop(tmp_path):
    eng = create_engine(f"sqlite:///{tmp_path / 'dev.db'}")
    cli.init_db(eng)
    with eng.connect() as conn:
        assert conn.execute(text("select version_num from alembic_version")).scalars().all() == ["0001"]
    with eng.begin() as conn:
        cli.migrate(conn)  # من غير "table already exists"
    assert _tables(eng) >= set(Base.metadata.tables)


def test_models_and_create_all_have_same_schema_as_migration(tmp_path):
    """create_all (تطوير) و migrations لازم يطلعوا نفس الـ schema."""
    a = create_engine(f"sqlite:///{tmp_path / 'a.db'}")
    Base.metadata.create_all(a)
    b = create_engine(f"sqlite:///{tmp_path / 'b.db'}")
    with b.begin() as conn:
        cli.migrate(conn)
    for t in Base.metadata.tables:
        assert {c["name"] for c in inspect(a).get_columns(t)} == {c["name"] for c in inspect(b).get_columns(t)}, t
        assert {i["name"] for i in inspect(a).get_indexes(t)} == {i["name"] for i in inspect(b).get_indexes(t)}, t

import pytest

from app import cli, models


def test_init_db_creates_tables(tmp_path):
    from sqlalchemy import create_engine, inspect

    eng = create_engine(f"sqlite:///{tmp_path / 'fresh.db'}")
    cli.init_db(eng)
    tables = set(inspect(eng).get_table_names())
    assert {"users", "products", "product_locations", "login_logs", "stock_out_invoices"} <= tables


def test_init_db_refused_in_production(monkeypatch):
    monkeypatch.setenv("APP_ENV", "production")
    with pytest.raises(RuntimeError, match="production"):
        cli.init_db()


def test_create_admin_ok_and_can_login(db, anon):
    user = cli.create_admin(db, "owner", "Str0ngPass!", "صاحب النظام")
    assert user.is_admin and user.password_hash != "Str0ngPass!"
    assert anon.post("/api/auth/login", json={"username": "owner", "password": "Str0ngPass!"}).status_code == 200


@pytest.mark.parametrize("pw", ["short", "12345678", "        "])
def test_create_admin_rejects_weak_password(db, pw):
    with pytest.raises(ValueError):
        cli.create_admin(db, "owner", pw)
    assert db.query(models.User).count() == 0


def test_create_admin_rejects_bad_username_and_duplicates(db):
    with pytest.raises(ValueError):
        cli.create_admin(db, "a b", "Str0ngPass!")
    cli.create_admin(db, "owner", "Str0ngPass!")
    with pytest.raises(ValueError, match="موجود"):
        cli.create_admin(db, "owner", "Str0ngPass!")


def test_main_reports_errors_with_exit_code(monkeypatch, capsys):
    monkeypatch.setenv("APP_ENV", "production")
    assert cli.main(["init-db"]) == 1
    assert "production" in capsys.readouterr().err

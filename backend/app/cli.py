"""
أوامر الإدارة (بتتشغل من فولدر backend):

    python -m app.cli migrate         # alembic upgrade head - الطريقة الوحيدة لإنشاء/تحديث الجداول في production
    python -m app.cli init-db         # ينشئ الجداول - للتطوير والاختبار فقط (مرفوض في production)
    python -m app.cli cleanup         # يمسح سجلات الدخول ومحاولات الدخول القديمة (retention) - جرّب --dry-run الأول
    python -m app.cli check-stock     # يفحص تطابق إجمالي كل منتج مع مجموع أماكنه (exit 1 لو فيه فرق)
    python -m app.cli create-admin    # ينشئ حساب أدمن (بيسأل عن الباسورد، مفيش باسورد في env ولا في الأوامر)

أمثلة:
    python -m app.cli create-admin --username owner --full-name "صاحب النظام"
    printf '%s' "$PW" | python -m app.cli create-admin --username owner --password-stdin   # للسكربتات

ملاحظة: في production الجداول بتتعمل بـ migrations فقط (python -m app.cli migrate) - ممنوع create_all عند الإقلاع.
"""
import argparse
import getpass
import os
import sys
from typing import Optional

from dotenv import load_dotenv
from sqlalchemy.exc import SQLAlchemyError


def _is_production() -> bool:
    # نفس منطق auth.py (fail-closed). بنقراه من env مباشرة عشان الرفض يشتغل حتى لو SECRET_KEY مش متحدد
    from .env import is_production

    return is_production()


def _alembic_config(connection=None):
    from pathlib import Path

    from alembic.config import Config

    cfg = Config(str(Path(__file__).resolve().parent.parent / "alembic.ini"))
    if connection is not None:
        cfg.attributes["connection"] = connection  # للاختبارات: نفس الـ connection بدل engine التطبيق
    return cfg


def migrate(connection=None, revision: str = "head") -> None:
    """alembic upgrade (مأمون يتشغّل أكتر من مرة)."""
    from alembic import command

    command.upgrade(_alembic_config(connection), revision)


def init_db(engine=None) -> None:
    """ينشئ الجداول الناقصة (create_all). للتطوير والاختبارات فقط."""
    if _is_production():
        raise RuntimeError("init-db مرفوض في production. استخدم الـ migrations بدلًا منه.")
    from . import models  # noqa: F401  (لازم يتعمل import عشان الجداول تتسجل في Base.metadata)
    from .database import Base, engine as default_engine

    eng = engine or default_engine
    Base.metadata.create_all(bind=eng)
    from sqlalchemy.orm import sessionmaker

    from .services.roles import ensure_system_role

    with sessionmaker(bind=eng)() as db:
        ensure_system_role(db)  # دور "مدير النظام" الأساسي
    # بنعلّم الـ DB إنها على آخر revision عشان `migrate` بعد كده ما يحاولش ينشئ الجداول تاني
    from alembic import command

    with eng.begin() as conn:
        command.stamp(_alembic_config(conn), "head")


def create_admin(db, username: str, password: str, full_name: Optional[str] = None):
    """
    ينشئ مستخدم أدمن. بيرمي ValueError (برسالة عربي) لو الاسم أو الباسورد مش صالحين أو الاسم موجود.
    بيستخدم نفس قواعد التحقق بتاعة الـ API (schemas) فمفيش طريقة تعمل أدمن بباسورد ضعيف.
    """
    from . import auth, models, schemas
    from .services.roles import ensure_system_role

    username = schemas._clean_username(username)
    password = schemas._check_password_strength(password)
    if db.query(models.User).filter(models.User.username == username).first():
        raise ValueError("اسم المستخدم ده موجود بالفعل")
    user = models.User(
        username=username,
        full_name=(full_name or "").strip() or "مدير النظام",
        password_hash=auth.get_password_hash(password),
        role_id=ensure_system_role(db).id,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def cleanup(db, login_logs_days: int = 90, attempts_days: int = 7, dry_run: bool = False) -> dict:
    """
    retention: سجل الدخول المقفول أقدم من login_logs_days، ومحاولات الدخول (rate limit) الأقدم من attempts_days.
    الجلسات المفتوحة ما بتتمسحش أبدًا. dry_run = عدّ بس.
    """
    from datetime import timedelta

    from . import models
    from .timezone_utils import now_cairo

    now = now_cairo()
    logs_q = db.query(models.LoginLog).filter(
        models.LoginLog.logout_time.isnot(None), models.LoginLog.login_time < now - timedelta(days=login_logs_days)
    )
    att_q = db.query(models.LoginAttempt).filter(models.LoginAttempt.attempted_at < now - timedelta(days=attempts_days))
    result = {"login_logs": logs_q.count(), "login_attempts": att_q.count(), "dry_run": dry_run}
    if not dry_run:
        logs_q.delete(synchronize_session=False)
        att_q.delete(synchronize_session=False)
        db.commit()
    return result


def check_stock(db) -> list:
    """بيرجّع قايمة المنتجات اللي products.quantity فيها مش = مجموع product_locations (أو فيها رصيد سالب)."""
    from sqlalchemy import func

    from . import models

    P, PL = models.Product, models.ProductLocation
    sums = dict(db.query(PL.product_id, func.coalesce(func.sum(PL.quantity), 0)).group_by(PL.product_id).all())
    problems = []
    for pid, name, qty in db.query(P.id, P.name, P.quantity).all():
        total = round(float(sums.get(pid, 0)), 3)
        if round(float(qty or 0), 3) != total or total < 0:
            problems.append({"product_id": pid, "name": name, "product_quantity": float(qty or 0), "locations_sum": total})
    negatives = db.query(PL.product_id, PL.location_id, PL.quantity).filter(PL.quantity < 0).all()
    for pid, lid, qty in negatives:
        problems.append({"product_id": pid, "name": f"رصيد سالب في الموقع #{lid}", "product_quantity": None, "locations_sum": float(qty)})
    return problems


def _read_password(from_stdin: bool) -> str:
    if from_stdin:
        return sys.stdin.readline().rstrip("\r\n")
    first = getpass.getpass("كلمة المرور: ")
    if first != getpass.getpass("أعد كتابة كلمة المرور: "):
        raise ValueError("كلمتين المرور مش متطابقتين")
    return first


def main(argv: Optional[list] = None) -> int:
    load_dotenv()
    parser = argparse.ArgumentParser(prog="python -m app.cli", description="أوامر إدارة مخزني")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("migrate", help="تطبيق الـ migrations (alembic upgrade head)")
    sub.add_parser("init-db", help="إنشاء الجداول (تطوير/اختبار فقط)")
    p_clean = sub.add_parser("cleanup", help="مسح سجلات الدخول ومحاولات الدخول القديمة")
    p_clean.add_argument("--login-logs-days", type=int, default=90)
    p_clean.add_argument("--attempts-days", type=int, default=7)
    p_clean.add_argument("--dry-run", action="store_true")
    sub.add_parser("check-stock", help="فحص تطابق أرصدة المنتجات مع أماكنها (exit 1 لو فيه فرق)")
    p_admin = sub.add_parser("create-admin", help="إنشاء حساب أدمن")
    p_admin.add_argument("--username", required=True)
    p_admin.add_argument("--full-name", default=None)
    p_admin.add_argument("--password-stdin", action="store_true", help="قراءة الباسورد من stdin بدل السؤال التفاعلي")
    args = parser.parse_args(argv)

    try:
        if args.command == "migrate":
            migrate()
            print("✔ الـ schema على آخر revision")
            return 0
        if args.command == "init-db":
            init_db()
            print("✔ تم إنشاء الجداول")
            return 0
        if args.command == "cleanup":
            from .database import SessionLocal

            db = SessionLocal()
            try:
                res = cleanup(db, args.login_logs_days, args.attempts_days, args.dry_run)
            finally:
                db.close()
            verb = "هيتمسح" if res["dry_run"] else "اتمسح"
            print(f"✔ {verb}: {res['login_logs']} سجل دخول، {res['login_attempts']} محاولة دخول")
            return 0
        if args.command == "check-stock":
            from .database import SessionLocal

            db = SessionLocal()
            try:
                problems = check_stock(db)
            finally:
                db.close()
            if not problems:
                print("✔ كل الأرصدة متطابقة")
                return 0
            for p in problems:
                print(f"✘ منتج #{p['product_id']} ({p['name']}): الإجمالي={p['product_quantity']} ومجموع الأماكن={p['locations_sum']}")
            return 1
        if args.command == "create-admin":
            password = _read_password(args.password_stdin)
            from .database import SessionLocal

            db = SessionLocal()
            try:
                user = create_admin(db, args.username, password, args.full_name)
            finally:
                db.close()
            print(f"✔ تم إنشاء الأدمن: {user.username}")
            return 0
    except (ValueError, RuntimeError) as exc:
        print(f"✘ {exc}", file=sys.stderr)
        return 1
    except SQLAlchemyError:
        # مفيش تفاصيل الاستثناء هنا (ممكن تحتوي على بيانات اتصال)
        print("✘ تعذّر الوصول لقاعدة البيانات أو الجداول مش موجودة (في التطوير شغّل: python -m app.cli init-db)", file=sys.stderr)
        return 1
    return 2


if __name__ == "__main__":
    sys.exit(main())

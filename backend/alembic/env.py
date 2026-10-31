"""بيئة Alembic: نفس إعدادات الاتصال بتاعة التطبيق (app/database.py) + الـ models كمصدر للـ metadata."""
from logging.config import fileConfig

from alembic import context

from app import models  # noqa: F401  (لازم يتعمل import عشان كل الجداول تتسجل في Base.metadata)
from app.database import Base, SQLALCHEMY_DATABASE_URL, engine

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name, disable_existing_loggers=False)

target_metadata = Base.metadata


def _is_sqlite(url) -> bool:
    return str(url).startswith("sqlite")


def run_migrations_offline() -> None:
    context.configure(
        url=str(SQLALCHEMY_DATABASE_URL.render_as_string(hide_password=False) if hasattr(SQLALCHEMY_DATABASE_URL, "render_as_string") else SQLALCHEMY_DATABASE_URL),
        target_metadata=target_metadata,
        literal_binds=True,
        compare_type=True,
        render_as_batch=_is_sqlite(SQLALCHEMY_DATABASE_URL),
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    # الاتصال ممكن يتمرّر من برا (الاختبارات والـ CLI) عن طريق config.attributes["connection"]
    connection = config.attributes.get("connection")
    if connection is not None:
        _run(connection)
        return
    with engine.connect() as connection:
        _run(connection)


def _run(connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        compare_type=True,
        render_as_batch=connection.dialect.name == "sqlite",  # SQLite محتاجة batch لتعديل الجداول
    )
    with context.begin_transaction():
        context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()

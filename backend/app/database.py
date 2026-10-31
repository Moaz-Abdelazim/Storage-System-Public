import os
from pathlib import Path

from dotenv import load_dotenv
from sqlalchemy import MetaData, create_engine
from sqlalchemy.engine import URL
from sqlalchemy.orm import declarative_base, sessionmaker

load_dotenv()

DB_ENGINE = os.getenv("DB_ENGINE", "sqlite").lower()


def _int_env(name: str, default: int) -> int:
    try:
        return max(0, int(os.getenv(name, default)))
    except ValueError:
        return default


if DB_ENGINE == "mysql":
    # URL.create بيعمل escape للباسورد (رموز زي @ : / # كانت بتكسر الـ URL لو اتحطت في f-string)
    SQLALCHEMY_DATABASE_URL = URL.create(
        "mysql+pymysql",
        username=os.getenv("MYSQL_USER", "root"),
        password=os.getenv("MYSQL_PASSWORD", ""),
        host=os.getenv("MYSQL_HOST", "localhost"),
        port=int(os.getenv("MYSQL_PORT", "3306")),
        database=os.getenv("MYSQL_DB", "inventory_db"),
        query={"charset": "utf8mb4"},
    )
    engine = create_engine(
        SQLALCHEMY_DATABASE_URL,
        pool_pre_ping=True,
        pool_size=_int_env("DB_POOL_SIZE", 10),
        max_overflow=_int_env("DB_MAX_OVERFLOW", 20),
        pool_recycle=_int_env("DB_POOL_RECYCLE", 1800),  # أقل من wait_timeout بتاع MySQL/Aiven
        pool_timeout=_int_env("DB_POOL_TIMEOUT", 30),
    )
else:
    # SQLite - للتطوير/التجربة السريعة. المسار ثابت جنب فولدر backend (مش بيعتمد على مكان تشغيل الأمر)
    SQLALCHEMY_DATABASE_URL = f"sqlite:///{Path(__file__).resolve().parent.parent / 'inventory.db'}"
    engine = create_engine(SQLALCHEMY_DATABASE_URL, connect_args={"check_same_thread": False})

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

# أسماء ثابتة للـ constraints/indexes: بتخلّي الـ migrations قابلة للتراجع على MySQL وعلى SQLite بنفس الأسماء
NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}
Base = declarative_base(metadata=MetaData(naming_convention=NAMING_CONVENTION))


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

"""بيئة التشغيل. fail-closed: لو APP_ENV مش متحدد أو قيمته مش معروفة = production (أشد إعدادات الأمان)."""
import os

DEV_VALUES = {"development", "dev", "test", "testing"}


def app_env() -> str:
    return os.getenv("APP_ENV", "").strip().lower() or "production"


def is_production() -> bool:
    return app_env() not in DEV_VALUES

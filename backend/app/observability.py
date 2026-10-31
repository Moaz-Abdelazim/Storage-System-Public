"""لوجينج + request id + readiness (التشغيل والمراقبة)."""
import logging
import os
import re
import time
import uuid

_REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9\-_.]{8,64}$")


def setup_logging() -> None:
    """اللوجز على stdout (الـ platform/systemd بيجمعها). LOG_LEVEL من الـ env. مبنلمسش إعداد pytest لو موجود."""
    root = logging.getLogger()
    if root.handlers:
        return
    level = getattr(logging, os.getenv("LOG_LEVEL", "INFO").upper(), logging.INFO)
    logging.basicConfig(level=level, format="%(asctime)s %(levelname)s %(name)s %(message)s")


def new_request_id(incoming: str = None) -> str:
    """بنقبل X-Request-ID من الـ proxy لو شكله سليم (تتبع بين الخدمات)، وإلا بنولّد واحد."""
    if incoming and _REQUEST_ID_RE.match(incoming):
        return incoming
    return uuid.uuid4().hex


access_log = logging.getLogger("app.access")


def log_request(request_id: str, method: str, path: str, status: int, started: float) -> None:
    # المسار بس من غير query string (ممكن يحتوي بحث أو بيانات حساسة)
    ms = int((time.perf_counter() - started) * 1000)
    access_log.info("%s %s -> %s (%dms) rid=%s", method, path, status, ms, request_id)


def readiness(engine) -> dict:
    """جاهز = الـ DB بترد والـ schema على آخر revision. بنرجّع سبب مختصر (من غير تفاصيل داخلية)."""
    from pathlib import Path

    from alembic.config import Config
    from alembic.script import ScriptDirectory
    from sqlalchemy import text

    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
            try:
                current = conn.execute(text("SELECT version_num FROM alembic_version")).scalar()
            except Exception:
                return {"ready": False, "reason": "schema_not_migrated"}
    except Exception:
        return {"ready": False, "reason": "db_unreachable"}
    cfg = Config(str(Path(__file__).resolve().parent.parent / "alembic.ini"))
    head = ScriptDirectory.from_config(cfg).get_current_head()
    if current != head:
        return {"ready": False, "reason": "schema_outdated"}
    return {"ready": True}

"""التطبيق مينفعش يعمل side effects وقت الـ import (print / فتح socket) - خصوصًا في production."""
import os
import subprocess
import sys
import textwrap
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent


def test_production_import_has_no_output_and_opens_no_socket():
    code = textwrap.dedent(
        """
        import socket
        def boom(*a, **k): raise AssertionError("socket connect at import")
        socket.socket.connect = boom
        import app.main
        """
    )
    env = {**os.environ, "APP_ENV": "production", "SECRET_KEY": "x" * 48, "ALLOWED_HOSTS": "example.com",
           "ALLOWED_ORIGINS": "https://example.com", "DB_ENGINE": "sqlite"}
    res = subprocess.run([sys.executable, "-c", code], cwd=BACKEND, env=env, capture_output=True, text=True, timeout=60)
    assert res.returncode == 0, res.stderr
    assert res.stdout.strip() == ""


def test_production_requires_secret_key():
    env = {k: v for k, v in os.environ.items() if k != "SECRET_KEY"}
    env.update(APP_ENV="production", DB_ENGINE="sqlite")
    res = subprocess.run([sys.executable, "-c", "import app.main"], cwd=BACKEND, env=env, capture_output=True, text=True, timeout=60)
    assert res.returncode != 0 and "SECRET_KEY" in res.stderr

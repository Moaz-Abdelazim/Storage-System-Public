"""
إعدادات المشروع المركزية (اسم المنتج): مصدر واحد هو `backend/project.config.json`.

- الـ Backend بيقراه من هنا (عنوان الـ API، أوامر الـ CLI، ...).
- الواجهة بتجيبه من `GET /api/app-info` (عام، من غير تسجيل دخول) وتعرضه في العناوين وتسجيل الدخول والقائمة والتقارير،
  فتغيير الاسم = تعديل `name` في الملف ده + إعادة تشغيل/نشر الـ Backend، ومفيش أي حاجة تانية تتعدّل.
- (الواجهة والـ Backend على Vercel projects منفصلة، فملف في جذر الريبو مكانش هيوصل لأي منهم.)
"""
import json
import os
from functools import lru_cache
from pathlib import Path

_DEFAULT_PATH = Path(__file__).resolve().parent.parent / "project.config.json"
MAX_NAME_LEN = 60


class ProjectConfigError(RuntimeError):
    pass


@lru_cache(maxsize=1)
def get_project_config() -> dict:
    path = Path(os.getenv("PROJECT_CONFIG_PATH", "").strip() or _DEFAULT_PATH)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ProjectConfigError(f"ملف إعدادات المشروع مش موجود: {path}") from exc
    except (OSError, ValueError) as exc:
        raise ProjectConfigError(f"ملف إعدادات المشروع تالف أو مش JSON صحيح: {path} ({exc})") from exc
    if not isinstance(data, dict):
        raise ProjectConfigError(f"ملف إعدادات المشروع لازم يبقى JSON object: {path}")
    name = data.get("name")
    if not isinstance(name, str) or not name.strip() or len(name.strip()) > MAX_NAME_LEN:
        raise ProjectConfigError(f"`name` في {path} لازم يبقى نص غير فاضي (حد أقصى {MAX_NAME_LEN} حرف)")
    description = data.get("description", "")
    if not isinstance(description, str):
        raise ProjectConfigError(f"`description` في {path} لازم يبقى نص")
    return {"name": name.strip(), "description": description.strip()}


def app_name() -> str:
    return get_project_config()["name"]


def app_description() -> str:
    return get_project_config()["description"]

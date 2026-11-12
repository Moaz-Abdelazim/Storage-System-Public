"""اسم المنتج من مصدر واحد (backend/project.config.json) + حارس إن الاسم القديم مش ظاهر في الواجهة."""
import json
import re
from pathlib import Path

import pytest

from app import project_config

ROOT = Path(__file__).resolve().parents[2]
FRONTEND = ROOT / "frontend"
# "مستند مخزني" = مستند خاص بالمخزن (وصف عربي عادي مش اسم المنتج)
GENERIC_OK = ("مستند مخزني", "مستندات مخزنية", "مخزنية")


@pytest.fixture(autouse=True)
def _fresh_config(monkeypatch):
    project_config.get_project_config.cache_clear()
    yield
    monkeypatch.undo()
    project_config.get_project_config.cache_clear()


def test_app_info_is_public_and_matches_config_file(anon):
    r = anon.get("/api/app-info")
    assert r.status_code == 200
    expected = json.loads((ROOT / "backend" / "project.config.json").read_text(encoding="utf-8"))
    assert r.json()["name"] == expected["name"] == "Jardy"
    assert "max-age" in r.headers["cache-control"]


def test_api_title_and_cli_use_the_configured_name():
    from app.main import app
    assert app.title.startswith(project_config.app_name())


def test_changing_the_config_changes_the_name_everywhere(tmp_path, monkeypatch, anon):
    cfg = tmp_path / "project.config.json"
    cfg.write_text(json.dumps({"name": "اسم جديد", "description": "وصف"}, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setenv("PROJECT_CONFIG_PATH", str(cfg))
    project_config.get_project_config.cache_clear()
    assert anon.get("/api/app-info").json() == {"name": "اسم جديد", "description": "وصف"}


@pytest.mark.parametrize("content", ['{"name": ""}', '{"name": 5}', "{}", "[]", "not json", '{"name": "' + "س" * 61 + '"}'])
def test_invalid_config_fails_loudly(tmp_path, monkeypatch, content):
    cfg = tmp_path / "p.json"
    cfg.write_text(content, encoding="utf-8")
    monkeypatch.setenv("PROJECT_CONFIG_PATH", str(cfg))
    project_config.get_project_config.cache_clear()
    with pytest.raises(project_config.ProjectConfigError):
        project_config.get_project_config()


def test_missing_config_file_fails_loudly(tmp_path, monkeypatch):
    monkeypatch.setenv("PROJECT_CONFIG_PATH", str(tmp_path / "nope.json"))
    project_config.get_project_config.cache_clear()
    with pytest.raises(project_config.ProjectConfigError):
        project_config.get_project_config()


def test_old_brand_name_does_not_appear_in_the_frontend():
    if not (FRONTEND / "public").is_dir():
        pytest.skip("مجلد الواجهة مش موجود جنب الـ backend")
    pat = re.compile(r"مخزني|makhzani|makhzany", re.I)
    hits = []
    for path in list((FRONTEND / "public").glob("*.html")) + list((FRONTEND / "public" / "js").glob("*.js")):
        for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            cleaned = line
            for ok in GENERIC_OK:
                cleaned = cleaned.replace(ok, "")
            if pat.search(cleaned):
                hits.append(f"{path.name}:{n}")
    assert not hits, hits


def test_brand_elements_are_filled_from_the_config_not_hardcoded():
    if not (FRONTEND / "public").is_dir():
        pytest.skip("مجلد الواجهة مش موجود جنب الـ backend")
    for page in ("login.html", "setup.html"):
        assert "data-app-name" in (FRONTEND / "public" / page).read_text(encoding="utf-8")
    assert "data-app-name" in (FRONTEND / "public" / "js" / "layout.js").read_text(encoding="utf-8")

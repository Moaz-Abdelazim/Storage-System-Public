"""اتساق الواجهة مع الكتالوج: أي مفتاح صلاحية مكتوب في الـ HTML/JS لازم يكون موجود في app/permissions.py."""
import re
from pathlib import Path

from app import permissions as perms

PUBLIC = Path(__file__).resolve().parents[2] / "frontend" / "public"
KEY = r"[a-z_]+\.[a-z_]+"


def _keys_used():
    used = {}
    for f in list(PUBLIC.glob("*.html")) + list((PUBLIC / "js").glob("*.js")):
        text = f.read_text(encoding="utf-8")
        found = set()
        for m in re.finditer(r"""\b(?:can|canAll)\(([^)]*)\)""", text):
            found |= set(re.findall(rf'"({KEY})"', m.group(1)))
        for m in re.finditer(r'data-perm="([^"]+)"', text):
            found |= {k.strip() for k in m.group(1).split(",") if k.strip()}
        for m in re.finditer(r"""\b(?:perm|managePerm|deletePerm)\s*:\s*(\[[^\]]*\]|"[^"]*")""", text):
            found |= set(re.findall(rf'"({KEY})"', m.group(1)))
        if found:
            used[f.name] = found
    return used


def test_frontend_uses_only_known_permission_keys():
    if not PUBLIC.exists():
        return
    used = _keys_used()
    assert used, "مفيش ولا مفتاح صلاحية في الواجهة؟ الريجيكس محتاج مراجعة"
    unknown = {f: sorted(k - perms.ALL_KEYS) for f, k in used.items() if k - perms.ALL_KEYS}
    assert not unknown, unknown


def test_every_page_with_perm_attribute_maps_to_a_nav_entry_or_is_detail():
    if not PUBLIC.exists():
        return
    for html in PUBLIC.glob("*.html"):
        text = html.read_text(encoding="utf-8")
        if 'id="shell"' in text:
            assert ("data-perm=" in text) or ("data-admin-only" in text) or html.name in {"index.html", "settings.html"}, html.name

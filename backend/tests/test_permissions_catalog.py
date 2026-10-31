import re
from pathlib import Path

import pytest

from app import permissions as perms


def test_catalog_integrity():
    assert len(perms.PERMISSIONS) == len(set(perms.PERMISSIONS))
    for key, d in perms.PERMISSIONS.items():
        assert d.key == key and re.fullmatch(r"[a-z_]+\.[a-z_]+", key)
        assert re.search(r"[\u0600-\u06FF]", d.label), f"label عربي مطلوب: {key}"
        assert re.search(r"[\u0600-\u06FF]", d.group)
        for r in d.requires:
            assert r in perms.PERMISSIONS, f"{key} بيطلب صلاحية غير موجودة {r}"
            assert r != key


def test_requires_has_no_cycles():
    for key in perms.PERMISSIONS:
        seen, stack = set(), list(perms.PERMISSIONS[key].requires)
        while stack:
            k = stack.pop()
            assert k != key, f"دورة في requires عند {key}"
            if k not in seen:
                seen.add(k)
                stack.extend(perms.PERMISSIONS[k].requires)


def test_shared_any_of_groups_use_known_keys():
    for k in perms.PRODUCT_READ + perms.LOCATION_READ:
        assert k in perms.PERMISSIONS


def test_validate_permission_set_rules():
    assert perms.validate_permission_set(["products.view", "products.view"]) == ["products.view"]
    with pytest.raises(ValueError, match="غير معروفة"):
        perms.validate_permission_set(["nope.x"])
    with pytest.raises(ValueError, match="محتاجة"):
        perms.validate_permission_set(["products.price"])
    with pytest.raises(ValueError):
        perms.validate_permission_set(["reports.view"])  # محتاجة finance.view
    assert perms.validate_permission_set(sorted(perms.closure(["products.price"])))


def test_closure_is_transitive():
    assert perms.closure(["products.price"]) == {"products.price", "products.edit", "products.view"}
    assert perms.closure(["sessions.revoke"]) == {"sessions.revoke", "logs.view"}


def test_endpoint_table_uses_known_keys():
    for key, (kind, keys) in perms.ENDPOINT_PERMS.items():
        assert kind in {"public", "session", "lenient", "admin", "all", "any"}, key
        for k in keys:
            assert k in perms.PERMISSIONS, f"{key}: {k}"


def test_no_textual_role_checks_anywhere():
    """الأدمن = role.is_system بس. أي فحص نصي زي user.role == "admin" ممنوع (كان هيبقى False بصمت)."""
    root = Path(__file__).resolve().parent.parent / "app"
    bad = []
    for f in root.rglob("*.py"):
        text = f.read_text(encoding="utf-8")
        if re.search(r"""\.role\s*(==|!=|in)\s*[\(\["']""", text) or re.search(r"""role\s*==\s*["'](admin|staff)""", text):
            bad.append(str(f))
    assert not bad, bad

"""
كتالوج الصلاحيات (مصدر الحقيقة الوحيد) + جدول ENDPOINT_PERMS المرجعي.

- الكتالوج في الكود مش في الـ DB: أي صلاحية جديدة بتتضاف هنا بتبقى متاحة تلقائيًا لدور "مدير النظام" (is_system)
  لأنه عنده كل المفاتيح ضمنيًا (مش مخزّنة في role_permissions).
- كل صلاحية ليها requires: الدور لازم يحتوي عليها كلها (validate_permission_set بترفض غير كده).
- ENDPOINT_PERMS هو المرجع اللي بيستخدمه اختبار المصفوفة واختبار "مفيش route من غير حماية".
  الـ routers بتستخدم نفس القواعد صراحة (auth.require_permission / require_any_permission) وأي اختلاف بيكشفه الاختبار.
"""
from dataclasses import dataclass
from typing import Dict, Iterable, List, Optional, Set, Tuple


@dataclass(frozen=True)
class PermissionDef:
    key: str
    label: str
    group: str
    requires: Tuple[str, ...] = ()


def _p(key: str, label: str, group: str, *requires: str) -> Tuple[str, PermissionDef]:
    return key, PermissionDef(key, label, group, tuple(requires))


PERMISSIONS: Dict[str, PermissionDef] = dict([
    # ---- المنتجات ----
    _p("products.view", "عرض المنتجات", "المنتجات"),
    _p("products.create", "إضافة منتج", "المنتجات", "products.view"),
    _p("products.edit", "تعديل بيانات المنتج", "المنتجات", "products.view"),
    _p("products.price", "تغيير سعر المنتج", "المنتجات", "products.edit"),
    _p("products.toggle", "تفعيل/تعطيل المنتج", "المنتجات", "products.view"),
    _p("products.adjust", "تعديل الكميات وتوزيعها (تصحيح جرد)", "المنتجات", "products.view"),
    _p("products.import", "استيراد Excel", "المنتجات", "products.view"),
    _p("products.export", "تصدير Excel", "المنتجات", "products.view"),
    # ---- التصنيفات والماركات ----
    _p("catalog.manage", "إضافة/تعديل التصنيفات والماركات", "التصنيفات والماركات"),
    _p("catalog.delete", "حذف التصنيفات والماركات", "التصنيفات والماركات", "catalog.manage"),
    # ---- المخزون والفواتير ----
    _p("stock_in.create", "تسجيل وارد (إذن إضافة)", "المخزون والفواتير"),
    _p("stock_out.create", "تسجيل صادر (إذن صرف)", "المخزون والفواتير"),
    _p("stock_out.manage_held", "إتمام/تعديل/مسح الفواتير المعلّقة", "المخزون والفواتير", "stock_out.create", "invoices.view"),
    _p("stock_out.discount", "تطبيق خصم/ضريبة على الصادر", "المخزون والفواتير", "stock_out.create"),
    _p("invoices.view", "عرض الفواتير وسجل الحركات", "المخزون والفواتير"),
    _p("invoices.print", "طباعة المستندات", "المخزون والفواتير", "invoices.view"),
    _p("invoices.void", "إلغاء الفواتير", "المخزون والفواتير", "invoices.view"),
    _p("finance.view", "رؤية القيم المالية (أسعار الشراء، الإجماليات، قيمة المخزون)", "المخزون والفواتير"),
    # ---- المواقع والتحويلات ----
    _p("locations.view", "عرض المخازن والمواقع", "المواقع والتحويلات"),
    _p("locations.manage", "إضافة/تعديل المخازن والمواقع", "المواقع والتحويلات", "locations.view"),
    _p("locations.delete", "حذف المخازن والمواقع", "المواقع والتحويلات", "locations.manage"),
    _p("transfers.create", "تنفيذ نقل بين المواقع", "المواقع والتحويلات"),
    _p("transfers.view", "عرض سجل النقل", "المواقع والتحويلات"),
    # ---- الأطراف ----
    _p("suppliers.view", "عرض الموردين", "الموردين"),
    _p("suppliers.manage", "إضافة/تعديل الموردين", "الموردين", "suppliers.view"),
    _p("suppliers.delete", "حذف الموردين", "الموردين", "suppliers.manage"),
    _p("recipients.view", "عرض جهات الاستلام", "جهات الاستلام"),
    _p("recipients.manage", "إضافة/تعديل جهات الاستلام", "جهات الاستلام", "recipients.view"),
    _p("recipients.delete", "حذف جهات الاستلام", "جهات الاستلام", "recipients.manage"),
    # ---- الجرد ----
    _p("stocktake.manage", "إدارة الجرد", "الجرد"),
    _p("stocktake.approve", "اعتماد الجرد", "الجرد", "stocktake.manage"),
    # ---- التقارير ----
    _p("reports.view", "التقارير والتقرير التنفيذي", "التقارير", "finance.view"),
    # ---- الإدارة ----
    _p("users.manage", "إدارة المستخدمين", "الإدارة"),
    _p("logs.view", "عرض سجل الدخول", "الإدارة"),
    _p("sessions.revoke", "إنهاء جلسات المستخدمين", "الإدارة", "logs.view"),
    _p("settings.company", "إعدادات الشركة", "الإدارة"),
])

ALL_KEYS: Set[str] = set(PERMISSIONS)

# مجموعات any-of المشتركة
PRODUCT_READ = ("products.view", "stock_in.create", "stock_out.create", "transfers.create", "stocktake.manage")
LOCATION_READ = ("locations.view", "stock_in.create", "stock_out.create", "transfers.create", "transfers.view",
                 "products.create", "products.adjust", "stocktake.manage")


def label_of(key: str) -> str:
    d = PERMISSIONS.get(key)
    return d.label if d else key


def closure(keys: Iterable[str]) -> Set[str]:
    """المفاتيح + كل اللي بتتطلبه (تراكميًا)."""
    out, stack = set(), list(keys)
    while stack:
        k = stack.pop()
        if k in out or k not in PERMISSIONS:
            continue
        out.add(k)
        stack.extend(PERMISSIONS[k].requires)
    return out


def validate_permission_set(keys: Iterable[str]) -> List[str]:
    """
    بيتحقق إن المفاتيح معروفة وإن كل `requires` موجود. بيرجّع القايمة مرتبة وبلا تكرار،
    أو بيرمي ValueError برسالة عربي بتذكر المفقود.
    """
    given = list(dict.fromkeys(keys))
    unknown = [k for k in given if k not in PERMISSIONS]
    if unknown:
        raise ValueError(f"صلاحيات غير معروفة: {', '.join(unknown)}")
    have = set(given)
    problems = []
    for k in given:
        missing = [r for r in PERMISSIONS[k].requires if r not in have]
        if missing:
            problems.append(f"({label_of(k)}) محتاجة: {' و'.join(label_of(m) for m in missing)}")
    if problems:
        raise ValueError("الصلاحيات غير مكتملة - " + "؛ ".join(problems))
    return sorted(have)


def catalog_grouped() -> List[dict]:
    groups: Dict[str, List[dict]] = {}
    for d in PERMISSIONS.values():
        groups.setdefault(d.group, []).append({"key": d.key, "label": d.label, "requires": list(d.requires)})
    return [{"group": g, "permissions": items} for g, items in groups.items()]


def user_permissions(user) -> Set[str]:
    """الأدمن (دور is_system) = كل المفاتيح. دور موقوف أو مفيش دور = ∅. غير كده: صلاحيات الدور المخزّنة."""
    role = getattr(user, "role", None)
    if role is None:
        return set()
    if role.is_system:
        return set(ALL_KEYS)
    if not role.is_active:
        return set()
    return {rp.permission for rp in role.permissions if rp.permission in ALL_KEYS}


def has_permission(user, key: str) -> bool:
    return key in user_permissions(user)


# ---------------------------------------------------------------------------
# جدول ENDPOINT_PERMS: "METHOD /path" -> (النوع, المفاتيح)
#   public  : مفتوح            session : أي مستخدم مسجّل (دوره فعّال)   lenient : أي مسجّل حتى لو دوره موقوف
#   admin   : دور is_system     all     : لازم كل المفاتيح            any     : لازم واحد منهم على الأقل
# الصلاحيات الشرطية (سعر، كمية ابتدائية، خصم، تقنيع finance) بتتفحص جوه الـ handlers وليها اختباراتها.
# ---------------------------------------------------------------------------
P, S, L, A = "public", "session", "lenient", "admin"
ALL, ANY = "all", "any"

_ADMIN_PAGES = {
    # ------- auth -------
    "POST /api/auth/login": (P, ()),
    "POST /api/auth/logout": (L, ()),
    "GET /api/auth/me": (L, ()),
    "POST /api/auth/change-password": (S, ()),
    "POST /api/auth/users": (ALL, ("users.manage",)),
    "GET /api/auth/users": (ALL, ("users.manage",)),
    "GET /api/auth/users/stats": (ALL, ("users.manage",)),
    "PUT /api/auth/users/{user_id}": (ALL, ("users.manage",)),
    "DELETE /api/auth/users/{user_id}": (ALL, ("users.manage",)),
    "GET /api/auth/usernames": (ANY, ("invoices.view", "users.manage")),
    "GET /api/auth/roles-lite": (ALL, ("users.manage",)),
    "GET /api/auth/login-logs": (ALL, ("logs.view",)),
    "POST /api/auth/login-logs/{session_id}/revoke": (ALL, ("sessions.revoke",)),
    # ------- roles (للأدمن فقط - مش صلاحية قابلة للتفويض) -------
    "GET /api/roles/permissions": (A, ()),
    "GET /api/roles": (A, ()),
    "POST /api/roles": (A, ()),
    "PUT /api/roles/{role_id}": (A, ()),
    "PATCH /api/roles/{role_id}/active": (A, ()),
    "DELETE /api/roles/{role_id}": (A, ()),
}

ENDPOINT_PERMS: Dict[str, Tuple[str, Tuple[str, ...]]] = dict(_ADMIN_PAGES)


def _add(prefix: str, rows: Dict[str, Tuple[str, Tuple[str, ...]]]) -> None:
    for k, v in rows.items():
        method, _, path = k.partition(" ")
        ENDPOINT_PERMS[f"{method} {prefix}{path}"] = v


for _name in ("categories", "brands"):
    _add(f"/api/{_name}", {
        "GET ": (S, ()),
        "GET /stats": (ANY, ("products.view", "catalog.manage")),
        "POST ": (ALL, ("catalog.manage",)),
        "PUT /{item_id}": (ALL, ("catalog.manage",)),
        "DELETE /{item_id}": (ALL, ("catalog.delete",)),
    })

_add("/api/products", {
    "GET ": (ANY, PRODUCT_READ),
    "GET /lookup": (ANY, PRODUCT_READ),
    "GET /by-barcode/{code}": (ANY, PRODUCT_READ),
    "GET /{product_id}": (ANY, PRODUCT_READ),
    "GET /{product_id}/movements": (ALL, ("invoices.view",)),
    "POST ": (ALL, ("products.create",)),
    "PUT /{product_id}": (ALL, ("products.edit",)),
    "PUT /{product_id}/locations": (ALL, ("products.adjust",)),
    "POST /{product_id}/adjust-quantity": (ALL, ("products.adjust",)),
    "PATCH /{product_id}/active": (ALL, ("products.toggle",)),
})

_add("/api/suppliers", {
    "GET ": (ALL, ("suppliers.view",)),
    "GET /lookup": (ANY, ("suppliers.view", "stock_in.create")),
    "POST ": (ANY, ("suppliers.manage", "stock_in.create")),
    "PUT /{item_id}": (ALL, ("suppliers.manage",)),
    "DELETE /{item_id}": (ALL, ("suppliers.delete",)),
})
_add("/api/recipients", {
    "GET ": (ALL, ("recipients.view",)),
    "GET /lookup": (ANY, ("recipients.view", "stock_out.create")),
    "POST ": (ANY, ("recipients.manage", "stock_out.create")),
    "PUT /{item_id}": (ALL, ("recipients.manage",)),
    "DELETE /{item_id}": (ALL, ("recipients.delete",)),
})

_add("/api/stock", {
    "POST /in": (ALL, ("stock_in.create",)),
    "POST /out": (ALL, ("stock_out.create",)),
    "PUT /out/{invoice_id}": (ALL, ("stock_out.manage_held",)),
    "POST /out/{invoice_id}/complete": (ALL, ("stock_out.manage_held",)),
    "DELETE /out/{invoice_id}": (ALL, ("stock_out.manage_held",)),
    "GET /out/{invoice_id}/edit-data": (ALL, ("stock_out.manage_held",)),
    "GET /in": (ALL, ("invoices.view",)),
    "GET /in/{invoice_id}": (ALL, ("invoices.view",)),
    "GET /out": (ALL, ("invoices.view",)),
    "GET /out/{invoice_id}": (ALL, ("invoices.view",)),
    "GET /log": (ANY, ("invoices.view", "transfers.view")),
    "GET /{kind}/{invoice_id}/print": (ALL, ("invoices.print",)),
})

_add("/api/reports", {
    "GET /summary": (S, ()),
    "GET /out-totals": (ALL, ("finance.view",)),
    "GET /low-stock": (ALL, ("products.view",)),
    "GET /movements": (ALL, ("reports.view",)),
    "GET /top-products": (ALL, ("reports.view",)),
    "GET /by-category": (ALL, ("reports.view",)),
    "GET /by-brand": (ALL, ("reports.view",)),
    "GET /top-suppliers": (ALL, ("reports.view",)),
    "GET /top-recipients": (ALL, ("reports.view",)),
    "GET /product-movements/{product_id}": (ALL, ("invoices.view",)),
    "GET /supplier-products/{supplier_id}": (ALL, ("suppliers.view",)),
    "GET /supplier-product-invoices/{supplier_id}/{product_id}": (ALL, ("suppliers.view",)),
    "GET /recipient-products/{recipient_id}": (ALL, ("recipients.view",)),
    "GET /recipient-product-invoices/{recipient_id}/{product_id}": (ALL, ("recipients.view",)),
})

for _ep in ("abc", "adjustments", "coverage", "dead-stock", "employees", "executive-report", "heatmap", "kpis", "margin",
            "price-variance", "trend"):
    ENDPOINT_PERMS[f"GET /api/analytics/{_ep}"] = (ALL, ("reports.view", "finance.view"))

_add("/api/dashboard", {"GET /overview": (S, ()), "GET /flow": (S, ())})

_add("/api/excel", {
    "GET /products/export": (ALL, ("products.export",)),
    "GET /products/template": (ALL, ("products.import",)),
    "POST /products/import": (ALL, ("products.import",)),
    "GET /movements/export": (ALL, ("invoices.view",)),
    "GET /quantity-adjustments/export": (ALL, ("reports.view",)),
})

_add("/api/locations", {
    "GET /warehouses": (ANY, LOCATION_READ),
    "GET /tree": (ANY, LOCATION_READ),
    "GET /children": (ANY, LOCATION_READ),
    "GET /index": (ANY, LOCATION_READ),
    "GET /resolve": (ANY, LOCATION_READ),
    "GET /reports/stock": (ANY, LOCATION_READ),
    "GET /putaway": (ANY, ("transfers.create", "transfers.view", "locations.view")),
    "GET /transfers": (ALL, ("transfers.view",)),
    "POST /transfer": (ALL, ("transfers.create",)),
    "POST /warehouses": (ALL, ("locations.manage",)),
    "PUT /warehouses/{warehouse_id}": (ALL, ("locations.manage",)),
    "PUT /warehouses/{warehouse_id}/default-location": (ALL, ("locations.manage",)),
    "POST ": (ALL, ("locations.manage",)),
    "PUT /{location_id}": (ALL, ("locations.manage",)),
    "DELETE /warehouses/{warehouse_id}": (ALL, ("locations.delete",)),
    "DELETE /{location_id}": (ALL, ("locations.delete",)),
    "GET /nav/describe": (S, ()),
    "GET /nav/summary": (ANY, LOCATION_READ),
    "GET /nav/roots": (ANY, LOCATION_READ),
    "GET /nav/children": (ANY, LOCATION_READ),
    "GET /nav/search": (ANY, LOCATION_READ),
})

_add("/api/company", {
    "GET ": (S, ()),
    "GET /logo": (S, ()),
    "PUT ": (ALL, ("settings.company",)),
    "POST /logo": (ALL, ("settings.company",)),
    "DELETE /logo": (ALL, ("settings.company",)),
})

ENDPOINT_PERMS["GET /api/health"] = (P, ())
ENDPOINT_PERMS["GET /api/ready"] = (P, ())

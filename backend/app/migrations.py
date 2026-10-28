"""
ترقية بسيطة للـ schema: Base.metadata.create_all بينشئ الجداول الناقصة بس، ومش بيضيف أعمدة جديدة
لجدول موجود. الدالة دي بتضيف الأعمدة الجديدة (لو مش موجودة) من غير ما تمس أي بيانات، وبتشتغل
على SQLite و MySQL، ومأمونة لو اتنفذت أكتر من مرة.
"""
from sqlalchemy import inspect, text

# (الجدول, العمود, تعريف العمود)
_COLUMNS = [
    ("products", "is_active", "BOOLEAN NOT NULL DEFAULT 1"),
    ("stock_in_items", "location_id", "INTEGER NULL"),
    ("stock_out_items", "location_id", "INTEGER NULL"),
    ("warehouses", "default_location_id", "INTEGER NULL"),
    # فواتير الصادر: الحالة (معلّقة/مقفولة) + الخصم والضريبة. الفواتير القديمة بتاخد completed تلقائيًا
    ("stock_out_invoices", "status", "VARCHAR(20) NOT NULL DEFAULT 'completed'"),
    ("stock_out_invoices", "discount_type", "VARCHAR(10) NULL"),
    ("stock_out_invoices", "discount_value", "DECIMAL(14,2) NULL"),
    ("stock_out_invoices", "tax_type", "VARCHAR(10) NULL"),
    ("stock_out_invoices", "tax_value", "DECIMAL(14,2) NULL"),
    ("stock_out_invoices", "subtotal_amount", "DECIMAL(14,2) NULL"),
    ("stock_out_invoices", "discount_amount", "DECIMAL(14,2) NULL"),
    ("stock_out_invoices", "tax_amount", "DECIMAL(14,2) NULL"),
    ("stock_out_invoices", "grand_total", "DECIMAL(14,2) NULL"),
]
_INDEXES = [
    ("products", "ix_products_is_active", "is_active"),
    # فهارس أداء للتقارير والسجلات (التواريخ وأعمدة الربط بتتفلتر وتتجمّع عليها كتير)
    ("stock_in_invoices", "ix_stock_in_invoices_date", "date"),
    ("stock_out_invoices", "ix_stock_out_invoices_date", "date"),
    ("stock_in_invoices", "ix_stock_in_invoices_supplier", "supplier_id"),
    ("stock_out_invoices", "ix_stock_out_invoices_recipient", "recipient_id"),
    ("stock_in_items", "ix_stock_in_items_invoice", "invoice_id"),
    ("stock_in_items", "ix_stock_in_items_product", "product_id"),
    ("stock_out_items", "ix_stock_out_items_invoice", "invoice_id"),
    ("stock_out_items", "ix_stock_out_items_product", "product_id"),
    ("quantity_adjustments", "ix_quantity_adjustments_date", "date"),
    ("quantity_adjustments", "ix_quantity_adjustments_product", "product_id"),
    ("login_logs", "ix_login_logs_login_time", "login_time"),
    ("products", "ix_products_name", "name"),
]


def ensure_schema(engine) -> None:
    insp = inspect(engine)
    tables = set(insp.get_table_names())
    for table, column, ddl in _COLUMNS:
        if table not in tables:
            continue
        if column in {c["name"] for c in insp.get_columns(table)}:
            continue
        try:
            with engine.begin() as conn:
                conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}"))
            print(f"✔ تمت إضافة العمود {table}.{column}")
        except Exception:
            # على serverless ممكن instance تانية تكون سبقتنا وأضافت نفس العمود في نفس اللحظة - ده مش خطأ
            if column in {c["name"] for c in inspect(engine).get_columns(table)}:
                continue
            raise
    insp = inspect(engine)
    for table, index_name, column in _INDEXES:
        if table not in tables or index_name in {i["name"] for i in insp.get_indexes(table)}:
            continue
        try:
            with engine.begin() as conn:
                conn.execute(text(f"CREATE INDEX {index_name} ON {table} ({column})"))
        except Exception as exc:  # الفهرس تحسين أداء بس - مش سبب نوقف السيرفر
            print(f"⚠ تعذّر إنشاء الفهرس {index_name}: {exc}")

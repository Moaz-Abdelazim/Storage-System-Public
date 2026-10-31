from sqlalchemy import (
    Column, Index, Integer, String, DateTime, ForeignKey, Text, Boolean, UniqueConstraint, CheckConstraint, Numeric, LargeBinary
)
from sqlalchemy.dialects.mysql import LONGBLOB
from sqlalchemy import event
from sqlalchemy.orm import relationship
from datetime import datetime
from .database import Base
from .timezone_utils import now_cairo


# الكميات DECIMAL(14,3) والفلوس DECIMAL(14,2): دقة مضمونة في الـ DB. asdecimal=False = بتتقرا في Python كـ float
# (شكل الـ API والتقارير ما بيتغيرش)، والكتابة بتعدّي دايمًا على app/money.py (Decimal + ROUND_HALF_UP).
def Qty():
    return Numeric(14, 3, asdecimal=False)


def Money():
    return Numeric(14, 2, asdecimal=False)


class Role(Base):
    """
    دور بيحدده الأدمن (اسم + صلاحيات من الكتالوج في app/permissions.py). الدور is_system ("مدير النظام") عنده
    كل الصلاحيات ضمنيًا (مش متخزنة في role_permissions) ومينفعش يتعدّل ولا يتعطّل ولا يتمسح.
    """
    __tablename__ = "roles"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(50), unique=True, nullable=False)
    description = Column(String(255), nullable=True)
    is_active = Column(Boolean, nullable=False, default=True, server_default="1")
    is_system = Column(Boolean, nullable=False, default=False, server_default="0")
    created_at = Column(DateTime, default=now_cairo)
    updated_at = Column(DateTime, default=now_cairo, onupdate=now_cairo)

    permissions = relationship("RolePermission", back_populates="role", cascade="all, delete-orphan", passive_deletes=True)
    users = relationship("User", back_populates="role")


class RolePermission(Base):
    __tablename__ = "role_permissions"

    role_id = Column(Integer, ForeignKey("roles.id", ondelete="CASCADE"), primary_key=True)
    permission = Column(String(64), primary_key=True)

    role = relationship("Role", back_populates="permissions")


class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, index=True)
    username = Column(String(50), unique=True, index=True, nullable=False)
    full_name = Column(String(100), nullable=True)
    password_hash = Column(String(255), nullable=False)
    role_id = Column(Integer, ForeignKey("roles.id", ondelete="RESTRICT"), nullable=False, index=True)
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime, default=now_cairo)

    role = relationship("Role", back_populates="users")
    login_logs = relationship("LoginLog", back_populates="user", foreign_keys="LoginLog.user_id")

    @property
    def is_admin(self) -> bool:
        """الأدمن = دور is_system (مفيش أي فحص نصي على اسم الدور في أي مكان)."""
        return bool(self.role is not None and self.role.is_system)

    @property
    def role_name(self) -> str:
        return self.role.name if self.role is not None else ""


class LoginLog(Base):
    """
    كل صف هنا هو أيضًا "جلسة" فعلية بتتحقق منها كل طلب للـ API (مش مجرد سجل تاريخي) -
    التوكن بياخد رقم الصف ده (session id) وقت الدخول، ولو الصف اتقفل (logout_time اتحط)
    التوكن بيبقى مرفوض على طول حتى لو لسه صالح من ناحية تاريخ الانتهاء.
    """
    __tablename__ = "login_logs"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=True)  # ممكن يبقى NULL لو الحساب اتمسح بعدين
    username = Column(String(50), nullable=True)  # نسخة من الاسم وقت الدخول - تفضل موجودة حتى لو الحساب اتمسح
    login_time = Column(DateTime, default=now_cairo)
    logout_time = Column(DateTime, nullable=True)  # لو مليان يبقى الجلسة دي مقفولة (خروج عادي أو إغلاق من الأدمن)
    ip_address = Column(String(50), nullable=True)
    closed_by_id = Column(Integer, ForeignKey("users.id"), nullable=True)  # لو أدمن هو اللي قفلها بالقوة

    user = relationship("User", back_populates="login_logs", foreign_keys=[user_id])
    closed_by = relationship("User", foreign_keys=[closed_by_id])


class Category(Base):
    """تصنيفات المنتجات - قابلة للإضافة والتعديل من الواجهة."""
    __tablename__ = "categories"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(100), unique=True, nullable=False)
    created_at = Column(DateTime, default=now_cairo)

    products = relationship("Product", back_populates="category")


class Supplier(Base):
    __tablename__ = "suppliers"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(150), nullable=False)
    phone = Column(String(30), nullable=True)
    address = Column(String(255), nullable=True)
    notes = Column(Text, nullable=True)
    created_at = Column(DateTime, default=now_cairo)

    stock_in_invoices = relationship("StockInInvoice", back_populates="supplier")


class Recipient(Base):
    """الجهة اللي بتستلم البضاعة لما تخرج من المخزن (عميل / فرع / قسم)."""
    __tablename__ = "recipients"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(150), nullable=False)
    phone = Column(String(30), nullable=True)
    address = Column(String(255), nullable=True)
    notes = Column(Text, nullable=True)
    created_at = Column(DateTime, default=now_cairo)

    stock_out_invoices = relationship("StockOutInvoice", back_populates="recipient")


class Brand(Base):
    """ماركات المنتجات - نفس فكرة التصنيف بالظبط، قابلة للإضافة والتعديل من الواجهة."""
    __tablename__ = "brands"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(100), unique=True, nullable=False)
    created_at = Column(DateTime, default=now_cairo)

    products = relationship("Product", back_populates="brand")


class Product(Base):
    __tablename__ = "products"
    __table_args__ = (
        CheckConstraint("quantity >= 0", name="quantity_nonneg"),
        UniqueConstraint("barcode", name="uq_products_barcode"),  # الـ unique بيعمل index تلقائي؛ التفرّد case-insensitive بيتفحص في الـ API
    )

    id = Column(Integer, primary_key=True, index=True)
    sku = Column(String(50), unique=True, index=True, nullable=True)  # اختياري - كود داخلي للشركة
    barcode = Column(String(64), nullable=True)  # اختياري - الكود المطبوع على الملصق (نص عشان الأصفار في الأول)
    name = Column(String(150), nullable=False)
    category_id = Column(Integer, ForeignKey("categories.id"), nullable=True)
    brand_id = Column(Integer, ForeignKey("brands.id"), nullable=True)
    unit = Column(String(30), default="قطعة")
    price = Column(Money(), nullable=False, default=0.0, server_default="0")
    quantity = Column(Qty(), nullable=False, default=0.0, server_default="0")  # الكمية الحالية بالمخزن (= مجموع الأماكن)
    min_quantity = Column(Qty(), nullable=False, default=0.0, server_default="0")  # حد أدنى للتنبيه
    # المنتجات ما بتتحذفش - بتتعطّل (is_active=False) فتختفي من القوايم والفواتير الجديدة وسجلها يفضل محفوظ
    is_active = Column(Boolean, nullable=False, default=True, server_default="1", index=True)
    created_at = Column(DateTime, default=now_cairo)
    updated_at = Column(DateTime, default=now_cairo, onupdate=now_cairo)

    category = relationship("Category", back_populates="products")
    brand = relationship("Brand", back_populates="products")
    stock_in_items = relationship("StockInItem", back_populates="product")
    stock_out_items = relationship("StockOutItem", back_populates="product")
    locations = relationship("ProductLocation", back_populates="product", cascade="all, delete-orphan")


class StockInInvoice(Base):
    """فاتورة وارد - ممكن تحتوي على أكتر من منتج في نفس العملية."""
    __tablename__ = "stock_in_invoices"

    id = Column(Integer, primary_key=True, index=True)
    supplier_id = Column(Integer, ForeignKey("suppliers.id"), nullable=True)
    received_by_id = Column(Integer, ForeignKey("users.id"), nullable=True)  # مين استلم (ممكن يبقى NULL لو اتمسح)
    received_by_username = Column(String(50), nullable=True)  # نسخة من اسمه وقت التسجيل - بتفضل موجودة دايمًا
    date = Column(DateTime, default=now_cairo)
    notes = Column(Text, nullable=True)

    supplier = relationship("Supplier", back_populates="stock_in_invoices")
    received_by = relationship("User")
    items = relationship("StockInItem", back_populates="invoice", cascade="all, delete-orphan")


class StockInItem(Base):
    """سطر منتج داخل فاتورة وارد."""
    __tablename__ = "stock_in_items"

    id = Column(Integer, primary_key=True, index=True)
    invoice_id = Column(Integer, ForeignKey("stock_in_invoices.id"), nullable=False)
    product_id = Column(Integer, ForeignKey("products.id"), nullable=False)
    quantity = Column(Qty(), nullable=False)
    unit_price = Column(Money(), nullable=True)

    invoice = relationship("StockInInvoice", back_populates="items")
    product = relationship("Product", back_populates="stock_in_items")
    location_id = Column(Integer, ForeignKey("locations.id"), nullable=True)  # مكان التخزين اللي اتحط/اتسحب منه (اختياري)
    location = relationship("Location")


class StockOutInvoice(Base):
    """فاتورة صادر - ممكن تحتوي على أكتر من منتج في نفس العملية."""
    __tablename__ = "stock_out_invoices"

    id = Column(Integer, primary_key=True, index=True)
    recipient_id = Column(Integer, ForeignKey("recipients.id"), nullable=True)
    delivered_by_id = Column(Integer, ForeignKey("users.id"), nullable=True)  # مين سلم (ممكن يبقى NULL لو اتمسح)
    delivered_by_username = Column(String(50), nullable=True)  # نسخة من اسمه وقت التسجيل - بتفضل موجودة دايمًا
    date = Column(DateTime, default=now_cairo)
    notes = Column(Text, nullable=True)

    # حالة الفاتورة: completed (مقفولة) / held (معلّقة: الكمية اتخصمت من المخزون وممكن تتعدّل أو تتمسح أو تتقفل)
    status = Column(String(20), nullable=False, default="completed", server_default="completed")
    # خصم/ضريبة على إجمالي الفاتورة (اختياريين): النوع amount (مبلغ ثابت) أو percent (نسبة %) + القيمة
    discount_type = Column(String(10), nullable=True)
    discount_value = Column(Numeric(14, 2, asdecimal=False), nullable=True)
    tax_type = Column(String(10), nullable=True)
    tax_value = Column(Numeric(14, 2, asdecimal=False), nullable=True)
    # الإجماليات المحسوبة وقت الحفظ (NULL في الفواتير القديمة - بتتحسب من الأصناف وقت العرض)
    subtotal_amount = Column(Numeric(14, 2, asdecimal=False), nullable=True)
    discount_amount = Column(Numeric(14, 2, asdecimal=False), nullable=True)
    tax_amount = Column(Numeric(14, 2, asdecimal=False), nullable=True)
    grand_total = Column(Numeric(14, 2, asdecimal=False), nullable=True)

    recipient = relationship("Recipient", back_populates="stock_out_invoices")
    delivered_by = relationship("User")
    items = relationship("StockOutItem", back_populates="invoice", cascade="all, delete-orphan")


class StockOutItem(Base):
    """سطر منتج داخل فاتورة صادر."""
    __tablename__ = "stock_out_items"

    id = Column(Integer, primary_key=True, index=True)
    invoice_id = Column(Integer, ForeignKey("stock_out_invoices.id"), nullable=False)
    product_id = Column(Integer, ForeignKey("products.id"), nullable=False)
    quantity = Column(Qty(), nullable=False)
    unit_price = Column(Money(), nullable=True)

    invoice = relationship("StockOutInvoice", back_populates="items")
    product = relationship("Product", back_populates="stock_out_items")
    location_id = Column(Integer, ForeignKey("locations.id"), nullable=True)  # مكان التخزين اللي اتحط/اتسحب منه (اختياري)
    location = relationship("Location")


class QuantityAdjustment(Base):
    """
    سجل لأي تعديل يدوي على كمية منتج (بره فواتير الوارد/الصادر العادية).
    مش بيظهر في الموقع - بس قابل للتحميل كملف Excel من تاب التقارير (للأدمن).
    """
    __tablename__ = "quantity_adjustments"

    id = Column(Integer, primary_key=True, index=True)
    product_id = Column(Integer, ForeignKey("products.id"), nullable=False)
    old_quantity = Column(Qty(), nullable=False)
    new_quantity = Column(Qty(), nullable=False)
    difference = Column(Qty(), nullable=False)
    changed_by_id = Column(Integer, ForeignKey("users.id"), nullable=True)  # ممكن يبقى NULL لو الحساب اتمسح
    changed_by_username = Column(String(50), nullable=True)  # نسخة من اسمه وقت التعديل
    reason = Column(Text, nullable=True)
    date = Column(DateTime, default=now_cairo)

    product = relationship("Product")
    changed_by = relationship("User")



# =====================================================================
#  Dynamic Location Hierarchy
#  Warehouse -> Location -> Location -> ... (عدد مستويات مفتوح لكل مخزن)
#  الـ Full Location Code (زي W1-F3-R5-S2) بيتبني وقت العرض من الـ parents
#  ومش متخزن في أي جدول.
# =====================================================================
class Warehouse(Base):
    __tablename__ = "warehouses"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(100), nullable=False)
    code = Column(String(30), unique=True, index=True, nullable=False)  # الكود اللي المستخدم بيشوفه (بيتخزن Upper-case)
    # مكان الاستلام الافتراضي (اختياري): أي بضاعة بتدخل المخزن ده من فاتورة وارد بتتحط فيه تلقائيًا، وبعدين
    # الموظف بينقلها لمكانها النهائي (الرص). من غير FK عشان ما يبقاش فيه تبعية دائرية مع locations - الـ API هو اللي بيتحقق.
    default_location_id = Column(Integer, nullable=True)
    created_at = Column(DateTime, default=now_cairo)

    locations = relationship("Location", back_populates="warehouse")


class Location(Base):
    __tablename__ = "locations"
    # parent_key = COALESCE(parent_id, 0): عمود عادي NOT NULL بيتحدّث تلقائيًا (listener تحت) عشان الـ unique يمنع تكرار الكود
    # بين الإخوة حتى لما parent_id = NULL (في MySQL/SQLite الـ NULL مش بيتساوى بـ NULL فالـ unique القديم كان بيسمح بالتكرار)
    __table_args__ = (UniqueConstraint("warehouse_id", "parent_key", "code", name="uq_location_sibling_code"),)

    id = Column(Integer, primary_key=True, index=True)
    warehouse_id = Column(Integer, ForeignKey("warehouses.id"), nullable=False, index=True)
    parent_id = Column(Integer, ForeignKey("locations.id"), nullable=True, index=True)  # NULL = تحت المخزن مباشرة
    parent_key = Column(Integer, nullable=False, default=0, server_default="0")
    name = Column(String(100), nullable=False)
    code = Column(String(30), nullable=False)  # كود المستوى ده بس (مش الكود الكامل)
    type = Column(String(50), nullable=True)  # اختياري: دور / غرفة / رف ... (وصف فقط، النظام ما بيفترضش معنى له)
    created_at = Column(DateTime, default=now_cairo)

    warehouse = relationship("Warehouse", back_populates="locations")
    parent = relationship("Location", remote_side=[id], back_populates="children")
    children = relationship("Location", back_populates="parent")


class ProductLocation(Base):
    """كمية المنتج في مكان تخزين معيّن (المنتج الواحد ممكن يبقى في أكتر من مكان)."""
    __tablename__ = "product_locations"
    __table_args__ = (
        UniqueConstraint("product_id", "location_id", name="uq_product_location"),
        CheckConstraint("quantity >= 0", name="quantity_nonneg"),
    )

    id = Column(Integer, primary_key=True, index=True)
    product_id = Column(Integer, ForeignKey("products.id"), nullable=False, index=True)
    location_id = Column(Integer, ForeignKey("locations.id"), nullable=False, index=True)
    quantity = Column(Qty(), nullable=False, default=0.0)

    product = relationship("Product", back_populates="locations")
    location = relationship("Location")


class LocationTransfer(Base):
    """سجل نقل كمية منتج من مكان لمكان - حركة مستقلة بتظهر في سجل الحركات والتقارير."""
    __tablename__ = "location_transfers"

    id = Column(Integer, primary_key=True, index=True)
    product_id = Column(Integer, ForeignKey("products.id"), nullable=False, index=True)
    from_location_id = Column(Integer, ForeignKey("locations.id"), nullable=False)
    to_location_id = Column(Integer, ForeignKey("locations.id"), nullable=False)
    quantity = Column(Qty(), nullable=False)
    moved_by_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    moved_by_username = Column(String(50), nullable=True)  # نسخة من الاسم وقت النقل - بتفضل حتى لو الحساب اتمسح
    notes = Column(Text, nullable=True)
    date = Column(DateTime, default=now_cairo, index=True)

    product = relationship("Product")
    from_location = relationship("Location", foreign_keys=[from_location_id])
    to_location = relationship("Location", foreign_keys=[to_location_id])
    moved_by = relationship("User")


class LoginAttempt(Base):
    """محاولة دخول فاشلة - بتتخزن في القاعدة (مش في الذاكرة) عشان حماية التخمين تشتغل صح مع أكتر من worker
    وما تتصفّرش لما السيرفر يتعمله restart. بتتنضّف تلقائيًا بعد انتهاء فترة الحظر."""
    __tablename__ = "login_attempts"

    id = Column(Integer, primary_key=True, index=True)
    ip = Column(String(64), nullable=False, index=True)
    username = Column(String(64), nullable=False, index=True)  # lower-case
    attempted_at = Column(DateTime, nullable=False, index=True)  # UTC


class CompanySettings(Base):
    """بيانات الشركة (صف واحد بس id=1): الاسم + رقمين تليفون + اللوجو (بيتخزن في القاعدة نفسها)."""
    __tablename__ = "company_settings"

    id = Column(Integer, primary_key=True)
    name = Column(String(150), nullable=True)
    phone1 = Column(String(30), nullable=True)
    phone2 = Column(String(30), nullable=True)
    # LONGBLOB على MySQL (الـ BLOB العادي حده 64KB) و BLOB عادي على SQLite
    logo = Column(LargeBinary().with_variant(LONGBLOB(), "mysql"), nullable=True)
    logo_mime = Column(String(30), nullable=True)
    logo_etag = Column(String(64), nullable=True)  # SHA-1 للمحتوى: بيتستخدم كـ ETag وكرقم نسخة للكاش
    updated_at = Column(DateTime, default=now_cairo, onupdate=now_cairo)


# فهارس أداء للتقارير والسجلات (التواريخ وأعمدة الربط بتتفلتر وتتجمّع عليها كتير) - كانت في migrations.py القديم
_EXTRA_INDEXES = [
    ("ix_stock_in_invoices_date", StockInInvoice.date),
    ("ix_stock_out_invoices_date", StockOutInvoice.date),
    ("ix_stock_in_invoices_supplier", StockInInvoice.supplier_id),
    ("ix_stock_out_invoices_recipient", StockOutInvoice.recipient_id),
    ("ix_stock_in_items_invoice", StockInItem.invoice_id),
    ("ix_stock_in_items_product", StockInItem.product_id),
    ("ix_stock_out_items_invoice", StockOutItem.invoice_id),
    ("ix_stock_out_items_product", StockOutItem.product_id),
    ("ix_quantity_adjustments_date", QuantityAdjustment.date),
    ("ix_quantity_adjustments_product", QuantityAdjustment.product_id),
    ("ix_login_logs_login_time", LoginLog.login_time),
]
for _name, _col in _EXTRA_INDEXES:
    Index(_name, _col)


@event.listens_for(Location, "before_insert")
@event.listens_for(Location, "before_update")
def _sync_location_parent_key(mapper, connection, target):
    target.parent_key = target.parent_id or 0

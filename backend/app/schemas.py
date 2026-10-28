from pydantic import BaseModel, Field, field_validator
from datetime import datetime
from typing import Annotated, Literal, Optional, List

# أرقام محدودة ومنتهية: بتمنع NaN / Infinity (كانت بتعدّي فحص "الكمية أكبر من صفر" وتبوّظ المخزون)
Num = Annotated[float, Field(allow_inf_nan=False, ge=-1e12, le=1e12)]
NonNegNum = Annotated[float, Field(allow_inf_nan=False, ge=0, le=1e12)]


# ---------- Auth ----------
def _clean_username(value: str) -> str:
    value = (value or "").strip()
    if not (3 <= len(value) <= 50):
        raise ValueError("اسم المستخدم لازم يكون من 3 لـ 50 حرف")
    if any(ch.isspace() or ord(ch) < 32 for ch in value):
        raise ValueError("اسم المستخدم ما ينفعش يحتوي على مسافات أو رموز تحكم")
    return value


class LoginRequest(BaseModel):
    username: str = Field(max_length=50)
    password: str = Field(max_length=128)


class Token(BaseModel):
    access_token: str = ""  # فاضي: التوكن في HttpOnly cookie ومش بيتبعت في الـ body
    token_type: str = "cookie"
    username: str
    role: str


class UserOut(BaseModel):
    id: int
    username: str
    full_name: Optional[str] = None
    role: str
    is_active: bool

    class Config:
        from_attributes = True


def _check_password_strength(value: str) -> str:
    if len(value) < 8:
        raise ValueError("كلمة المرور لازم تكون 8 حروف على الأقل")
    if len(value.encode("utf-8")) > 72:
        raise ValueError("كلمة المرور طويلة جدًا (أقصى حاجة 72 بايت)")
    if value.strip() == "" or value.lower() in {"password", "12345678", "123456789", "admin123", "11111111"}:
        raise ValueError("كلمة المرور ضعيفة جدًا")
    return value


class UserCreate(BaseModel):
    username: str
    password: str
    full_name: Optional[str] = Field(default=None, max_length=100)
    role: str = "staff"

    @field_validator("username")
    @classmethod
    def _un(cls, v):
        return _clean_username(v)

    @field_validator("password")
    @classmethod
    def _pw(cls, v):
        return _check_password_strength(v)

    @field_validator("role")
    @classmethod
    def _role(cls, v):
        if v not in ("admin", "staff"):
            raise ValueError("الصلاحية لازم تكون admin أو staff")
        return v


class UserUpdate(BaseModel):
    username: Optional[str] = None
    password: Optional[str] = None
    full_name: Optional[str] = Field(default=None, max_length=100)
    role: Optional[str] = None
    is_active: Optional[bool] = None

    @field_validator("username")
    @classmethod
    def _un(cls, v):
        return None if v in (None, "") else _clean_username(v)

    @field_validator("password")
    @classmethod
    def _pw(cls, v):
        return None if v in (None, "") else _check_password_strength(v)

    @field_validator("role")
    @classmethod
    def _role(cls, v):
        if v is not None and v not in ("admin", "staff"):
            raise ValueError("الصلاحية لازم تكون admin أو staff")
        return v


# ---------- Category ----------
class CategoryCreate(BaseModel):
    name: str = Field(max_length=100)


class CategoryUpdate(BaseModel):
    name: str = Field(max_length=100)


class CategoryOut(BaseModel):
    id: int
    name: str
    created_at: datetime

    class Config:
        from_attributes = True


# ---------- Brand ----------
class BrandCreate(BaseModel):
    name: str = Field(max_length=100)


class BrandUpdate(BaseModel):
    name: str = Field(max_length=100)


class BrandOut(BaseModel):
    id: int
    name: str
    created_at: datetime

    class Config:
        from_attributes = True


# ---------- Supplier ----------
class SupplierBase(BaseModel):
    name: str = Field(min_length=1, max_length=150)
    phone: Optional[str] = Field(default=None, max_length=30)
    address: Optional[str] = Field(default=None, max_length=255)
    notes: Optional[str] = Field(default=None, max_length=5000)


class SupplierCreate(SupplierBase):
    pass


class SupplierOut(SupplierBase):
    id: int
    created_at: datetime
    invoices_count: int = 0
    total_quantity: float = 0

    class Config:
        from_attributes = True


# ---------- Recipient ----------
class RecipientBase(BaseModel):
    name: str = Field(min_length=1, max_length=150)
    phone: Optional[str] = Field(default=None, max_length=30)
    address: Optional[str] = Field(default=None, max_length=255)
    notes: Optional[str] = Field(default=None, max_length=5000)


class RecipientCreate(RecipientBase):
    pass


class RecipientOut(RecipientBase):
    id: int
    created_at: datetime
    invoices_count: int = 0
    total_quantity: float = 0

    class Config:
        from_attributes = True


# ---------- Locations ----------
class WarehouseCreate(BaseModel):
    name: str
    code: str


class DefaultLocationSet(BaseModel):
    location_id: Optional[int] = None  # None = إلغاء مكان الاستلام الافتراضي


class WarehouseUpdate(BaseModel):
    name: Optional[str] = None
    code: Optional[str] = None


class LocationCreate(BaseModel):
    name: str = Field(max_length=100)
    code: str = Field(max_length=30)
    type: Optional[str] = Field(default=None, max_length=50)
    parent_id: Optional[int] = None      # لو موجود بيبقى هو الـ parent (والمخزن بيتحدد منه تلقائيًا)
    warehouse_id: Optional[int] = None   # لازم لو مفيش parent_id (موقع على مستوى المخزن مباشرة)


class LocationUpdate(BaseModel):
    name: Optional[str] = None
    code: Optional[str] = None
    type: Optional[str] = None


class ProductLocationIn(BaseModel):
    location_id: int
    quantity: NonNegNum


class ProductLocationOut(BaseModel):
    location_id: int
    full_code: str
    quantity: float


class ProductLocationsSet(BaseModel):
    locations: List[ProductLocationIn]
    reason: Optional[str] = None


class TransferCreate(BaseModel):
    product_id: int
    from_location_id: int
    to_location_id: int
    quantity: Num
    notes: Optional[str] = Field(default=None, max_length=1000)


# ---------- Product ----------
class ProductBase(BaseModel):
    sku: Optional[str] = Field(default=None, max_length=50)
    name: str = Field(min_length=1, max_length=150)
    category_id: Optional[int] = None
    brand_id: Optional[int] = None
    unit: Optional[str] = Field(default="قطعة", max_length=30)
    price: NonNegNum = 0.0
    min_quantity: NonNegNum = 0.0


class ProductCreate(ProductBase):
    quantity: NonNegNum = 0.0  # كمية "من غير موقع محدد" (اختيارية) - بتتضاف على كميات المواقع لو اتحددت
    locations: List[ProductLocationIn] = []


class ProductUpdate(BaseModel):
    name: Optional[str] = Field(default=None, min_length=1, max_length=150)
    category_id: Optional[int] = None
    brand_id: Optional[int] = None
    unit: Optional[str] = Field(default=None, max_length=30)
    price: Optional[NonNegNum] = None
    min_quantity: Optional[NonNegNum] = None


class ProductOut(BaseModel):
    id: int
    sku: Optional[str] = None
    name: str
    category_id: Optional[int] = None
    category_name: Optional[str] = None
    brand_id: Optional[int] = None
    brand_name: Optional[str] = None
    unit: Optional[str] = None
    price: float
    quantity: float
    min_quantity: float
    updated_at: datetime
    is_active: bool = True
    locations: List[ProductLocationOut] = []
    unassigned_quantity: float = 0.0   # الجزء من الكمية الإجمالية اللي مش متحدد له موقع
    scoped_quantity: Optional[float] = None  # الكمية جوه الموقع المفلتر عليه (لو فيه فلتر)

    class Config:
        from_attributes = True


class ProductActiveSet(BaseModel):
    is_active: bool


class QuantityAdjustmentCreate(BaseModel):
    location_id: int            # المكان اللي بنصحّح كميته (تصحيح جرد)
    new_quantity: NonNegNum     # الكمية الفعلية في المكان ده
    reason: Optional[str] = Field(default=None, max_length=1000)


# ---------- Stock In (invoice style) ----------
class StockInItemCreate(BaseModel):
    product_id: int
    quantity: Num
    unit_price: Optional[NonNegNum] = None
    # الصنف بيدخل مخزن (warehouse_id) فيتحط في مكان الاستلام الافتراضي بتاعه، أو مكان محدد (location_id) لو اتحدد
    warehouse_id: Optional[int] = None
    location_id: Optional[int] = None


class StockInInvoiceCreate(BaseModel):
    supplier_id: Optional[int] = None
    notes: Optional[str] = Field(default=None, max_length=5000)
    items: List[StockInItemCreate] = Field(max_length=500)


class StockInItemOut(BaseModel):
    id: int
    product_id: int
    product_name: Optional[str] = None
    quantity: float
    unit_price: Optional[float] = None
    location_id: Optional[int] = None
    location_code: Optional[str] = None


class StockInInvoiceOut(BaseModel):
    id: int
    supplier_id: Optional[int] = None
    supplier_name: Optional[str] = None
    received_by_id: Optional[int] = None
    received_by_name: Optional[str] = None
    date: datetime
    notes: Optional[str] = None
    items: List[StockInItemOut] = []
    items_total: int = 0  # إجمالي عدد الأصناف في الفاتورة (ممكن يكون أكبر من طول items لو معروضة مختصرة)
    total_value: float = 0.0  # إجمالي قيمة الفاتورة كاملة (محسوب من كل الأصناف مش بس المعروض منها)


# ---------- Stock Out (invoice style) ----------
class StockOutItemCreate(BaseModel):
    product_id: int
    quantity: Num
    unit_price: Optional[NonNegNum] = None
    location_id: Optional[int] = None


class StockOutInvoiceCreate(BaseModel):
    recipient_id: Optional[int] = None
    notes: Optional[str] = Field(default=None, max_length=5000)
    items: List[StockOutItemCreate] = Field(max_length=500)
    # اختياري: completed (الافتراضي - الفاتورة تتقفل) أو held (تتساب معلّقة: الكمية بتتخصم وممكن تتعدّل أو تتمسح أو تتقفل بعدين)
    status: Literal["completed", "held"] = "completed"
    # خصم/ضريبة اختياريين على إجمالي الفاتورة (النوع: amount = مبلغ ثابت، percent = نسبة %). الحساب والفاليديشن في الباك اند.
    discount_type: Optional[Literal["amount", "percent"]] = None
    discount_value: Optional[Num] = None
    tax_type: Optional[Literal["amount", "percent"]] = None
    tax_value: Optional[Num] = None


class StockOutInvoiceUpdate(BaseModel):
    """تعديل فاتورة صادر معلّقة: بنفس شكل الإنشاء (الأصناف بتتبدّل كلها) من غير الحالة."""
    recipient_id: Optional[int] = None
    notes: Optional[str] = Field(default=None, max_length=5000)
    items: List[StockOutItemCreate] = Field(max_length=500)
    discount_type: Optional[Literal["amount", "percent"]] = None
    discount_value: Optional[Num] = None
    tax_type: Optional[Literal["amount", "percent"]] = None
    tax_value: Optional[Num] = None


class StockOutItemOut(BaseModel):
    id: int
    product_id: int
    product_name: Optional[str] = None
    quantity: float
    unit_price: Optional[float] = None
    location_id: Optional[int] = None
    location_code: Optional[str] = None


class StockOutInvoiceOut(BaseModel):
    id: int
    recipient_id: Optional[int] = None
    recipient_name: Optional[str] = None
    delivered_by_id: Optional[int] = None
    delivered_by_name: Optional[str] = None
    date: datetime
    notes: Optional[str] = None
    items: List[StockOutItemOut] = []
    items_total: int = 0  # إجمالي عدد الأصناف في الفاتورة (ممكن يكون أكبر من طول items لو معروضة مختصرة)
    total_value: float = 0.0  # إجمالي قيمة الفاتورة كاملة (محسوب من كل الأصناف مش بس المعروض منها)
    # ---- إضافات (اختيارية بقيم افتراضية - الفرونت القديم بيتجاهلها) ----
    status: str = "completed"
    discount_type: Optional[str] = None
    discount_value: Optional[float] = None
    tax_type: Optional[str] = None
    tax_value: Optional[float] = None
    subtotal: Optional[float] = None
    discount_amount: float = 0.0
    tax_amount: float = 0.0
    grand_total: Optional[float] = None

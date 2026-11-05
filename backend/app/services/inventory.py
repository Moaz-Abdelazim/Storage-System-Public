"""
مصدر الحقيقة الوحيد لتغيير أرصدة المخزون (product_locations + products.quantity).

القواعد (دي اللي بتقفل ثغرة الرصيد السالب على MySQL REPEATABLE READ):
1. ممنوع القراءة ثم الكتابة بقيمة محسوبة في Python. كل تغيير هو UPDATE نسبي (quantity = quantity ± q)
   والخصم بشرط في الـ SQL نفسه (WHERE quantity >= q) وبنفحص rowcount.
2. أي قراءة رصيد جوه الـ service بتكون locking read (FOR UPDATE) عشان تشوف آخر قيمة committed
   مش الـ snapshot القديم.
3. كل عملية بتحدّث product_locations و products.quantity في نفس الـ transaction.
4. ترتيب ثابت (product_id ثم location_id) + قفل المنتجات الأول (lock_products) = مفيش deadlock دايري.
   ولو حصل deadlock/lock-timeout على MySQL فيه retry محدود (with_retry).
5. الـ service مبيعملش commit - الـ endpoint هو اللي بيقرر (عشان الفاتورة كلها transaction واحد).
6. الحجز: product_locations.reserved (جزء من quantity مش خارج منه). available = quantity - reserved.
   أي صرف/نقل/تسوية لازم يسيب quantity >= reserved (الشرط بيتفحص في الـ UPDATE نفسه). الحجز والإتمام وفكّ الحجز عمليات ذرّية بنفس الأسلوب.
"""
import time
from decimal import Decimal
from typing import Callable, Dict, Optional, Tuple, TypeVar

from sqlalchemy import delete, func, select, update
from sqlalchemy.dialects import mysql, sqlite
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from .. import models
from ..money import q3, qty_float

PL = models.ProductLocation
P = models.Product
T = TypeVar("T")

_MYSQL_RETRIABLE = {1205, 1213}  # lock wait timeout / deadlock


class InsufficientStock(Exception):
    """الرصيد مش كفاية. الـ main.py بيحوّلها لـ 400 برسالة عربي."""

    def __init__(self, product_name: str, location_code: str, available: float, requested: float, reserved: float = 0.0):
        self.product_name = product_name
        self.location_code = location_code
        self.available = available  # المتاح فعلاً (الفعلي - المحجوز)
        self.requested = requested
        self.reserved = reserved
        super().__init__(self.message)

    @property
    def message(self) -> str:
        extra = f" (منها {self.reserved:g} محجوزة لإذونات تانية)" if self.reserved else ""
        return (f"الكمية المتاحة من ({self.product_name}) في الموقع {self.location_code} "
                f"هي {self.available:g}{extra} وده أقل من المطلوب ({self.requested:g})")


class ReservationInvalid(Exception):
    """الحجز لم يعد صالحًا للتنفيذ (الموقع/الرصيد اتغير). الـ main.py بيحوّلها لـ 409."""

    def __init__(self, product_name: str, location_code: str, detail: str = ""):
        self.product_name = product_name
        self.location_code = location_code
        super().__init__(
            f"الحجز لـ ({product_name}) في الموقع {location_code} لم يعد صالحًا للتنفيذ{(' - ' + detail) if detail else ''}. افك الحجز وراجع الأرصدة."
        )


class InconsistentStock(Exception):
    """إجمالي المنتج مش متطابق مع مجموع الأماكن (بيانات قديمة تالفة). الـ main.py بيحوّلها لـ 409."""

    def __init__(self, product_name: str):
        self.product_name = product_name
        super().__init__(
            f"إجمالي كمية ({product_name}) مش متطابق مع مجموع أماكن التخزين. شغّل python -m app.cli check-stock وراجع المخزون."
        )


# ---------------------------------------------------------------- أدوات داخلية
def _dialect(db: Session) -> str:
    return db.get_bind().dialect.name


def _round_expr(expr):
    # ROUND مفيدة على SQLite (REAL) عشان نتفادى 0.30000000000000004؛ على MySQL DECIMAL مفيش تأثير
    return func.round(expr, 3)


def _names(db: Session, product_id: int, location_id: int) -> Tuple[str, str]:
    from ..location_utils import LocationIndex

    name = db.execute(select(P.name).where(P.id == product_id)).scalar() or f"#{product_id}"
    try:
        code = LocationIndex(db).full_code(location_id)
    except Exception:
        code = f"#{location_id}"
    return name, code


def location_balance(db: Session, product_id: int, location_id: int, lock: bool = True) -> Decimal:
    """رصيد المنتج في مكان (locking read افتراضيًا)."""
    stmt = select(PL.quantity).where(PL.product_id == product_id, PL.location_id == location_id)
    if lock:
        stmt = stmt.with_for_update()
    return q3(db.execute(stmt).scalar() or 0)


def location_balances(db: Session, product_id: int, location_id: int, lock: bool = True) -> Tuple[Decimal, Decimal]:
    """(on_hand, reserved) للمكان (locking read افتراضيًا)."""
    stmt = select(PL.quantity, PL.reserved).where(PL.product_id == product_id, PL.location_id == location_id)
    if lock:
        stmt = stmt.with_for_update()
    row = db.execute(stmt).first()
    return (q3(row[0]), q3(row[1])) if row else (Decimal(0), Decimal(0))


def product_total(db: Session, product_id: int, lock: bool = True) -> Decimal:
    stmt = select(P.quantity).where(P.id == product_id)
    if lock:
        stmt = stmt.with_for_update()
    return q3(db.execute(stmt).scalar() or 0)


def _bump_product_total(db: Session, product_id: int, delta: Decimal) -> None:
    """products.quantity += delta (بشرط ما يبقاش سالب)."""
    if delta == 0:
        return
    stmt = update(P).where(P.id == product_id)
    if delta < 0:
        stmt = stmt.where(P.quantity >= -delta)
    res = db.execute(stmt.values(quantity=_round_expr(P.quantity + delta)), execution_options={"synchronize_session": False})
    if res.rowcount != 1:
        name = db.execute(select(P.name).where(P.id == product_id)).scalar() or f"#{product_id}"
        raise InconsistentStock(name)


def _inc_location(db: Session, product_id: int, location_id: int, qty: Decimal) -> None:
    """upsert ذرّي: يزوّد رصيد المكان أو ينشئ الصف."""
    d = _dialect(db)
    row = {"product_id": product_id, "location_id": location_id, "quantity": qty}
    if d == "mysql":
        stmt = mysql.insert(PL).values(**row).on_duplicate_key_update(quantity=PL.__table__.c.quantity + qty)
    elif d == "sqlite":
        stmt = sqlite.insert(PL).values(**row).on_conflict_do_update(
            index_elements=["product_id", "location_id"], set_={"quantity": _round_expr(PL.__table__.c.quantity + qty)}
        )
    else:  # pragma: no cover
        raise RuntimeError(f"قاعدة بيانات غير مدعومة: {d}")
    db.execute(stmt)


def _dec_location(db: Session, product_id: int, location_id: int, qty: Decimal) -> None:
    """خصم ذرّي بشرط الكفاية **من المتاح** (quantity - reserved). بيرمي InsufficientStock لو المتاح أقل (حتى لو الفعلي يكفي)."""
    res = db.execute(
        update(PL)
        .where(PL.product_id == product_id, PL.location_id == location_id, PL.quantity - PL.reserved >= qty)
        .values(quantity=_round_expr(PL.quantity - qty)),
        execution_options={"synchronize_session": False},
    )
    if res.rowcount != 1:
        on_hand, reserved = location_balances(db, product_id, location_id)
        name, code = _names(db, product_id, location_id)
        raise InsufficientStock(name, code, float(on_hand - reserved), float(qty), float(reserved))
    _cleanup_empty(db, product_id, location_id)


def _cleanup_empty(db: Session, product_id: int, location_id: int) -> None:
    # الصف اللي وصل صفر بيتشال (باقي الكود بيفترض إن مفيش صفوف صفرية). CHECK reserved<=quantity بيضمن إن الصف الصفري مفيهوش حجز
    db.execute(delete(PL).where(PL.product_id == product_id, PL.location_id == location_id, PL.quantity <= 0),
               execution_options={"synchronize_session": False})


def _positive(qty) -> Decimal:
    d = q3(qty)
    if d <= 0:
        raise ValueError("الكمية لازم تكون أكبر من صفر")
    return d


# ---------------------------------------------------------------- فحص التوافر (بيجمّع كل النواقص مرة واحدة)
def shortages(db: Session, requested: Dict[Tuple[int, int], Decimal]) -> list:
    """
    requested = {(product_id, location_id): الكمية المطلوبة}. بيقرا الأرصدة بـ locking read (بترتيب ثابت) ويرجّع قايمة
    InsufficientStock لكل (منتج، مكان) المتاح فيه أقل من المطلوب - عشان رسالة الرفض تذكر **كل** الأصناف المتأثرة مش أول واحد بس.
    الفحص ده للرسالة؛ الأمان نفسه (ضد السباق) في شرط الـ UPDATE الذرّي جوه reserve_stock/remove_stock.
    """
    out = []
    for (product_id, location_id), qty in sorted(requested.items()):
        on_hand, reserved = location_balances(db, product_id, location_id)
        if on_hand - reserved < q3(qty):
            name, code = _names(db, product_id, location_id)
            out.append(InsufficientStock(name, code, float(on_hand - reserved), float(q3(qty)), float(reserved)))
    return out


def shortages_message(items: list) -> str:
    return "؛ ".join(e.message for e in items)


# ---------------------------------------------------------------- الحجز
def reserve_stock(db: Session, product_id: int, location_id: int, qty) -> None:
    """يحجز كمية في مكان (مش بينقص الفعلي). بيرمي InsufficientStock لو المتاح (الفعلي - المحجوز) أقل."""
    d = _positive(qty)
    res = db.execute(
        update(PL)
        .where(PL.product_id == product_id, PL.location_id == location_id, PL.quantity - PL.reserved >= d)
        .values(reserved=_round_expr(PL.reserved + d)),
        execution_options={"synchronize_session": False},
    )
    if res.rowcount != 1:
        on_hand, reserved = location_balances(db, product_id, location_id)
        name, code = _names(db, product_id, location_id)
        raise InsufficientStock(name, code, float(on_hand - reserved), float(d), float(reserved))


def release_reservation(db: Session, product_id: int, location_id: int, qty) -> None:
    """يفك حجز (فك/تعديل/إلغاء). لو الحجز مش موجود بالقيمة دي يبقى فيه تلف بيانات: ReservationInvalid."""
    d = _positive(qty)
    res = db.execute(
        update(PL).where(PL.product_id == product_id, PL.location_id == location_id, PL.reserved >= d)
        .values(reserved=_round_expr(PL.reserved - d)),
        execution_options={"synchronize_session": False},
    )
    if res.rowcount != 1:
        name, code = _names(db, product_id, location_id)
        raise ReservationInvalid(name, code, "المحجوز في الموقع أقل من المسجّل في الإذن")


def fulfill_reservation(db: Session, product_id: int, location_id: int, qty) -> None:
    """إتمام الحجز: بينقص الفعلي وينهي الحجز في نفس الـ UPDATE (ذرّي)."""
    d = _positive(qty)
    res = db.execute(
        update(PL)
        .where(PL.product_id == product_id, PL.location_id == location_id, PL.reserved >= d, PL.quantity >= d)
        .values(quantity=_round_expr(PL.quantity - d), reserved=_round_expr(PL.reserved - d)),
        execution_options={"synchronize_session": False},
    )
    if res.rowcount != 1:
        name, code = _names(db, product_id, location_id)
        raise ReservationInvalid(name, code, "الرصيد الفعلي أو المحجوز في الموقع اتغيّر")
    _cleanup_empty(db, product_id, location_id)
    _bump_product_total(db, product_id, -d)


# ---------------------------------------------------------------- العمليات العامة
def add_stock(db: Session, product_id: int, location_id: int, qty) -> None:
    """وارد/إرجاع: يزوّد المكان وإجمالي المنتج."""
    d = _positive(qty)
    _inc_location(db, product_id, location_id, d)
    _bump_product_total(db, product_id, d)


def remove_stock(db: Session, product_id: int, location_id: int, qty) -> None:
    """صرف: يخصم من المكان وإجمالي المنتج. بيرمي InsufficientStock."""
    d = _positive(qty)
    _dec_location(db, product_id, location_id, d)
    _bump_product_total(db, product_id, -d)


def transfer_stock(db: Session, product_id: int, from_location_id: int, to_location_id: int, qty) -> None:
    """نقل بين مكانين (الإجمالي ما بيتغيرش)."""
    if from_location_id == to_location_id:
        raise ValueError("مكان النقل منه ومكان النقل إليه لازم يكونوا مختلفين")
    d = _positive(qty)
    _dec_location(db, product_id, from_location_id, d)
    _inc_location(db, product_id, to_location_id, d)


def adjust_product_total_only(db: Session, product_id: int, delta) -> None:
    """للأسطر القديمة اللي من غير مكان (legacy) - بتعدّل الإجمالي بس."""
    _bump_product_total(db, product_id, q3(delta))


class BelowReserved(Exception):
    """التسوية هتنزّل الرصيد تحت المحجوز. الـ main.py بيحوّلها لـ 409."""

    def __init__(self, product_name: str, location_code: str, target: float, reserved: float):
        self.product_name, self.location_code, self.target, self.reserved = product_name, location_code, target, reserved
        super().__init__(
            f"الكمية المطلوبة ({target:g}) لـ ({product_name}) في الموقع {location_code} أقل من المحجوز لإذونات صرف ({reserved:g}). فك الحجوزات الأول."
        )


def set_location_quantity(db: Session, product_id: int, location_id: int, new_qty) -> Tuple[Decimal, Decimal]:
    """
    تصحيح/جرد: يضبط رصيد المكان على قيمة محددة (0 = يمسح الصف). بيرجّع (القديم، الجديد) للمكان.
    الإجمالي بيتحدّث بالفرق (delta) بشكل نسبي - فالحركات اللي حصلت بالتوازي مبتضيعش.
    مينفعش القيمة الجديدة تقل عن المحجوز في المكان (BelowReserved).
    """
    target = q3(new_qty)
    if target < 0:
        raise ValueError("الكمية لازم تكون موجبة")
    old, reserved = location_balances(db, product_id, location_id)
    if target == old:
        return old, target
    if target < reserved:
        name, code = _names(db, product_id, location_id)
        raise BelowReserved(name, code, float(target), float(reserved))
    if target > 0:
        res = db.execute(
            update(PL).where(PL.product_id == product_id, PL.location_id == location_id).values(quantity=target),
            execution_options={"synchronize_session": False},
        )
        if res.rowcount != 1:
            _inc_location(db, product_id, location_id, target)
    else:
        db.execute(delete(PL).where(PL.product_id == product_id, PL.location_id == location_id),
                   execution_options={"synchronize_session": False})
    _bump_product_total(db, product_id, target - old)
    return old, target


def replace_locations(db: Session, product_id: int, wanted: Dict[int, Decimal]) -> Tuple[Decimal, Decimal]:
    """
    بيستبدل توزيع المنتج على الأماكن بالقايمة (أي مكان مش فيها بيتصفّر). بيرجّع (الإجمالي القديم، الجديد).
    """
    old_total = product_total(db, product_id)
    current = {
        lid: q3(qty)
        for lid, qty in db.execute(select(PL.location_id, PL.quantity).where(PL.product_id == product_id).with_for_update()).all()
    }
    for lid in sorted(set(current) | set(wanted)):
        set_location_quantity(db, product_id, lid, wanted.get(lid, Decimal(0)))
    return old_total, product_total(db, product_id)


# ---------------------------------------------------------------- retry على deadlock
def is_retriable(exc: BaseException) -> bool:
    if not isinstance(exc, OperationalError):
        return False
    orig = getattr(exc, "orig", None)
    code = orig.args[0] if getattr(orig, "args", None) and isinstance(orig.args[0], int) else None
    if code in _MYSQL_RETRIABLE:
        return True
    return "database is locked" in str(orig).lower()  # SQLite تحت ضغط كتابة متزامنة


def with_retry(db: Session, fn: Callable[[], T], attempts: int = 3, base_delay: float = 0.05) -> T:
    """
    بينفّذ الـ transaction كلها (fn) ولو MySQL رجّعت deadlock/lock-timeout بيعمل rollback ويعيد (max attempts).
    fn لازم تكون idempotent قبل الـ commit (أي كل شغلها جوه الـ transaction).
    """
    for attempt in range(1, attempts + 1):
        try:
            return fn()
        except OperationalError as exc:
            db.rollback()
            if attempt == attempts or not is_retriable(exc):
                raise
            time.sleep(base_delay * (2 ** (attempt - 1)))
    raise RuntimeError("unreachable")  # pragma: no cover

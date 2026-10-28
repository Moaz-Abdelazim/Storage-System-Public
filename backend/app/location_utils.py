"""
أدوات نظام المواقع الديناميكي (Warehouse -> Location -> Location -> ...).

الفكرة: الـ Full Location Code (زي W1-F3-R5-S2) **مش متخزن** في الداتا بيز - بيتبني
في كل مرة من كود المخزن + أكواد الـ parents، عن طريق LocationIndex اللي بيحمّل
كل المخازن والمواقع مرة واحدة في الذاكرة (عددهم صغير عادةً) وبعدين كل العمليات
(بناء الكود، البحث بالكود، كل الـ descendants، ...) بتتعمل من غير استعلامات إضافية.
"""
import re
from datetime import datetime
from typing import Dict, List, Optional, Set

from fastapi import HTTPException
from sqlalchemy import or_
from sqlalchemy.orm import Session

from . import models
from .display_utils import format_user_display

SEP = "-"  # فاصل المستويات في الكود الكامل - عشان كده الكود الواحد ما ينفعش يحتوي عليه
_CODE_RE = re.compile(r"^[^\s\-/\\]+$")


def normalize_code(raw: Optional[str], label: str = "الكود") -> str:
    """بيتحقق من كود مستوى واحد (مش الكود الكامل) وبيرجعه Upper-case."""
    code = (raw or "").strip().upper()
    if not code:
        raise HTTPException(status_code=400, detail=f"اكتب {label}")
    if len(code) > 30:
        raise HTTPException(status_code=400, detail=f"{label} طويل جدًا (أقصى حاجة 30 حرف)")
    if not _CODE_RE.match(code):
        raise HTTPException(
            status_code=400,
            detail=f"{label} ما ينفعش يحتوي على مسافات أو على علامة (-) أو (/) لأن (-) هي الفاصل بين مستويات الكود الكامل",
        )
    return code


def split_full_code(raw: Optional[str]) -> List[str]:
    text = (raw or "").strip().upper()
    for dash in ("–", "—", "−", "‑"):  # شرطات مختلفة ممكن تيجي من Excel/نسخ ولصق
        text = text.replace(dash, SEP)
    return [p.strip() for p in text.split(SEP)]


def _natural_key(code: str):
    return [int(t) if t.isdigit() else t for t in re.split(r"(\d+)", code or "")]


class LocationIndex:
    def __init__(self, db: Session):
        self.warehouses: Dict[int, models.Warehouse] = {w.id: w for w in db.query(models.Warehouse).all()}
        self.locations: Dict[int, models.Location] = {l.id: l for l in db.query(models.Location).all()}
        self._wh_by_code = {w.code.upper(): w for w in self.warehouses.values()}
        self._children: Dict[tuple, List[models.Location]] = {}
        for loc in self.locations.values():
            self._children.setdefault((loc.warehouse_id, loc.parent_id), []).append(loc)
        for lst in self._children.values():
            lst.sort(key=lambda l: _natural_key(l.code))
        self._code_cache: Dict[int, str] = {}

    # ---------- بنية الشجرة ----------
    def warehouses_sorted(self) -> List[models.Warehouse]:
        return sorted(self.warehouses.values(), key=lambda w: _natural_key(w.code))

    def children_of(self, warehouse_id: int, parent_id: Optional[int] = None) -> List[models.Location]:
        return self._children.get((warehouse_id, parent_id), [])

    def is_leaf(self, location_id: int) -> bool:
        loc = self.locations[location_id]
        return not self.children_of(loc.warehouse_id, loc.id)

    def chain(self, location_id: int) -> List[models.Location]:
        """المواقع من الجذر لحد الموقع نفسه (من غير المخزن)."""
        out, seen = [], set()
        loc = self.locations.get(location_id)
        while loc is not None and loc.id not in seen:
            seen.add(loc.id)
            out.append(loc)
            loc = self.locations.get(loc.parent_id) if loc.parent_id else None
        out.reverse()
        return out

    # ---------- الأكواد ----------
    def full_code(self, location_id: int) -> str:
        if location_id not in self._code_cache:
            chain = self.chain(location_id)
            wh = self.warehouses.get(chain[0].warehouse_id) if chain else None
            parts = ([wh.code] if wh else []) + [l.code for l in chain]
            self._code_cache[location_id] = SEP.join(parts)
        return self._code_cache[location_id]

    def path(self, location_id: Optional[int], warehouse_id: Optional[int] = None) -> List[dict]:
        """كل أجزاء المكان بالترتيب (المخزن ثم كل مستوى) مع اسم كل جزء - لصفحة البحث عن Location."""
        chain = self.chain(location_id) if location_id else []
        wh = self.warehouses.get(chain[0].warehouse_id if chain else warehouse_id)
        out, codes = [], []
        if wh:
            codes.append(wh.code)
            out.append({"level": 1, "kind": "warehouse", "id": wh.id, "code": wh.code, "name": wh.name,
                        "type": None, "full_code": SEP.join(codes)})
        for i, loc in enumerate(chain):
            codes.append(loc.code)
            out.append({"level": i + 2, "kind": "location", "id": loc.id, "code": loc.code, "name": loc.name,
                        "type": loc.type, "full_code": SEP.join(codes)})
        return out

    def resolve(self, full_code: str) -> dict:
        """
        بيحوّل Full Location Code لـ (warehouse, location). لو فيه جزء مش موجود بيرجع error
        بيقول بالظبط أنهي جزء فشل (عشان رسالة الخطأ تبقى واضحة للمستخدم).
        """
        parts = split_full_code(full_code)
        if not parts or any(not p for p in parts):
            return {"error": "كود الموقع غير صالح - الشكل الصحيح مثلاً: W1-F3-R5"}
        wh = self._wh_by_code.get(parts[0])
        if wh is None:
            return {"error": f"مفيش مخزن بكود ({parts[0]})"}
        parent_id, current, walked = None, None, [wh.code]
        for code in parts[1:]:
            match = next((l for l in self.children_of(wh.id, parent_id) if l.code.upper() == code), None)
            if match is None:
                return {"error": f"الكود ({code}) مش موجود تحت ({SEP.join(walked)})"}
            current, parent_id = match, match.id
            walked.append(match.code)
        return {"warehouse": wh, "location": current, "error": None}

    # ---------- مكان الاستلام الافتراضي ----------
    def default_location(self, warehouse_id: int) -> Optional[models.Location]:
        """مكان الاستلام الافتراضي للمخزن (لو متحدد وسليم - بيتحقق إنه لسه موجود وفي نفس المخزن)."""
        wh = self.warehouses.get(warehouse_id)
        if wh is None or not wh.default_location_id:
            return None
        loc = self.locations.get(wh.default_location_id)
        return loc if loc is not None and loc.warehouse_id == warehouse_id else None

    def default_location_ids(self) -> Set[int]:
        return {l.id for w in self.warehouses.values() for l in [self.default_location(w.id)] if l is not None}

    def warehouse_dict(self, wh: models.Warehouse) -> dict:
        d = self.default_location(wh.id)
        return {
            "id": wh.id, "code": wh.code, "name": wh.name,
            "default_location_id": d.id if d else None,
            "default_location_code": self.full_code(d.id) if d else None,
        }

    # ---------- نطاقات الفلترة ----------
    def descendants(self, location_id: int) -> Set[int]:
        """الموقع نفسه + كل المواقع اللي تحته بأي عمق."""
        result, stack = set(), [location_id]
        while stack:
            current = stack.pop()
            if current in result or current not in self.locations:
                continue
            result.add(current)
            loc = self.locations[current]
            stack.extend(c.id for c in self.children_of(loc.warehouse_id, loc.id))
        return result

    def warehouse_location_ids(self, warehouse_id: int) -> Set[int]:
        return {l.id for l in self.locations.values() if l.warehouse_id == warehouse_id}

    def scope_ids(self, warehouse_id: Optional[int], location_id: Optional[int]) -> Optional[Set[int]]:
        """None = من غير فلتر. location_id أقوى من warehouse_id."""
        if location_id:
            if location_id not in self.locations:
                raise HTTPException(status_code=404, detail="الموقع المختار مش موجود")
            return self.descendants(location_id)
        if warehouse_id:
            if warehouse_id not in self.warehouses:
                raise HTTPException(status_code=404, detail="المخزن المختار مش موجود")
            return self.warehouse_location_ids(warehouse_id)
        return None


def require_leaf_location(idx: LocationIndex, location_id: int) -> models.Location:
    """الكميات بتتخزن في المواقع النهائية بس (اللي ملهاش مواقع تحتها)."""
    loc = idx.locations.get(location_id)
    if loc is None:
        raise HTTPException(status_code=404, detail="الموقع المختار مش موجود")
    if not idx.is_leaf(location_id):
        raise HTTPException(
            status_code=400,
            detail=f"الموقع ({idx.full_code(location_id)}) فيه مستويات تحته - اختار الموقع النهائي اللي هتتخزن فيه البضاعة",
        )
    return loc


def lock_products(db: Session, product_ids) -> dict:
    """
    بيقفل صفوف المنتجات (SELECT ... FOR UPDATE) بترتيب ثابت (id) قبل أي تعديل على كمية/أماكن المنتج، عشان
    عمليتين في نفس اللحظة من جهازين (وارد/صادر/نقل/تصحيح) ما يتداخلوش ويبوّظوا الكميات (ولا يحصل deadlock).
    SQLite بتتجاهل الـ FOR UPDATE (الكتابة فيها بتتسلسل أصلاً)، وMySQL/InnoDB بتطبّقه فعلاً.
    """
    ids = sorted({int(i) for i in product_ids})
    if not ids:
        return {}
    rows = db.query(models.Product).filter(models.Product.id.in_(ids)).order_by(models.Product.id).with_for_update().all()
    return {p.id: p for p in rows}


def resolve_receiving_location(idx: LocationIndex, warehouse_id: Optional[int], location_id: Optional[int]) -> models.Location:
    """
    تحديد المكان اللي صنف الوارد هيدخله: مكان محدد (لازم يكون نهائي) أو مكان الاستلام الافتراضي للمخزن المختار.
    الصنف لازم يدخل مكان - مفيش بضاعة من غير مكان.
    """
    if location_id:
        return require_leaf_location(idx, location_id)
    if not warehouse_id:
        raise HTTPException(status_code=400, detail="حدد المخزن (أو المكان) اللي هيدخله كل صنف في فاتورة الوارد")
    wh = idx.warehouses.get(warehouse_id)
    if wh is None:
        raise HTTPException(status_code=404, detail="المخزن المختار مش موجود")
    default = idx.default_location(warehouse_id)
    if default is None:
        raise HTTPException(
            status_code=400,
            detail=f"المخزن ({wh.code}) ملوش مكان استلام افتراضي. اختار مكان محدد للصنف، أو حدد مكان الاستلام الافتراضي من صفحة إدارة المواقع.",
        )
    return require_leaf_location(idx, default.id)


def validate_location_rows(idx: LocationIndex, rows) -> List[tuple]:
    """rows: قايمة فيها location_id و quantity. بترجع [(location_id, qty)] بعد التحقق (الكميات الصفرية بتتشال)."""
    seen, out = set(), []
    for r in rows:
        if r.quantity is None or r.quantity < 0:
            raise HTTPException(status_code=400, detail="كمية الموقع لازم تكون رقم موجب")
        require_leaf_location(idx, r.location_id)
        if r.location_id in seen:
            raise HTTPException(status_code=400, detail=f"الموقع ({idx.full_code(r.location_id)}) متكرر - اجمع الكميات في سطر واحد")
        seen.add(r.location_id)
        qty = round(r.quantity, 3)
        if qty > 0:
            out.append((r.location_id, qty))
    return out


# ---------- حركات النقل ----------
def transfer_to_dict(t: models.LocationTransfer, idx: LocationIndex) -> dict:
    return {
        "id": t.id,
        "date": t.date,
        "product_id": t.product_id,
        "product_name": t.product.name if t.product else None,
        "sku": t.product.sku if t.product else None,
        "unit": t.product.unit if t.product else None,
        "quantity": t.quantity,
        "from_location_id": t.from_location_id,
        "from_code": idx.full_code(t.from_location_id),
        "to_location_id": t.to_location_id,
        "to_code": idx.full_code(t.to_location_id),
        "moved_by": format_user_display(t.moved_by, t.moved_by_username),
        "notes": t.notes,
    }


def query_transfers(
    db: Session,
    idx: LocationIndex,
    *,
    product_id: Optional[int] = None,
    search: Optional[str] = None,
    scope_ids: Optional[Set[int]] = None,
    party: Optional[str] = None,  # بحث في كود "من" / "إلى" / اسم المنتج
    user: Optional[str] = None,
    date_from: Optional[datetime] = None,
    date_to_exclusive: Optional[datetime] = None,
) -> List[dict]:
    q = db.query(models.LocationTransfer)
    if product_id:
        q = q.filter(models.LocationTransfer.product_id == product_id)
    if date_from:
        q = q.filter(models.LocationTransfer.date >= date_from)
    if date_to_exclusive:
        q = q.filter(models.LocationTransfer.date < date_to_exclusive)
    if scope_ids is not None:
        q = q.filter(or_(
            models.LocationTransfer.from_location_id.in_(scope_ids),
            models.LocationTransfer.to_location_id.in_(scope_ids),
        ))
    if search:
        like = f"%{search.strip()}%"
        q = q.join(models.Product, models.Product.id == models.LocationTransfer.product_id).filter(
            or_(models.Product.name.like(like), models.Product.sku.like(like))
        )
    rows = [transfer_to_dict(t, idx) for t in q.order_by(models.LocationTransfer.date.desc(), models.LocationTransfer.id.desc()).all()]
    if party:
        p = party.strip().lower()
        rows = [r for r in rows if p in (r["from_code"] or "").lower() or p in (r["to_code"] or "").lower() or p in (r["product_name"] or "").lower()]
    if user:
        u = user.strip().lower()
        rows = [r for r in rows if r["moved_by"] and u in r["moved_by"].lower()]
    return rows

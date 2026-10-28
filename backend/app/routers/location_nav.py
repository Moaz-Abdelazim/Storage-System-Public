"""
تصفّح المواقع بدون تحميل الشجرة كلها (للمخازن الكبيرة):
  GET /api/locations/nav/roots      المخازن (مستوى أول) بإحصائياتها
  GET /api/locations/nav/children   أبناء مخزن/موقع (بترقيم) - بيتفتحوا لما المستخدم يوسّع الفرع
  GET /api/locations/nav/search     بحث سريع في كل المواقع (للقوايم المنسدلة) بحد أقصى للنتايج
  GET /api/locations/nav/describe   وصف تفصيلي لكود موقع (بيتعرض في الـ hover على الكود)

الأداء: بنحمّل جدول المواقع الخفيف بس (id/parent/code/name) عشان نبني الأكواد والمسارات، والكميات بتتحسب بـ SQL مجمّع
للنتايج المطلوبة فقط (مش للشجرة كلها)، والنتايج دايمًا محدودة أو بترقيم.
"""
import math
import re
from collections import defaultdict
from typing import Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import exists, func
from sqlalchemy.orm import Session, aliased

from .. import models, auth
from ..database import get_db
from ..query_helpers import page_args, paged

router = APIRouter(prefix="/api/locations/nav", tags=["Locations Navigation"])

_AR = str.maketrans({"أ": "ا", "إ": "ا", "آ": "ا", "ى": "ي", "ة": "ه"})


def _norm(s: str) -> str:
    return re.sub(r"[\u064B-\u0652]", "", str(s or "")).lower().translate(_AR)


class _Index:
    """فهرس خفيف في الذاكرة للمواقع (أعمدة قليلة بس)."""

    def __init__(self, db: Session):
        W, L = models.Warehouse, models.Location
        self.wh = {w.id: w for w in db.query(W.id, W.code, W.name, W.default_location_id).all()}
        self.loc = {l.id: l for l in db.query(L.id, L.warehouse_id, L.parent_id, L.code, L.name, L.type).all()}
        self.kids: Dict[Optional[int], List[int]] = defaultdict(list)       # parent_id -> children
        self.root_kids: Dict[int, List[int]] = defaultdict(list)            # warehouse_id -> top-level
        for l in self.loc.values():
            (self.kids[l.parent_id] if l.parent_id else self.root_kids[l.warehouse_id]).append(l.id)
        key = lambda i: (_norm(self.loc[i].code), self.loc[i].id)
        for v in list(self.kids.values()) + list(self.root_kids.values()):
            v.sort(key=key)
        self.default_ids = {w.default_location_id for w in self.wh.values() if w.default_location_id}

    def chain(self, loc_id: int) -> list:
        out, cur, guard = [], self.loc.get(loc_id), 0
        while cur is not None and guard < 100:
            out.append(cur)
            cur = self.loc.get(cur.parent_id) if cur.parent_id else None
            guard += 1
        return out[::-1]

    def full_code(self, loc_id: int) -> str:
        ch = self.chain(loc_id)
        if not ch:
            return str(loc_id)
        return "-".join([self.wh[ch[0].warehouse_id].code] + [c.code for c in ch])

    def descendants(self, loc_id: int) -> List[int]:
        out, stack = [], [loc_id]
        while stack:
            cur = stack.pop()
            out.append(cur)
            stack.extend(self.kids.get(cur, []))
        return out

    def warehouse_locations(self, wh_id: int) -> List[int]:
        return [l.id for l in self.loc.values() if l.warehouse_id == wh_id]


def _stats(db: Session, groups: Dict[str, List[int]]) -> Dict[str, tuple]:
    """{key: (كمية, عدد الأصناف المختلفة)} لمجموعات مواقع - SQL مجمّع على دفعات."""
    PL = models.ProductLocation
    out = {}
    for key, ids in groups.items():
        qty, prods = 0.0, set()
        for i in range(0, len(ids), 900):
            chunk = ids[i:i + 900]
            qty += float(db.query(func.coalesce(func.sum(PL.quantity), 0)).filter(PL.quantity > 0, PL.location_id.in_(chunk)).scalar() or 0)
            prods.update(p for (p,) in db.query(PL.product_id).filter(PL.quantity > 0, PL.location_id.in_(chunk)).distinct().all())
        out[key] = (round(qty, 3), len(prods))
    return out


def _loc_node(idx: _Index, l, stats: Optional[tuple] = None) -> dict:
    return {
        "kind": "location", "id": l.id, "warehouse_id": l.warehouse_id, "parent_id": l.parent_id, "code": l.code, "name": l.name,
        "type": l.type, "full_code": idx.full_code(l.id), "is_leaf": not idx.kids.get(l.id), "children_count": len(idx.kids.get(l.id, [])),
        "quantity": stats[0] if stats else None, "products_count": stats[1] if stats else None,
        "is_default_receiving": l.id in idx.default_ids,
    }


def _wh_node(idx: _Index, w, stats: Optional[tuple] = None) -> dict:
    dl = idx.loc.get(w.default_location_id) if w.default_location_id else None
    n_kids = len(idx.root_kids.get(w.id, []))
    return {
        "kind": "warehouse", "id": w.id, "warehouse_id": w.id, "parent_id": None, "code": w.code, "name": w.name, "type": None,
        "full_code": w.code, "is_leaf": n_kids == 0, "children_count": n_kids,
        "quantity": stats[0] if stats else None, "products_count": stats[1] if stats else None,
        "default_location_id": w.default_location_id if dl else None,
        "default_location_code": idx.full_code(dl.id) if dl else None,
    }


@router.get("/summary")
def summary(db: Session = Depends(get_db), current_user: models.User = Depends(auth.get_current_user)):
    """أرقام عامة للكروت (عدد المخازن والمواقع والأماكن النهائية وإجمالي الكمية) بـ SQL مجمّع."""
    L, PL = models.Location, models.ProductLocation
    child = aliased(L)
    return {
        "warehouses": db.query(func.count(models.Warehouse.id)).scalar() or 0,
        "locations": db.query(func.count(L.id)).scalar() or 0,
        "leaves": db.query(func.count(L.id)).filter(~exists().where(child.parent_id == L.id)).scalar() or 0,
        "quantity": round(float(db.query(func.coalesce(func.sum(PL.quantity), 0)).filter(PL.quantity > 0).scalar() or 0), 3),
    }


@router.get("/roots")
def roots(db: Session = Depends(get_db), current_user: models.User = Depends(auth.get_current_user)):
    """المخازن (المستوى الأول) بإجمالي الكمية وعدد الأصناف. عدد المخازن صغير عادة."""
    idx = _Index(db)
    whs = sorted(idx.wh.values(), key=lambda w: (_norm(w.code), w.id))
    stats = _stats(db, {str(w.id): idx.warehouse_locations(w.id) for w in whs})
    return [_wh_node(idx, w, stats.get(str(w.id))) for w in whs]


@router.get("/children")
def children(
    warehouse_id: Optional[int] = None,
    parent_id: Optional[int] = None,
    page: int = 1,
    page_size: int = 50,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(auth.get_current_user),
):
    """أبناء مخزن (لو parent_id فاضي) أو موقع - بترقيم، والإحصائيات للصفحة دي بس."""
    idx = _Index(db)
    if parent_id:
        if parent_id not in idx.loc:
            raise HTTPException(status_code=404, detail="الموقع مش موجود")
        ids = idx.kids.get(parent_id, [])
    else:
        if not warehouse_id or warehouse_id not in idx.wh:
            raise HTTPException(status_code=404, detail="المخزن مش موجود")
        ids = idx.root_kids.get(warehouse_id, [])
    pg, size = page_args(page, page_size, default=50, max_value=200)
    page_ids = ids[(pg - 1) * size: pg * size]
    stats = _stats(db, {str(i): idx.descendants(i) for i in page_ids})
    return paged([_loc_node(idx, idx.loc[i], stats.get(str(i))) for i in page_ids], len(ids), pg, size)


@router.get("/search")
def search(
    q: str = "",
    leaf_only: bool = False,
    include_warehouses: bool = False,
    mode: Optional[str] = None,  # receiving: مخازن ليها مكان استلام افتراضي + الأماكن النهائية
    warehouse_id: Optional[int] = None,
    limit: int = 30,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(auth.get_current_user),
):
    """بحث في كل المواقع بالاسم أو الكود أو الكود الكامل (حد أقصى للنتايج) - للقوايم المنسدلة."""
    idx = _Index(db)
    limit = max(1, min(limit, 100))
    needle = _norm(q.strip())
    receiving = mode == "receiving"
    results = []

    if (include_warehouses or receiving):
        for w in sorted(idx.wh.values(), key=lambda w: _norm(w.code)):
            if warehouse_id and w.id != warehouse_id:
                continue
            if receiving and not (w.default_location_id and w.default_location_id in idx.loc):
                continue
            if needle and needle not in _norm(f"{w.code} {w.name}"):
                continue
            results.append(("w", w.code, _wh_node(idx, w), [w.name]))

    leaf_flag = leaf_only or receiving
    for l in idx.loc.values():
        if warehouse_id and l.warehouse_id != warehouse_id:
            continue
        if leaf_flag and idx.kids.get(l.id):
            continue
        ch = idx.chain(l.id)
        fc = "-".join([idx.wh[ch[0].warehouse_id].code] + [c.code for c in ch])
        names = [idx.wh[ch[0].warehouse_id].name] + [c.name for c in ch]
        if needle and needle not in _norm(f"{fc} {' '.join(names)}"):
            continue
        node = _loc_node(idx, l)
        node["full_code"] = fc
        results.append(("l", fc, node, names))

    results.sort(key=lambda r: (_norm(r[1]), r[0]))
    total = len(results)
    results = results[:limit]
    # كميات الأماكن النهائية المرجّعة (بس)
    leaf_ids = [r[2]["id"] for r in results if r[0] == "l" and r[2]["is_leaf"]]
    qty = {}
    if leaf_ids:
        PL = models.ProductLocation
        qty = dict(db.query(PL.location_id, func.sum(PL.quantity)).filter(PL.quantity > 0, PL.location_id.in_(leaf_ids)).group_by(PL.location_id).all())
    items = []
    for kind, _fc, node, names in results:
        node["path"] = names
        if kind == "l" and node["is_leaf"]:
            node["quantity"] = round(float(qty.get(node["id"], 0) or 0), 3)
        items.append(node)
    return {"items": items, "total": total, "limit": limit, "truncated": total > limit}


@router.get("/describe")
def describe(code: str, db: Session = Depends(get_db), current_user: models.User = Depends(auth.get_current_user)):
    """وصف تفصيلي لكود موقع (W1-F3-R5): المخزن ثم كل مستوى باسمه ونوعه. بيتعرض في الـ hover على الكود."""
    idx = _Index(db)
    code = (code or "").strip().upper()
    if not code:
        raise HTTPException(status_code=400, detail="اكتب كود الموقع")
    wh = next((w for w in idx.wh.values() if w.code.upper() == code), None)
    if wh:
        n = _wh_node(idx, wh)
        return {"full_code": wh.code, "kind": "warehouse", "is_leaf": n["is_leaf"], "path": [
            {"kind": "warehouse", "code": wh.code, "name": wh.name, "type": "مخزن", "full_code": wh.code}]}
    for l in idx.loc.values():
        if idx.full_code(l.id).upper() == code:
            ch = idx.chain(l.id)
            w = idx.wh[ch[0].warehouse_id]
            path = [{"kind": "warehouse", "code": w.code, "name": w.name, "type": "مخزن", "full_code": w.code}]
            acc = [w.code]
            for c in ch:
                acc.append(c.code)
                path.append({"kind": "location", "code": c.code, "name": c.name, "type": c.type, "full_code": "-".join(acc)})
            return {"full_code": "-".join(acc), "kind": "location", "is_leaf": not idx.kids.get(l.id),
                    "is_default_receiving": l.id in idx.default_ids, "path": path}
    raise HTTPException(status_code=404, detail="الموقع مش موجود")

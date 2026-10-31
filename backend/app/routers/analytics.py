"""
تقارير تحليلية متقدمة - للأدمن فقط (الحماية على الـ router كله).

الأداء: كل تقرير جدولي بيتحسب بـ SQL مجمّع (GROUP BY / subqueries) والترقيم والبحث والترتيب بيحصلوا في قاعدة البيانات،
والرد بيحتوي على صفحة واحدة بس (items + total + page + page_size + total_pages) + ملخص (summary) محسوب على الكل.
مفيش أي تقرير بيحمّل كل الصفوف في الذاكرة ويقصّها بعد كده. تحليل ABC بيستخدم window functions في SQL (SUM() OVER)، ولو الداتا بيز
مش بتدعمها (مثلًا MySQL 5.7) بيرجع تلقائيًا للحساب في Python (صف واحد لكل صنف مباع).
الباراميترات المشتركة: page / page_size (الافتراضي 15، الحد 100) / search / sort / export=1 (حتى 5000 صف للتصدير CSV).

ملاحظة عن الأسعار: سعر الوحدة في فواتير الوارد بيتعامل معاه كتكلفة شراء، وفي فواتير الصادر كسعر بيع.
لو سعر السطر فاضي بنرجع لسعر المنتج نفسه. تقرير الربحية "تقديري" ومعتمد على إن الأسعار بتتسجّل في الفواتير.
"""
import math
from collections import defaultdict
from datetime import datetime, timedelta
from typing import Dict, List, Optional

from fastapi import APIRouter, Depends
from sqlalchemy import Integer, and_, case, cast, distinct, exists, func, literal, literal_column, select, union_all
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from .. import models, auth
from ..database import get_db
from ..query_helpers import contains, page_args, paged, slice_page
from ..timezone_utils import now_cairo

router = APIRouter(
    prefix="/api/analytics",
    tags=["Analytics"],
    dependencies=[Depends(auth.require_permission("reports.view", "finance.view"))],
)

P = models.Product
InInv, InItem = models.StockInInvoice, models.StockInItem
OutInv, OutItem = models.StockOutInvoice, models.StockOutItem


def _period(days: Optional[int], default: int = 90, lo: int = 7, hi: int = 730) -> int:
    return max(lo, min(days or default, hi))


def _since(days: int) -> datetime:
    return now_cairo() - timedelta(days=days)


def _f(v, nd: int = 2) -> float:
    return round(float(v or 0), nd)


def _out_price():
    """سعر البيع المعتمد للسطر: سعر السطر أو سعر المنتج."""
    return func.coalesce(OutItem.unit_price, P.price, 0)


def _in_price():
    return func.coalesce(InItem.unit_price, P.price, 0)


def _search_products(q, search: Optional[str]):
    if search and search.strip():
        q = q.filter(contains(P.name, search) | contains(P.sku, search))
    return q


# =====================================================================
#  الأصناف الراكدة (SQL): فيها رصيد ومفيش عليها صادر في الفترة
# =====================================================================
def _dead_base(db: Session, days: int):
    since = _since(days)
    recent_out = exists().where(and_(OutItem.product_id == P.id, OutItem.invoice_id == OutInv.id, OutInv.date >= since))
    return db.query(P).filter(P.is_active.is_(True), P.quantity > 0, ~recent_out)


def _dead_summary(db: Session, days: int) -> dict:
    base = _dead_base(db, days)
    row = base.with_entities(
        func.count(P.id), func.coalesce(func.sum(P.quantity * P.price), 0), func.coalesce(func.sum(P.quantity), 0)
    ).first()
    ever_out = exists().where(OutItem.product_id == P.id)
    never = base.filter(~ever_out).count()
    return {"count": row[0] or 0, "total_value": _f(row[1]), "total_quantity": _f(row[2], 3), "never_sold": never}


@router.get("/dead-stock")
def dead_stock(
    days: int = 90, page: int = 1, page_size: int = 15, search: Optional[str] = None, export: bool = False,
    db: Session = Depends(get_db),
):
    days = _period(days)
    page, page_size = page_args(page, page_size, export=export)
    summary = _dead_summary(db, days)
    q = _search_products(_dead_base(db, days), search)
    total = q.count()
    prods = q.order_by((P.quantity * P.price).desc(), P.id).offset((page - 1) * page_size).limit(page_size).all()
    last_out = {}
    if prods:
        last_out = dict(
            db.query(OutItem.product_id, func.max(OutInv.date))
            .select_from(OutItem).join(OutInv, OutInv.id == OutItem.invoice_id)
            .filter(OutItem.product_id.in_([p.id for p in prods])).group_by(OutItem.product_id).all()
        )
    now = now_cairo()
    items = []
    for p in prods:
        last = last_out.get(p.id)
        items.append({
            "product_id": p.id, "name": p.name, "sku": p.sku, "quantity": _f(p.quantity, 3), "unit": p.unit,
            "price": _f(p.price), "value": _f((p.quantity or 0) * (p.price or 0)),
            "last_out_date": last, "days_since_last_out": (now - last).days if last else None,
        })
    return paged(items, total, page, page_size, days=days, summary=summary)


# =====================================================================
#  مؤشرات عامة (KPIs)
# =====================================================================
@router.get("/kpis")
def kpis(days: int = 90, db: Session = Depends(get_db)):
    days = _period(days)
    since = _since(days)

    stock_value = _f(db.query(func.coalesce(func.sum(P.price * P.quantity), 0)).filter(P.is_active.is_(True)).scalar())
    out_value = _f(
        db.query(func.coalesce(func.sum(OutItem.quantity * _out_price()), 0))
        .select_from(OutItem).join(OutInv, OutInv.id == OutItem.invoice_id).join(P, P.id == OutItem.product_id)
        .filter(OutInv.date >= since).scalar()
    )
    in_value = _f(
        db.query(func.coalesce(func.sum(InItem.quantity * _in_price()), 0))
        .select_from(InItem).join(InInv, InInv.id == InItem.invoice_id).join(P, P.id == InItem.product_id)
        .filter(InInv.date >= since).scalar()
    )
    out_qty = _f(
        db.query(func.coalesce(func.sum(OutItem.quantity), 0))
        .select_from(OutItem).join(OutInv, OutInv.id == OutItem.invoice_id).filter(OutInv.date >= since).scalar(), 3
    )
    dead = _dead_summary(db, days)
    dead_value = dead["total_value"]

    turnover = round(out_value / stock_value, 2) if stock_value > 0 else None
    low_count = db.query(func.count(P.id)).filter(P.is_active.is_(True), P.quantity <= P.min_quantity).scalar() or 0
    out_count = db.query(func.count(P.id)).filter(P.is_active.is_(True), P.quantity <= 0).scalar() or 0
    A = models.QuantityAdjustment
    adj = (
        db.query(func.count(A.id), func.coalesce(func.sum(A.difference * P.price), 0))
        .select_from(A).join(P, P.id == A.product_id).filter(A.date >= since).first()
    )
    return {
        "days": days, "stock_value": stock_value, "in_value": in_value, "out_value": out_value, "out_qty": out_qty,
        "turnover": turnover,  # عدد مرات "تدوير" قيمة المخزون في الفترة (تقديري)
        "days_of_inventory": round(days / turnover, 1) if turnover else None,
        "dead_stock_count": dead["count"], "dead_stock_value": dead_value,
        "dead_stock_pct": round(dead_value / stock_value * 100, 1) if stock_value > 0 else 0,
        "low_stock_count": low_count, "out_of_stock_count": out_count,
        "adjustments_count": adj[0] or 0, "adjustments_net_value": _f(adj[1]),
    }


# =====================================================================
#  اتجاه الوارد/الصادر عبر الزمن (مجمّع في SQL بالأيام - صفوف قليلة)
# =====================================================================
@router.get("/trend")
def trend(days: int = 90, group: str = "day", db: Session = Depends(get_db)):
    days = _period(days, 90, 7, 730)
    group = group if group in ("day", "week", "month") else "day"
    since = _since(days)

    def per_day(inv, item, price):
        day = func.date(inv.date)
        rows = (
            db.query(day, func.coalesce(func.sum(item.quantity), 0),
                     func.coalesce(func.sum(item.quantity * price), 0), func.count(distinct(inv.id)))
            .select_from(item).join(inv, inv.id == item.invoice_id).join(P, P.id == item.product_id)
            .filter(inv.date >= since).group_by(day).all()
        )
        return {str(r[0])[:10]: (float(r[1]), float(r[2]), int(r[3])) for r in rows}

    ins = per_day(InInv, InItem, _in_price())
    outs = per_day(OutInv, OutItem, _out_price())

    def bucket(d: str) -> str:
        dt = datetime.fromisoformat(d)
        if group == "week":
            return (dt - timedelta(days=dt.weekday())).date().isoformat()
        if group == "month":
            return d[:7]
        return d

    agg: Dict[str, dict] = defaultdict(lambda: {"in_qty": 0.0, "out_qty": 0.0, "in_value": 0.0, "out_value": 0.0, "invoices": 0})
    for d, (q, v, c) in ins.items():
        b = agg[bucket(d)]
        b["in_qty"] += q; b["in_value"] += v; b["invoices"] += c
    for d, (q, v, c) in outs.items():
        b = agg[bucket(d)]
        b["out_qty"] += q; b["out_value"] += v; b["invoices"] += c

    items = [
        {"period": k, "in_qty": round(v["in_qty"], 3), "out_qty": round(v["out_qty"], 3),
         "in_value": round(v["in_value"], 2), "out_value": round(v["out_value"], 2),
         "net_qty": round(v["in_qty"] - v["out_qty"], 3), "invoices": v["invoices"]}
        for k, v in sorted(agg.items())
    ]
    return {
        "days": days, "group": group, "items": items,
        "totals": {
            "in_qty": round(sum(i["in_qty"] for i in items), 3), "out_qty": round(sum(i["out_qty"] for i in items), 3),
            "in_value": round(sum(i["in_value"] for i in items), 2), "out_value": round(sum(i["out_value"] for i in items), 2),
        },
    }


# =====================================================================
#  تحليل ABC (باريتو)
#  بيحتاج الترتيب والمجموع الكلي للأصناف المباعة (صف واحد لكل صنف مباع، من غير أسطر فواتير)، والترقيم على النتيجة.
# =====================================================================
def _supports_window(db: Session) -> bool:
    """هل قاعدة البيانات بتدعم window functions؟ (SQLite 3.25+ / MySQL 8.0.2+ / MariaDB 10.2+ / PostgreSQL)."""
    dialect = db.get_bind().dialect
    ver = getattr(dialect, "server_version_info", None) or ()
    if dialect.name == "sqlite":
        return tuple(dialect.dbapi.sqlite_version_info) >= (3, 25, 0)
    if dialect.name == "mysql":
        return bool(ver) and tuple(ver) >= ((10, 2, 0) if getattr(dialect, "is_mariadb", False) else (8, 0, 2))
    return dialect.name == "postgresql"


def _abc_legacy(db: Session, since: datetime, days: int, page: int, page_size: int, search: Optional[str], until: Optional[datetime] = None) -> dict:
    """النسخة القديمة (حساب الفئات في Python) - بتتستخدم كـ fallback لو الداتا بيز مش بتدعم window functions (مثلًا MySQL 5.7)."""
    rows = (
        db.query(P.id, P.name, P.sku, func.sum(OutItem.quantity), func.sum(OutItem.quantity * _out_price()),
                 func.count(distinct(OutInv.id)))
        .select_from(OutItem).join(OutInv, OutInv.id == OutItem.invoice_id).join(P, P.id == OutItem.product_id)
        .filter(OutInv.date >= since, *([OutInv.date < until] if until is not None else []))
        .group_by(P.id, P.name, P.sku).all()
    )
    total_value = sum(float(r[4] or 0) for r in rows)
    basis = "value" if total_value > 0 else "quantity"
    metric = (lambda r: float(r[4] or 0)) if basis == "value" else (lambda r: float(r[3] or 0))
    rows = sorted(rows, key=lambda r: (-metric(r), r[0]))  # id عند التساوي (ترتيب ثابت زي النسخة الجديدة)
    total = sum(metric(r) for r in rows) or 1.0

    ranked, cum = [], 0.0
    summary = {c: {"count": 0, "value": 0.0, "qty": 0.0} for c in "ABC"}
    for rank, r in enumerate(rows, 1):
        prev_share = cum / total * 100
        cum += metric(r)
        cls = "A" if prev_share < 80 else ("B" if prev_share < 95 else "C")
        summary[cls]["count"] += 1
        summary[cls]["value"] += float(r[4] or 0)
        summary[cls]["qty"] += float(r[3] or 0)
        ranked.append({
            "rank": rank, "product_id": r[0], "name": r[1], "sku": r[2], "quantity": _f(r[3], 3), "value": _f(r[4]),
            "invoices": r[5], "share_pct": round(metric(r) / total * 100, 2), "cumulative_pct": round(cum / total * 100, 2), "class": cls,
        })
    for c in summary.values():
        c["value"], c["qty"] = round(c["value"], 2), round(c["qty"], 3)

    chart = ranked[:25]
    if search and search.strip():
        s = search.strip().lower()
        ranked = [r for r in ranked if s in (r["name"] or "").lower() or s in (r["sku"] or "").lower()]
    return paged(slice_page(ranked, page, page_size), len(ranked), page, page_size, days=days, basis=basis,
                 total_value=round(total_value, 2), products_count=len(rows), summary=summary, chart=chart)


def _abc_sql(db: Session, since: datetime, days: int, page: int, page_size: int, search: Optional[str], until: Optional[datetime] = None) -> dict:
    """
    ABC بالكامل في SQL وفي **استعلام واحد** (التجميع بيحصل مرة واحدة بس):
      agg     صف واحد لكل صنف مباع (الكمية والقيمة وعدد الفواتير)
      l1      الأساس (قيمة لو في مبيعات بالقيمة، وإلا الكمية) بـ SUM() OVER ()
      ranked  الترتيب (ROW_NUMBER) ومجموع اللي قبل الصنف (SUM() OVER (ROWS ... 1 PRECEDING)) والمجموع الكلي
      classed الفئة: A لو النسبة التراكمية **قبل** الصنف أقل من 80%، B لحد 95%، C الباقي (زي النسخة القديمة)
    وبنرجّع في نفس الاستعلام (UNION ALL): صفوف الصفحة ('p') + ملخص كل فئة ('s') + أول 25 للشارت ('c' - لو مش مستنتجة من الصفحة).
    البحث والترقيم برا الـ window فالـ rank والنسبة التراكمية بيفضلوا على كل الأصناف حتى مع البحث.
    ملاحظة: لازم نحدد ROWS صراحة - الـ frame الافتراضي (RANGE) بيجمّع الأصناف المتساوية مع بعض.
    """
    agg = (
        select(
            P.id.label("pid"), P.name.label("name"), P.sku.label("sku"),
            func.coalesce(func.sum(OutItem.quantity), 0).label("qty"),
            func.coalesce(func.sum(OutItem.quantity * _out_price()), 0).label("val"),
            func.count(distinct(OutInv.id)).label("inv"),
        )
        .select_from(OutItem).join(OutInv, OutInv.id == OutItem.invoice_id).join(P, P.id == OutItem.product_id)
        .where(OutInv.date >= since, *([OutInv.date < until] if until is not None else []))
        .group_by(P.id, P.name, P.sku).cte("agg")
    )
    tot_val = func.sum(agg.c.val).over()
    l1 = select(
        agg.c.pid, agg.c.name, agg.c.sku, agg.c.qty, agg.c.val, agg.c.inv,
        case((tot_val > 0, agg.c.val), else_=agg.c.qty).label("metric"),
        tot_val.label("tot_val"),
    ).subquery("l1")
    order = [l1.c.metric.desc(), l1.c.pid.asc()]
    tot_metric = func.sum(l1.c.metric).over()
    ranked = select(
        *l1.c,
        func.row_number().over(order_by=order).label("rk"),
        func.coalesce(func.sum(l1.c.metric).over(order_by=order, rows=(None, -1)), 0).label("prev"),  # مجموع اللي قبله بس
        case((tot_metric == 0, 1.0), else_=tot_metric).label("tot"),  # المجموع الكلي (أو 1 لو صفر - زي القديم)
        func.count().over().label("n"),
    ).subquery("ranked")
    cls_expr = case(
        (ranked.c.prev / ranked.c.tot * 100 < 80, literal_column("'A'")),
        (ranked.c.prev / ranked.c.tot * 100 < 95, literal_column("'B'")),
        else_=literal_column("'C'"),
    )
    classed = select(*ranked.c, cls_expr.label("cls")).cte("classed")
    C = classed.c

    searching = bool(search and search.strip())
    derive_chart = (not searching) and page == 1 and page_size >= 25  # الصفحة الأولى فيها الـ 25 الأوائل أصلًا
    NULL = lambda name: literal_column("NULL").label(name)
    row_cols = lambda kind: [
        literal_column(f"'{kind}'").label("k"), C.rk.label("rk"), C.pid.label("pid"), C.name.label("name"), C.sku.label("sku"),
        C.qty.label("qty"), C.val.label("val"), C.inv.label("inv"), C.metric.label("metric"), C.prev.label("prev"),
        C.tot.label("tot"), C.tot_val.label("tot_val"), C.n.label("n"), C.cls.label("cls"),
    ]

    page_q = select(*row_cols("p"), func.count().over().label("cnt")).select_from(classed)
    if searching:
        page_q = page_q.where(contains(C.name, search) | contains(C.sku, search))  # count() OVER () بتتحسب بعد الفلتر
    page_sub = page_q.order_by(C.rk).limit(page_size).offset((page - 1) * page_size).subquery("pg")
    parts = [select(*page_sub.c)]
    summary_q = select(
        literal_column("'s'").label("k"), NULL("rk"), NULL("pid"), NULL("name"), NULL("sku"),
        func.coalesce(func.sum(C.qty), 0).label("qty"), func.coalesce(func.sum(C.val), 0).label("val"), NULL("inv"), NULL("metric"),
        NULL("prev"), func.max(C.tot).label("tot"), func.max(C.tot_val).label("tot_val"), func.max(C.n).label("n"),
        C.cls.label("cls"), func.count().label("cnt"),
    ).group_by(C.cls)
    parts.append(summary_q)
    if not derive_chart:
        chart_sub = select(*row_cols("c"), NULL("cnt")).select_from(classed).where(C.rk <= 25).order_by(C.rk).subquery("ch")
        parts.append(select(*chart_sub.c))
    rows = db.execute(union_all(*parts)).all()

    summary = {c: {"count": 0, "value": 0.0, "qty": 0.0} for c in "ABC"}
    page_rows, chart_rows, n_products, total_value, total, cnt_filtered = [], [], 0, 0.0, 1.0, None
    for r in rows:
        if r.k == "s":
            summary[r.cls] = {"count": int(r.cnt), "value": round(float(r.val or 0), 2), "qty": round(float(r.qty or 0), 3)}
            n_products, total_value, total = int(r.n), float(r.tot_val or 0), float(r.tot)
        elif r.k == "p":
            page_rows.append(r)
            cnt_filtered = int(r.cnt)
        else:
            chart_rows.append(r)
    page_rows.sort(key=lambda r: r.rk)
    chart_rows.sort(key=lambda r: r.rk)
    basis = "value" if total_value > 0 else "quantity"

    def to_item(r) -> dict:
        m = float(r.metric or 0)
        cum = float(r.prev or 0) + m
        return {
            "rank": int(r.rk), "product_id": r.pid, "name": r.name, "sku": r.sku, "quantity": _f(r.qty, 3), "value": _f(r.val),
            "invoices": r.inv, "share_pct": round(m / total * 100, 2), "cumulative_pct": round(cum / total * 100, 2), "class": r.cls,
        }

    items = [to_item(r) for r in page_rows]
    chart = items[:25] if derive_chart else [to_item(r) for r in chart_rows]
    if not searching:
        total_rows = n_products
    elif cnt_filtered is not None:
        total_rows = cnt_filtered
    else:  # صفحة فاضية مع بحث (صفحة بعد الأخيرة): بنعدّ المطابق بس
        total_rows = db.execute(
            select(func.count()).select_from(classed).where(contains(C.name, search) | contains(C.sku, search))
        ).scalar() or 0
    return paged(items, total_rows, page, page_size, days=days, basis=basis,
                 total_value=round(total_value, 2), products_count=n_products, summary=summary, chart=chart)


@router.get("/abc")
def abc(
    days: int = 90, page: int = 1, page_size: int = 15, search: Optional[str] = None, export: bool = False,
    db: Session = Depends(get_db),
):
    days = _period(days)
    page, page_size = page_args(page, page_size, export=export)
    since = _since(days)
    if _supports_window(db):
        try:
            return _abc_sql(db, since, days, page, page_size, search)
        except DBAPIError:  # لو السيرفر رفض الـ window functions (نسخة قديمة / إعدادات) نرجع للنسخة القديمة
            db.rollback()
    return _abc_legacy(db, since, days, page, page_size, search)


# =====================================================================
#  التغطية (أيام الكفاية) واقتراح إعادة الطلب (SQL)
# =====================================================================
@router.get("/coverage")
def coverage(
    days: int = 30, lead_days: int = 14, page: int = 1, page_size: int = 15, search: Optional[str] = None,
    status: Optional[str] = None, export: bool = False, db: Session = Depends(get_db),
):
    days = _period(days, 30, 7, 365)
    lead_days = max(1, min(lead_days, 180))
    page, page_size = page_args(page, page_size, export=export)
    since = _since(days)

    sold_sq = (
        db.query(OutItem.product_id.label("pid"), func.sum(OutItem.quantity).label("sold"))
        .select_from(OutItem).join(OutInv, OutInv.id == OutItem.invoice_id)
        .filter(OutInv.date >= since).group_by(OutItem.product_id).subquery()
    )
    cover = P.quantity * days / func.nullif(sold_sq.c.sold, 0)  # أيام الكفاية = الرصيد / (المباع ÷ الأيام)
    status_expr = case(
        (cover <= lead_days, literal("critical")), (cover <= lead_days * 2, literal("warning")), else_=literal("ok"),
    )
    base = (
        db.query(P, sold_sq.c.sold, cover.label("cover"), status_expr.label("status"))
        .select_from(sold_sq).join(P, P.id == sold_sq.c.pid).filter(P.is_active.is_(True), sold_sq.c.sold > 0)
    )
    counts = {"critical": 0, "warning": 0, "ok": 0}
    for st, n in base.with_entities(status_expr, func.count(P.id)).group_by(status_expr).all():
        counts[st] = n
    q = _search_products(base, search)
    if status in counts:
        q = q.filter(status_expr == status)
    total = q.count()
    rows = q.order_by(cover.asc(), P.id).offset((page - 1) * page_size).limit(page_size).all()

    items = []
    for p, sold, cov, st in rows:
        daily = float(sold) / days
        qty = float(p.quantity or 0)
        suggest = max(0.0, daily * lead_days + float(p.min_quantity or 0) - qty)
        items.append({
            "product_id": p.id, "name": p.name, "sku": p.sku, "unit": p.unit, "quantity": _f(qty, 3),
            "min_quantity": _f(p.min_quantity, 3), "sold_in_period": _f(sold, 3), "avg_daily": _f(daily, 3),
            "days_of_cover": round(float(cov), 1) if cov is not None else None, "status": st,
            "suggested_order": math.ceil(suggest) if suggest > 0 else 0,
        })
    return paged(items, total, page, page_size, days=days, lead_days=lead_days, summary=counts)


# =====================================================================
#  الربحية التقديرية (SQL): سعر البيع مقابل متوسط تكلفة الشراء
# =====================================================================
@router.get("/margin")
def margin(
    days: int = 90, page: int = 1, page_size: int = 15, search: Optional[str] = None,
    sort: str = "profit", export: bool = False, db: Session = Depends(get_db),
):
    days = _period(days)
    page, page_size = page_args(page, page_size, export=export)
    since = _since(days)

    out_sq = (
        db.query(OutItem.product_id.label("pid"), func.sum(OutItem.quantity).label("qty"),
                 func.sum(OutItem.quantity * _out_price()).label("rev"))
        .select_from(OutItem).join(OutInv, OutInv.id == OutItem.invoice_id).join(P, P.id == OutItem.product_id)
        .filter(OutInv.date >= since).group_by(OutItem.product_id).subquery()
    )
    cost_sq = (
        db.query(InItem.product_id.label("pid"),
                 (func.sum(InItem.quantity * InItem.unit_price) / func.nullif(func.sum(InItem.quantity), 0)).label("avg_cost"))
        .filter(InItem.unit_price.isnot(None), InItem.unit_price > 0).group_by(InItem.product_id).subquery()
    )
    has_cost = cost_sq.c.avg_cost.isnot(None)
    cogs = out_sq.c.qty * cost_sq.c.avg_cost
    profit = out_sq.c.rev - cogs

    base = (
        db.query(P.id, P.name, P.sku, out_sq.c.qty, out_sq.c.rev, cost_sq.c.avg_cost)
        .select_from(out_sq).join(P, P.id == out_sq.c.pid).outerjoin(cost_sq, cost_sq.c.pid == out_sq.c.pid)
    )
    s = base.with_entities(
        func.count(P.id),
        func.coalesce(func.sum(case((has_cost, out_sq.c.rev), else_=0)), 0),
        func.coalesce(func.sum(case((has_cost, cogs), else_=0)), 0),
        func.coalesce(func.sum(case((has_cost, 1), else_=0)), 0),
        func.coalesce(func.sum(case((and_(has_cost, profit < 0), 1), else_=0)), 0),
    ).first()
    n_all, rev_c, cogs_c, n_cost, n_neg = s[0] or 0, float(s[1] or 0), float(s[2] or 0), int(s[3] or 0), int(s[4] or 0)

    q = _search_products(base, search)
    total = q.count()
    sort_expr = {
        "revenue": out_sq.c.rev.desc(), "quantity": out_sq.c.qty.desc(),
        "margin": (profit / func.nullif(out_sq.c.rev, 0)).desc(),
    }.get(sort, profit.desc())
    rows = (
        q.order_by(case((has_cost, 0), else_=1), sort_expr, P.id)
        .offset((page - 1) * page_size).limit(page_size).all()
    )
    items = []
    for pid, name, sku, qty, rev, cost in rows:
        qty, rev = float(qty or 0), float(rev or 0)
        row = {"product_id": pid, "name": name, "sku": sku, "quantity": round(qty, 3), "revenue": round(rev, 2),
               "avg_cost": round(float(cost), 2) if cost is not None else None, "cogs": None, "profit": None, "margin_pct": None}
        if cost is not None:
            c = qty * float(cost)
            row.update({"cogs": round(c, 2), "profit": round(rev - c, 2),
                        "margin_pct": round((rev - c) / rev * 100, 1) if rev > 0 else None})
        items.append(row)
    return paged(items, total, page, page_size, days=days, summary={
        "revenue": round(rev_c, 2), "cogs": round(cogs_c, 2), "profit": round(rev_c - cogs_c, 2),
        "margin_pct": round((rev_c - cogs_c) / rev_c * 100, 1) if rev_c > 0 else None,
        "products_count": n_all, "with_cost_count": n_cost, "coverage_pct": round(n_cost / n_all * 100, 1) if n_all else 0,
        "negative_count": n_neg,
    })


# =====================================================================
#  أداء الموظفين (عدد المستخدمين صغير؛ التجميع في SQL والترقيم على الناتج)
# =====================================================================
@router.get("/employees")
def employees(
    days: int = 30, page: int = 1, page_size: int = 15, search: Optional[str] = None, export: bool = False,
    db: Session = Depends(get_db),
):
    days = _period(days, 30, 7, 365)
    page, page_size = page_args(page, page_size, export=export)
    since = _since(days)
    users: Dict[str, dict] = defaultdict(lambda: {
        "in_invoices": 0, "in_qty": 0.0, "out_invoices": 0, "out_qty": 0.0, "transfers": 0, "adjustments": 0, "adjustments_net": 0.0,
    })

    def inv_stats(inv, item, user_col, inv_key, qty_key):
        rows = (
            db.query(user_col, func.count(distinct(inv.id)), func.coalesce(func.sum(item.quantity), 0))
            .select_from(inv).outerjoin(item, item.invoice_id == inv.id)
            .filter(inv.date >= since).group_by(user_col).all()
        )
        for name, cnt, qty in rows:
            u = users[name or "غير معروف"]
            u[inv_key] += cnt
            u[qty_key] += float(qty or 0)

    inv_stats(InInv, InItem, InInv.received_by_username, "in_invoices", "in_qty")
    inv_stats(OutInv, OutItem, OutInv.delivered_by_username, "out_invoices", "out_qty")
    for name, cnt in (
        db.query(models.LocationTransfer.moved_by_username, func.count(models.LocationTransfer.id))
        .filter(models.LocationTransfer.date >= since).group_by(models.LocationTransfer.moved_by_username).all()
    ):
        users[name or "غير معروف"]["transfers"] += cnt
    for name, cnt, net in (
        db.query(models.QuantityAdjustment.changed_by_username, func.count(models.QuantityAdjustment.id),
                 func.coalesce(func.sum(models.QuantityAdjustment.difference), 0))
        .filter(models.QuantityAdjustment.date >= since).group_by(models.QuantityAdjustment.changed_by_username).all()
    ):
        u = users[name or "غير معروف"]
        u["adjustments"] += cnt
        u["adjustments_net"] += float(net or 0)

    items = []
    for name, u in users.items():
        ops = u["in_invoices"] + u["out_invoices"] + u["transfers"] + u["adjustments"]
        items.append({"username": name, **{k: (round(v, 3) if isinstance(v, float) else v) for k, v in u.items()}, "operations": ops})
    items.sort(key=lambda x: -x["operations"])
    chart = items[:12]
    if search and search.strip():
        s = search.strip().lower()
        items = [i for i in items if s in i["username"].lower()]
    return paged(slice_page(items, page, page_size), len(items), page, page_size, days=days, chart=chart)


# =====================================================================
#  تفاوت أسعار الشراء بين الموردين (SQL: تجميع على مستويين - صنف × مورد ثم صنف)
# =====================================================================
@router.get("/price-variance")
def price_variance(
    days: int = 365, page: int = 1, page_size: int = 15, search: Optional[str] = None,
    sort: str = "saving", export: bool = False, db: Session = Depends(get_db),
):
    days = _period(days, 365, 30, 1460)
    page, page_size = page_args(page, page_size, export=export)
    since = _since(days)

    def valid(q):
        return q.filter(InInv.date >= since, InItem.unit_price.isnot(None), InItem.unit_price > 0)

    sub1 = valid(
        db.query(
            InItem.product_id.label("pid"), InInv.supplier_id.label("sid"),
            func.sum(InItem.quantity * InItem.unit_price).label("cost"), func.sum(InItem.quantity).label("qty"),
            func.count(InItem.id).label("n"), func.min(InItem.unit_price).label("pmin"), func.max(InItem.unit_price).label("pmax"),
        ).select_from(InItem).join(InInv, InInv.id == InItem.invoice_id)
    ).group_by(InItem.product_id, InInv.supplier_id).subquery()

    avg_s = sub1.c.cost / func.nullif(sub1.c.qty, 0)
    sub2 = (
        db.query(
            sub1.c.pid.label("pid"), func.sum(sub1.c.cost).label("cost"), func.sum(sub1.c.qty).label("qty"),
            func.sum(sub1.c.n).label("n"), func.min(sub1.c.pmin).label("pmin"), func.max(sub1.c.pmax).label("pmax"),
            func.min(avg_s).label("best_avg"), func.count().label("suppliers"),
        ).group_by(sub1.c.pid).subquery()
    )
    saving = case((sub2.c.suppliers > 1, sub2.c.cost - sub2.c.best_avg * sub2.c.qty), else_=0)
    spread = (sub2.c.pmax - sub2.c.pmin) / func.nullif(sub2.c.pmin, 0) * 100

    s = db.query(
        func.count(), func.coalesce(func.sum(saving), 0), func.coalesce(func.sum(case((spread >= 15, 1), else_=0)), 0)
    ).select_from(sub2).first()
    summary = {"products_count": s[0] or 0, "potential_saving": _f(s[1]), "flagged_count": int(s[2] or 0)}

    base = db.query(P.id, P.name, P.sku, sub2.c.n, sub2.c.pmin, sub2.c.pmax, sub2.c.cost, sub2.c.qty,
                    sub2.c.suppliers, saving.label("saving"), spread.label("spread")
                    ).select_from(sub2).join(P, P.id == sub2.c.pid)
    q = _search_products(base, search)
    total = q.count()
    order = spread.desc() if sort == "spread" else saving.desc()
    rows = q.order_by(order, spread.desc(), P.id).offset((page - 1) * page_size).limit(page_size).all()
    pids = [r[0] for r in rows]

    # آخر سعر شراء + موردين الصفحة دي بس
    last = {}
    sup: Dict[int, list] = defaultdict(list)
    if pids:
        latest = valid(
            db.query(InItem.product_id.label("pid"), func.max(InInv.date).label("d"))
            .select_from(InItem).join(InInv, InInv.id == InItem.invoice_id).filter(InItem.product_id.in_(pids))
        ).group_by(InItem.product_id).subquery()
        for pid, price in valid(
            db.query(InItem.product_id, InItem.unit_price).select_from(InItem)
            .join(InInv, InInv.id == InItem.invoice_id)
            .join(latest, and_(latest.c.pid == InItem.product_id, latest.c.d == InInv.date))
        ).all():
            last[pid] = float(price)
        for pid, sname, cost, qty in (
            db.query(sub1.c.pid, models.Supplier.name, sub1.c.cost, sub1.c.qty)
            .select_from(sub1).outerjoin(models.Supplier, models.Supplier.id == sub1.c.sid).filter(sub1.c.pid.in_(pids)).all()
        ):
            qty = float(qty or 0)
            avg = float(cost or 0) / qty if qty else 0
            sup[pid].append({"name": sname or "بدون مورد", "avg_price": round(avg, 2), "qty": round(qty, 3)})

    items = []
    for pid, name, sku, n, pmin, pmax, cost, qty, nsup, sav, spr in rows:
        avg_all = float(cost) / float(qty) if qty else 0
        suppliers = sorted(sup.get(pid, []), key=lambda x: x["avg_price"])
        lp = last.get(pid)
        items.append({
            "product_id": pid, "name": name, "sku": sku, "purchases": int(n or 0),
            "min_price": round(float(pmin), 2), "max_price": round(float(pmax), 2),
            "avg_price": round(avg_all, 2), "last_price": round(lp, 2) if lp is not None else None,
            "last_vs_avg_pct": round((lp - avg_all) / avg_all * 100, 1) if lp is not None and avg_all else None,
            "spread_pct": round(float(spr or 0), 1), "suppliers": suppliers,
            "best_supplier": suppliers[0]["name"] if len(suppliers) > 1 else None,
            "potential_saving": round(float(sav or 0), 2), "flag": float(spr or 0) >= 15,
        })
    return paged(items, total, page, page_size, days=days, summary=summary)


# =====================================================================
#  تعديلات الكميات اليدوية (SQL)
# =====================================================================
@router.get("/adjustments")
def adjustments(
    days: int = 30, page: int = 1, page_size: int = 15, search: Optional[str] = None,
    flagged: bool = False, export: bool = False, db: Session = Depends(get_db),
):
    days = _period(days, 30, 7, 730)
    page, page_size = page_args(page, page_size, export=export)
    since = _since(days)
    A = models.QuantityAdjustment
    value = A.difference * P.price
    flag = and_(func.abs(A.difference) >= 10,
                func.abs(A.difference) >= 0.2 * case((A.old_quantity > 1, A.old_quantity), else_=1))

    base = db.query(A).select_from(A).join(P, P.id == A.product_id).filter(A.date >= since)
    srow = base.with_entities(
        func.count(A.id), func.coalesce(func.sum(case((flag, 1), else_=0)), 0),
        func.coalesce(func.sum(case((value < 0, -value), else_=0)), 0),
        func.coalesce(func.sum(case((value >= 0, value), else_=0)), 0),
    ).first()
    loss, gain = float(srow[2] or 0), float(srow[3] or 0)
    summary = {"count": srow[0] or 0, "flagged_count": int(srow[1] or 0), "loss_value": round(loss, 2),
               "gain_value": round(gain, 2), "net_value": round(gain - loss, 2)}

    loss_sum = func.sum(case((value < 0, -value), else_=0))
    by_user = [
        {"username": u or "غير معروف", "count": c, "loss_value": round(float(l or 0), 2), "gain_value": round(float(g or 0), 2)}
        for u, c, l, g in base.with_entities(
            A.changed_by_username, func.count(A.id), loss_sum, func.sum(case((value >= 0, value), else_=0)),
        ).group_by(A.changed_by_username).order_by(loss_sum.desc()).limit(10).all()
    ]

    q = base
    if search and search.strip():
        q = q.filter(contains(P.name, search) | contains(P.sku, search))
    if flagged:
        q = q.filter(flag)
    total = q.count()
    rows = (
        q.with_entities(A, P.name, P.sku, P.price, case((flag, 1), else_=0).label("flag"))
        .order_by(case((flag, 0), else_=1), func.abs(value).desc(), A.id.desc())
        .offset((page - 1) * page_size).limit(page_size).all()
    )
    items = []
    for a, name, sku, price, fl in rows:
        diff = float(a.difference or 0)
        items.append({
            "id": a.id, "product_id": a.product_id, "name": name, "sku": sku, "old_quantity": _f(a.old_quantity, 3),
            "new_quantity": _f(a.new_quantity, 3), "difference": _f(diff, 3), "value_impact": round(diff * float(price or 0), 2),
            "user": a.changed_by_username, "reason": a.reason, "date": a.date, "flagged": bool(fl),
        })
    return paged(items, total, page, page_size, days=days, summary=summary, by_user=by_user)


# =====================================================================
#  خريطة النشاط (تجميع SQL حسب اليوم والساعة)
# =====================================================================
def _weekday_hour(db: Session, col):
    """(يوم الأسبوع: الاثنين=0, الساعة) كتعبيرات SQL متوافقة مع SQLite و MySQL."""
    if db.bind is not None and db.bind.dialect.name == "mysql":
        return func.weekday(col), func.hour(col)
    # SQLite: %w الأحد=0 .. السبت=6 -> نحوّله لـ الاثنين=0
    return (cast(func.strftime("%w", col), Integer) + 6) % 7, cast(func.strftime("%H", col), Integer)


@router.get("/heatmap")
def heatmap(days: int = 90, db: Session = Depends(get_db)):
    days = _period(days, 90, 7, 365)
    since = _since(days)
    grid = [[0] * 24 for _ in range(7)]  # [weekday (الاثنين=0)][hour]
    total = 0
    for model in (InInv, OutInv, models.LocationTransfer):
        wd, hr = _weekday_hour(db, model.date)
        for w, h, c in db.query(wd, hr, func.count()).filter(model.date >= since, model.date.isnot(None)).group_by(wd, hr).all():
            grid[int(w)][int(h)] += c
            total += c
    peak = max(((grid[w][h], w, h) for w in range(7) for h in range(24)), default=(0, 0, 0))
    return {"days": days, "total": total, "grid": grid,
            "peak": {"count": peak[0], "weekday": peak[1], "hour": peak[2]} if peak[0] else None}
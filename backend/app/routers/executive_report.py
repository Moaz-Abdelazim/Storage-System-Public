"""
التقرير التنفيذي للإدارة (للأدمن فقط) - endpoint واحد بيرجّع كل أقسام التقرير المطبوع في ضربة واحدة:
  GET /api/analytics/executive-report?days=30            (آخر N يوم، شاملة النهارده)
  GET /api/analytics/executive-report?date_from=YYYY-MM-DD&date_to=YYYY-MM-DD

الأداء: كل الأرقام بـ SQL مجمّع (GROUP BY / عدّادات شرطية)، ومفيش تحميل لفواتير أو أسطر في الذاكرة. الصفوف الراجعة محدودة
(قوايم أول 10/15/25 + نقاط الاتجاه الزمني بعدد قليل). عدد الاستعلامات 18 بالظبط (+2 للتحقق من الجلسة) - مكتوب جنب كل قسم.
بنعيد استخدام دوال analytics.py (ABC والراكد وأسعار الصادر/الوارد) عشان الأرقام تطابق صفحة التحليلات بالظبط.
الفلاتر والتجميع بالأسبوع/الشهر شغالة على SQLite و MySQL (dialect check).
"""
from collections import defaultdict
from datetime import date, datetime, timedelta
from typing import Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import Integer, and_, case, cast, distinct, exists, func, literal_column
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from .. import models, auth
from ..database import get_db
from ..timezone_utils import now_cairo
from .analytics import _abc_legacy, _abc_sql, _dead_base, _dead_summary, _f, _in_price, _out_price, _supports_window

router = APIRouter(prefix="/api/analytics", tags=["Executive Report"], dependencies=[Depends(auth.require_permission("reports.view", "finance.view"))])

P = models.Product
InInv, InItem = models.StockInInvoice, models.StockInItem
OutInv, OutItem = models.StockOutInvoice, models.StockOutItem
MAX_DAYS = 1095  # 3 سنين


# ---------- الفترة ----------
def _parse_day(value: str, label: str) -> date:
    try:
        return date.fromisoformat(value[:10])
    except (ValueError, TypeError):
        raise HTTPException(status_code=400, detail=f"صيغة {label} غير صحيحة (YYYY-MM-DD)")


def _period(days: Optional[int], date_from: Optional[str], date_to: Optional[str]):
    """(start, end_exclusive, عدد الأيام) بتوقيت القاهرة. من غير باراميترات: آخر 30 يوم."""
    today = now_cairo().date()
    if date_from or date_to:
        if not (date_from and date_to):
            raise HTTPException(status_code=400, detail="حدد تاريخ البداية والنهاية مع بعض")
        d_from, d_to = _parse_day(date_from, "تاريخ البداية"), min(_parse_day(date_to, "تاريخ النهاية"), today)
        if d_from > d_to:
            raise HTTPException(status_code=400, detail="تاريخ البداية بعد تاريخ النهاية")
    else:
        n = max(1, min(days or 30, MAX_DAYS))
        d_to, d_from = today, today - timedelta(days=n - 1)
    n = (d_to - d_from).days + 1
    if n > MAX_DAYS:
        raise HTTPException(status_code=400, detail="الفترة أطول من 3 سنين - قلّل الفترة")
    return datetime.combine(d_from, datetime.min.time()), datetime.combine(d_to + timedelta(days=1), datetime.min.time()), n


# ---------- التجميع الزمني (أسبوع/شهر) بـ SQL على SQLite و MySQL ----------
def _bucket_expr(db: Session, col, group: str):
    """تعبير SQL بيرجّع مفتاح الفترة: week = رقم يوم (منذ 1970) لبداية الأسبوع (الإثنين)، month = 'YYYY-MM'."""
    mysql = db.get_bind().dialect.name == "mysql"
    if group == "month":
        return func.date_format(col, "%Y-%m") if mysql else func.strftime("%Y-%m", col)
    if mysql:  # أيام منذ 1970 للإثنين: TO_DAYS(date) - 719528 - WEEKDAY(date)  (WEEKDAY: الإثنين = 0)
        return func.to_days(func.date(col)) - literal_column("719528") - func.weekday(col)
    # SQLite: julianday(date) - 2440587.5 = أيام منذ 1970، و strftime('%w') الأحد = 0 فبنطرح (w+6)%7 عشان نوصل للإثنين
    days = cast(func.julianday(func.date(col)) - literal_column("2440587.5"), Integer)
    weekday = (cast(func.strftime("%w", col), Integer) + literal_column("6")) % literal_column("7")
    return days - weekday


def _bucket_key(raw, group: str) -> str:
    if group == "month":
        return str(raw)[:7]
    return (date(1970, 1, 1) + timedelta(days=int(raw))).isoformat()


def _bucket_keys(start: datetime, end: datetime, group: str) -> List[str]:
    """كل مفاتيح الفترات من البداية للنهاية (بما فيها الفاضية - بتظهر بصفر في الشارت)."""
    keys, last = [], (end - timedelta(days=1)).date()
    if group == "month":
        y, m = start.year, start.month
        while (y, m) <= (last.year, last.month):
            keys.append(f"{y:04d}-{m:02d}")
            y, m = (y + 1, 1) if m == 12 else (y, m + 1)
        return keys
    d = start.date() - timedelta(days=start.weekday())
    while d <= last:
        keys.append(d.isoformat())
        d += timedelta(days=7)
    return keys


def _is_partial(key: str, group: str, start: datetime, end: datetime) -> bool:
    """الفترة اللي بتبدأ قبل بداية التقرير أو بتنتهي بعد نهايته (ناقصة) - مش بتتحسب في أعلى/أقل فترة."""
    if group == "month":
        y, m = int(key[:4]), int(key[5:7])
        first = date(y, m, 1)
        nxt = date(y + (m == 12), 1 if m == 12 else m + 1, 1)
        return first < start.date() or datetime.combine(nxt, datetime.min.time()) > end
    first = date.fromisoformat(key)
    return first < start.date() or datetime.combine(first + timedelta(days=7), datetime.min.time()) > end


def _series(db: Session, inv, item, price, start, end, group: str) -> Dict[str, tuple]:
    """{مفتاح الفترة: (كمية، قيمة، عدد فواتير)} - الفاتورة بتقع في فترة واحدة بس (حسب تاريخها)."""
    expr = _bucket_expr(db, inv.date, group)
    rows = (
        db.query(expr, func.coalesce(func.sum(item.quantity), 0), func.coalesce(func.sum(item.quantity * price), 0), func.count(distinct(inv.id)))
        .select_from(item).join(inv, inv.id == item.invoice_id).join(P, P.id == item.product_id)
        .filter(inv.date >= start, inv.date < end).group_by(expr).all()
    )
    return {_bucket_key(r[0], group): (float(r[1]), float(r[2]), int(r[3])) for r in rows}


def _pct_change(cur: float, prev: float) -> Optional[float]:
    return round((cur - prev) / prev * 100, 1) if prev else None


def _window_totals(db: Session, inv, item, price, start, end) -> dict:
    r = (
        db.query(func.count(distinct(inv.id)), func.coalesce(func.sum(item.quantity), 0), func.coalesce(func.sum(item.quantity * price), 0))
        .select_from(item).join(inv, inv.id == item.invoice_id).join(P, P.id == item.product_id)
        .filter(inv.date >= start, inv.date < end).one()
    )
    return {"invoices": int(r[0]), "qty": _f(r[1], 3), "value": _f(r[2])}


def _from_series(s: Dict[str, tuple]) -> dict:
    """إجماليات الفترة الحالية من نقاط الاتجاه نفسها (من غير مسح تاني لنفس الصفوف)."""
    return {"invoices": sum(v[2] for v in s.values()), "qty": round(sum(v[0] for v in s.values()), 3), "value": round(sum(v[1] for v in s.values()), 2)}


def _top_parties(db: Session, inv, item, price, party_model, fk, start, end, limit: int = 10) -> list:
    value = func.coalesce(func.sum(item.quantity * price), 0)
    qty = func.coalesce(func.sum(item.quantity), 0)
    rows = (
        db.query(party_model.name, func.count(distinct(inv.id)), qty, value)
        .select_from(item).join(inv, inv.id == item.invoice_id).join(P, P.id == item.product_id)
        .outerjoin(party_model, party_model.id == fk)
        .filter(inv.date >= start, inv.date < end).group_by(fk, party_model.name)
        .order_by(value.desc(), qty.desc()).limit(limit).all()
    )
    return [{"name": r[0] or "غير محدد", "invoices": int(r[1]), "quantity": _f(r[2], 3), "value": _f(r[3])} for r in rows]


@router.get("/executive-report")
def executive_report(
    days: Optional[int] = None, date_from: Optional[str] = None, date_to: Optional[str] = None,
    db: Session = Depends(get_db),
):
    start, end, n_days = _period(days, date_from, date_to)
    prev_start = start - timedelta(days=n_days)
    group = "week" if n_days <= 90 else "month"
    shown_end = (end - timedelta(days=1)).date()

    # 1) مؤشرات المخزون الحالية (استعلام 1)
    k = (
        db.query(
            func.count(P.id), func.coalesce(func.sum(P.quantity), 0), func.coalesce(func.sum(P.quantity * P.price), 0),
            func.coalesce(func.sum(case((P.quantity <= P.min_quantity, 1), else_=0)), 0),
            func.coalesce(func.sum(case((P.quantity <= 0, 1), else_=0)), 0),
        ).filter(P.is_active.is_(True)).one()
    )
    total_pieces, stock_value = _f(k[1], 3), _f(k[2])

    # 2+3) الاتجاه عبر الزمن: صادر ووارد مجمّعين بالأسبوع/الشهر في SQL (استعلامين، وبنحسب منهم إجماليات الفترة الحالية)،
    #      وإجماليات الفترة السابقة (استعلامين)، والنقل (استعلام). الفترات الفاضية بتتحط بصفر في الشارت.
    out_s = _series(db, OutInv, OutItem, _out_price(), start, end, group)
    in_s = _series(db, InInv, InItem, _in_price(), start, end, group)
    out_mv = {"cur": _from_series(out_s), "prev": _window_totals(db, OutInv, OutItem, _out_price(), prev_start, start)}
    in_mv = {"cur": _from_series(in_s), "prev": _window_totals(db, InInv, InItem, _in_price(), prev_start, start)}
    T = models.LocationTransfer
    cur_t, prv_t = and_(T.date >= start, T.date < end), and_(T.date >= prev_start, T.date < start)
    tr = db.query(
        func.coalesce(func.sum(case((cur_t, 1), else_=0)), 0), func.coalesce(func.sum(case((cur_t, T.quantity), else_=0)), 0),
        func.coalesce(func.sum(case((prv_t, 1), else_=0)), 0),
    ).filter(T.date >= prev_start, T.date < end).one()

    series = []
    for key in _bucket_keys(start, end, group):
        oq, ov, _oc = out_s.get(key, (0.0, 0.0, 0))
        iq, iv, _ic = in_s.get(key, (0.0, 0.0, 0))
        series.append({"period": key, "out_qty": round(oq, 3), "out_value": round(ov, 2), "in_qty": round(iq, 3), "in_value": round(iv, 2),
                       "partial": _is_partial(key, group, start, end)})
    full = [s for s in series if not s["partial"]]
    pool = full if len(full) >= 2 else series
    peak = max(pool, key=lambda s: (s["out_value"], s["out_qty"])) if pool else None
    trough = min(pool, key=lambda s: (s["out_value"], s["out_qty"])) if pool else None

    # 4) ABC (استعلام واحد - نفس دالة صفحة التحليلات) + تنبيه أصناف A اللي نفدت (استعلام)
    try:
        abc = _abc_sql(db, start, n_days, 1, 25, None, until=end) if _supports_window(db) else _abc_legacy(db, start, n_days, 1, 25, None, until=end)
    except DBAPIError:
        db.rollback()
        abc = _abc_legacy(db, start, n_days, 1, 25, None, until=end)
    a_ids = [i["product_id"] for i in abc["chart"] if i["class"] == "A"]
    alerts = []
    if a_ids:
        stock = {r[0]: r for r in db.query(P.id, P.quantity, P.min_quantity, P.unit).filter(P.id.in_(a_ids)).all()}
        for i in abc["chart"]:
            r = stock.get(i["product_id"])
            if i["class"] == "A" and r and (r[1] or 0) <= max(r[2] or 0, 0):
                alerts.append({"product_id": i["product_id"], "name": i["name"], "sku": i["sku"], "quantity": _f(r[1], 3),
                               "min_quantity": _f(r[2], 3), "unit": r[3]})

    # 5) الراكد: ملخص (استعلامين) + أكبر 10 أصناف (استعلام)
    dead_days = max(7, min(n_days, 730))
    dead = _dead_summary(db, dead_days)
    ever_out = exists().where(OutItem.product_id == P.id).label("ever")
    dead_rows = (
        _dead_base(db, dead_days)
        .with_entities(P.id, P.name, P.sku, P.unit, P.quantity, P.price, (P.quantity * P.price).label("value"), ever_out)
        .order_by((P.quantity * P.price).desc(), P.id).limit(10).all()
    )

    # 6) أصناف تحت الحد الأدنى: الأكبر عجزًا أولًا (استعلام)
    low_rows = (
        db.query(P.id, P.name, P.sku, P.unit, P.quantity, P.min_quantity)
        .filter(P.is_active.is_(True), P.quantity <= P.min_quantity)
        .order_by((P.quantity - P.min_quantity).asc(), P.id).limit(15).all()
    )

    # 7) أفضل الموردين والجهات المستلمة (استعلامين)
    suppliers = _top_parties(db, InInv, InItem, _in_price(), models.Supplier, InInv.supplier_id, start, end)
    recipients = _top_parties(db, OutInv, OutItem, _out_price(), models.Recipient, OutInv.recipient_id, start, end)

    # 8) قيمة المخزون لكل مخزن (استعلام): كل المخازن حتى اللي فاضية
    W, L, PL = models.Warehouse, models.Location, models.ProductLocation
    wh_rows = (
        db.query(W.id, W.code, W.name, func.coalesce(func.sum(PL.quantity), 0), func.coalesce(func.sum(PL.quantity * P.price), 0),
                 func.count(distinct(P.id)))
        .select_from(W).outerjoin(L, L.warehouse_id == W.id)
        .outerjoin(PL, and_(PL.location_id == L.id, PL.quantity > 0))
        .outerjoin(P, and_(P.id == PL.product_id, P.is_active.is_(True)))
        .group_by(W.id, W.code, W.name).order_by(W.code).all()
    )
    wh_items = [{"id": r[0], "code": r[1], "name": r[2], "quantity": _f(r[3], 3), "value": _f(r[4]), "products": int(r[5])} for r in wh_rows]
    placed_val, placed_qty = sum(w["value"] for w in wh_items), sum(w["quantity"] for w in wh_items)
    base = stock_value if stock_value > 0 else (placed_val or 1)
    for w in wh_items:
        w["pct_value"] = round(w["value"] / base * 100, 1)
    unassigned = None
    if stock_value - placed_val > 0.005 or total_pieces - placed_qty > 0.0005:
        unassigned = {"quantity": round(max(total_pieces - placed_qty, 0), 3), "value": round(max(stock_value - placed_val, 0), 2),
                      "pct_value": round(max(stock_value - placed_val, 0) / base * 100, 1)}

    # 9) عمق البيانات (استعلام) + مقارنة نفس الشهر في السنة السابقة لو البيانات 12 شهر أو أكتر (استعلام)
    first = db.query(func.min(OutInv.date)).scalar(), db.query(func.min(InInv.date)).scalar()  # استعلامين صغيرين (فهرس التاريخ)
    first_dates = [d for d in first if d]
    data_start = min(first_dates) if first_dates else None
    months_of_data = max(0.0, round((end - data_start).days / 30.44, 1)) if data_start else 0.0
    yoy = {"available": False, "months": []}
    if months_of_data >= 12:
        last_month = date(shown_end.year, shown_end.month, 1)
        cur_months = []
        y, m = last_month.year, last_month.month
        for _ in range(12):
            cur_months.append(f"{y:04d}-{m:02d}")
            y, m = (y - 1, 12) if m == 1 else (y, m - 1)
        cur_months.reverse()
        win_start = datetime(int(cur_months[0][:4]) - 1, int(cur_months[0][5:7]), 1)
        monthly = _series(db, OutInv, OutItem, _out_price(), win_start, end, "month")
        for key in cur_months:
            prev_key = f"{int(key[:4]) - 1:04d}-{key[5:7]}"
            prev_exists = data_start is not None and datetime(int(prev_key[:4]), int(prev_key[5:7]), 1) + timedelta(days=31) > data_start
            cq, cv, _c1 = monthly.get(key, (0.0, 0.0, 0))
            pq, pv, _c2 = monthly.get(prev_key, (0.0, 0.0, 0))
            yoy["months"].append({"month": key, "out_qty": round(cq, 3), "out_value": round(cv, 2),
                                  "prev_out_qty": round(pq, 3) if prev_exists else None, "prev_out_value": round(pv, 2) if prev_exists else None,
                                  "change_pct": _pct_change(cv, pv) if prev_exists else None,
                                  "partial": key == f"{shown_end.year:04d}-{shown_end.month:02d}" and (end.date() != date(shown_end.year + (shown_end.month == 12), 1 if shown_end.month == 12 else shown_end.month + 1, 1))})
        yoy["available"] = any(x["prev_out_value"] is not None for x in yoy["months"])
    if months_of_data <= 0:
        note = "مفيش حركات مسجّلة في النظام قبل نهاية الفترة دي."
    elif months_of_data < 12:
        note = f"بيانات النظام حوالي {months_of_data:g} شهر (أقل من 12 شهر) فالمواسم لسه مش واضحة، والاتجاه ده مؤشر عام بس."
    elif not yoy["available"]:
        note = "المقارنة بنفس الشهر في السنة السابقة غير متاحة لأن البيانات لسه مغطيتش سنة كاملة قبل الفترة."
    else:
        note = None

    # المقارنة بالفترة السابقة مش منطقية لو الفترة السابقة بتبدأ قبل أول حركة في النظام (هتبان "زيادة" وهمية)
    covered = data_start is not None and data_start <= prev_start

    def cmp(cur, prev):
        return {"current": cur, "previous": prev, "change_pct": _pct_change(cur, prev) if covered else None, "covered": covered}

    return {
        "period": {"start": start.date().isoformat(), "end": shown_end.isoformat(), "days": n_days, "group": group,
                   "prev_start": prev_start.date().isoformat(), "prev_end": (start - timedelta(days=1)).date().isoformat(),
                   "generated_at": now_cairo().isoformat(timespec="seconds")},
        "kpis": {
            "total_pieces": total_pieces, "stock_value": stock_value, "products_count": int(k[0]),
            "low_stock_count": int(k[3]), "out_of_stock_count": int(k[4]),
            "in": in_mv["cur"], "out": out_mv["cur"], "transfers": {"count": int(tr[0]), "qty": _f(tr[1], 3)},
        },
        "abc": {"basis": abc["basis"], "total_value": abc["total_value"], "products_count": abc["products_count"], "summary": abc["summary"],
                "top": abc["items"][:15], "chart": abc["chart"]},
        "dead": {**dead, "window_days": dead_days, "pct_of_stock": round(dead["total_value"] / stock_value * 100, 1) if stock_value > 0 else 0,
                 "top": [{"product_id": r[0], "name": r[1], "sku": r[2], "unit": r[3], "quantity": _f(r[4], 3), "price": _f(r[5]),
                          "value": _f(r[6]), "never_sold": not bool(r[7])} for r in dead_rows]},
        "low_stock": {"count": int(k[3]), "items": [{"product_id": r[0], "name": r[1], "sku": r[2], "unit": r[3], "quantity": _f(r[4], 3),
                                                      "min_quantity": _f(r[5], 3), "shortage": _f((r[5] or 0) - (r[4] or 0), 3)} for r in low_rows]},
        "top_suppliers": suppliers, "top_recipients": recipients,
        "warehouses": {"show": len(wh_items) > 1, "items": wh_items, "unassigned": unassigned},
        "trend": {
            "group": group, "series": series, "peak": peak, "trough": trough,
            "compare": {"out_value": cmp(out_mv["cur"]["value"], out_mv["prev"]["value"]), "out_qty": cmp(out_mv["cur"]["qty"], out_mv["prev"]["qty"]),
                        "in_value": cmp(in_mv["cur"]["value"], in_mv["prev"]["value"]), "in_qty": cmp(in_mv["cur"]["qty"], in_mv["prev"]["qty"]),
                        "transfers": cmp(int(tr[0]), int(tr[2]))},
            "yoy": yoy, "months_of_data": months_of_data, "seasonality_note": note, "stockout_alerts": alerts,
        },
    }

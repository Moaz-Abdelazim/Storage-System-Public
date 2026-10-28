// =====================================================================
//  executive-report.js — التقرير التنفيذي للإدارة (3 صفحات A4، ملونة).
//  بيجيب كل الأقسام من endpoint واحد (GET /api/analytics/executive-report) وبيرسم الشارتات SVG بكود محلي (من غير أي مكتبة).
//  الأرقام لاتينية، العملة جنيه (ج.م)، وتاريخ/وقت الطباعة بتوقيت القاهرة.
// =====================================================================
import { api, qs } from "./api.js";
import { guard } from "./auth.js";
import { API_BASE } from "./config.js";
import { getQuery, setQuery, todayISO } from "./utils.js";

const $ = (s) => document.querySelector(s);
const esc = (v) => String(v ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const num = (n, d = 3) => Number(n || 0).toLocaleString("en-US", { maximumFractionDigits: d });
const m2 = (n) => Number(n || 0).toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
const money = (n) => `<span class="num">${m2(n)}</span> ج.م`;
const compact = (n) => new Intl.NumberFormat("en-US", { notation: "compact", maximumFractionDigits: 1 }).format(n);
const dmy = (iso) => (iso ? `${iso.slice(8, 10)}/${iso.slice(5, 7)}/${iso.slice(0, 4)}` : "-");
const N = (v) => `<span class="num">${v}</span>`;
let uid = 0;

// تاريخ ووقت الطباعة بتوقيت القاهرة (أرقام لاتينية)
function cairoNow() {
  const p = Object.fromEntries(new Intl.DateTimeFormat("en-GB", { timeZone: "Africa/Cairo", year: "numeric", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", hourCycle: "h23" }).formatToParts(new Date()).map((x) => [x.type, x.value]));
  return `${p.day}/${p.month}/${p.year} ${p.hour}:${p.minute}`;
}

// ---------- أدوات الرسم ----------
function niceMax(v) {
  if (!(v > 0)) return 1;
  const p = Math.pow(10, Math.floor(Math.log10(v)));
  for (const m of [1, 1.5, 2, 2.5, 3, 4, 5, 6, 8, 10]) if (m * p >= v) return m * p;
  return 10 * p;
}
// ألوان الشارتات (نفس ألوان النظام): وارد أخضر، صادر برتقالي، الخط الأزرق، وفئات ABC (A أخضر / B كهرماني / C رمادي)
const C = { grid: "#cbd5e1", a: "#059669", b: "#f59e0b", c: "#94a3b8", out: "#ea580c", outSoft: "#fdba74", inn: "#059669", innSoft: "#6ee7b7", line: "#2563eb", guide: "#64748b" };
const defs = (id, color) => `<defs><pattern id="${id}" width="5" height="5" patternUnits="userSpaceOnUse" patternTransform="rotate(45)"><rect width="5" height="5" fill="#fff"/><line x1="0" y1="0" x2="0" y2="5" stroke="${color}" stroke-width="2.4"/></pattern></defs>`;
const emptyChart = (W, H, msg = "مفيش حركة في الفترة دي") => `<svg class="ch" viewBox="0 0 ${W} ${H}"><rect x="1" y="1" width="${W - 2}" height="${H - 2}" rx="6" fill="#f8fafc" stroke="${C.grid}" stroke-dasharray="4 3"/><text x="${W / 2}" y="${H / 2}" text-anchor="middle" font-size="14">${msg}</text></svg>`;
const gridLines = (m, W, H, ih, maxB, fs, right) => {
  let g = "";
  for (let i = 0; i <= 4; i++) {
    const y = m.t + ih * (1 - i / 4);
    g += `<line x1="${m.l}" x2="${W - m.r}" y1="${y}" y2="${y}" stroke="${C.grid}" stroke-width="${i === 0 ? 1.3 : 0.7}" ${i ? 'stroke-dasharray="3 3"' : ""}/>`;
    g += `<text x="${m.l - 4}" y="${y + fs / 3}" text-anchor="end" font-size="${fs}">${compact((maxB * i) / 4)}</text>`;
    if (right) g += `<text x="${W - m.r + 4}" y="${y + fs / 3}" text-anchor="start" font-size="${fs}">${right(i)}</text>`;
  }
  return g;
};

// أعمدة (قيمة) + خط (كمية) بمحورين
function barLine({ cats, W = 600, H = 170, fs = 11, barColor, line = true }) {
  const total = cats.reduce((s, c) => s + c.bar + c.line, 0);
  if (!cats.length || !(total > 0)) return emptyChart(W, H);
  const id = `h${++uid}`, m = { l: 48, r: line ? 44 : 10, t: 12, b: 24 }, iw = W - m.l - m.r, ih = H - m.t - m.b;
  const maxB = niceMax(Math.max(...cats.map((c) => c.bar))), maxL = niceMax(Math.max(...cats.map((c) => c.line)));
  const step = iw / cats.length, bw = Math.min(step * 0.62, 26), x = (i) => m.l + step * (i + 0.5);
  let g = defs(id, barColor) + gridLines(m, W, H, ih, maxB, fs, line ? (i) => compact((maxL * i) / 4) : null);
  cats.forEach((c, i) => {
    const h = ih * (c.bar / maxB);
    g += `<rect x="${x(i) - bw / 2}" y="${m.t + ih - h}" width="${bw}" height="${Math.max(h, c.bar > 0 ? 0.8 : 0)}" rx="2" fill="${c.partial ? `url(#${id})` : barColor}" stroke="${barColor}" stroke-width="0.8"/>`;
  });
  if (line) {
    const pts = cats.map((c, i) => `${x(i).toFixed(1)},${(m.t + ih * (1 - c.line / maxL)).toFixed(1)}`);
    g += `<polyline points="${pts.join(" ")}" fill="none" stroke="${C.line}" stroke-width="2" stroke-linejoin="round"/>`;
    pts.forEach((p) => { const [px, py] = p.split(","); g += `<circle cx="${px}" cy="${py}" r="2.8" fill="#fff" stroke="${C.line}" stroke-width="1.6"/>`; });
  }
  const every = Math.max(1, Math.ceil(cats.length / Math.floor(iw / (fs * 4.6))));
  cats.forEach((c, i) => { if (i % every === 0) g += `<text x="${x(i)}" y="${H - 8}" text-anchor="middle" font-size="${fs}">${c.label}</text>`; });
  return `<svg class="ch" viewBox="0 0 ${W} ${H}" role="img">${g}</svg>`;
}

// باريتو ABC: أعمدة ملونة حسب الفئة + خط النسبة التراكمية
function pareto(items, basis) {
  const W = 600, H = 200, fs = 15;
  if (!items.length) return emptyChart(W, H, "مفيش مبيعات في الفترة دي");
  const m = { l: 56, r: 50, t: 14, b: 30 }, iw = W - m.l - m.r, ih = H - m.t - m.b;
  const val = (i) => (basis === "quantity" ? i.quantity : i.value);
  const maxB = niceMax(Math.max(...items.map(val))), step = iw / items.length, bw = Math.min(step * 0.7, 18), x = (i) => m.l + step * (i + 0.5);
  let g = gridLines(m, W, H, ih, maxB, fs, (i) => `${i * 25}%`);
  for (const t of [80, 95]) { const y = m.t + ih * (1 - t / 100); g += `<line x1="${m.l}" x2="${W - m.r}" y1="${y}" y2="${y}" stroke="${C.guide}" stroke-width="1" stroke-dasharray="6 4"/>`; }
  const fill = { A: C.a, B: C.b, C: C.c };
  items.forEach((it, i) => {
    const h = ih * (val(it) / maxB);
    g += `<rect x="${x(i) - bw / 2}" y="${m.t + ih - h}" width="${bw}" height="${Math.max(h, 0.8)}" rx="2" fill="${fill[it.class]}"/>`;
    g += `<text x="${x(i)}" y="${H - 10}" text-anchor="middle" font-size="${fs - 4}">${it.rank}</text>`;
  });
  const pts = items.map((it, i) => `${x(i).toFixed(1)},${(m.t + ih * (1 - Math.min(it.cumulative_pct, 100) / 100)).toFixed(1)}`);
  g += `<polyline points="${pts.join(" ")}" fill="none" stroke="${C.line}" stroke-width="2.2" stroke-linejoin="round"/>`;
  pts.forEach((p) => { const [px, py] = p.split(","); g += `<circle cx="${px}" cy="${py}" r="3" fill="#fff" stroke="${C.line}" stroke-width="1.8"/>`; });
  return `<svg class="ch" viewBox="0 0 ${W} ${H}" role="img">${g}</svg>`;
}

// مقارنة شهرية: نفس الشهر السنة الحالية (برتقالي غامق) مقابل السنة السابقة (برتقالي فاتح)
function yoyChart(months) {
  const W = 600, H = 150, fs = 11;
  const id = `h${++uid}`, m = { l: 48, r: 10, t: 10, b: 24 }, iw = W - m.l - m.r, ih = H - m.t - m.b;
  const maxB = niceMax(Math.max(...months.map((x) => Math.max(x.out_value, x.prev_out_value || 0)), 0));
  if (months.every((x) => !x.out_value && !x.prev_out_value)) return emptyChart(W, H);
  const step = iw / months.length, bw = Math.min(step * 0.34, 18);
  let g = defs(id, C.out) + gridLines(m, W, H, ih, maxB, fs, null);
  months.forEach((mo, i) => {
    const cx = m.l + step * (i + 0.5), hc = ih * (mo.out_value / maxB), hp = ih * ((mo.prev_out_value || 0) / maxB);
    g += `<rect x="${cx - bw - 0.5}" y="${m.t + ih - hp}" width="${bw}" height="${Math.max(hp, mo.prev_out_value ? 0.8 : 0)}" rx="2" fill="${C.outSoft}"/>`;
    g += `<rect x="${cx + 0.5}" y="${m.t + ih - hc}" width="${bw}" height="${Math.max(hc, mo.out_value ? 0.8 : 0)}" rx="2" fill="${mo.partial ? `url(#${id})` : C.out}" stroke="${C.out}" stroke-width="0.8"/>`;
    g += `<text x="${cx}" y="${H - 8}" text-anchor="middle" font-size="${fs}">${mo.month.slice(5)}/${mo.month.slice(2, 4)}${mo.partial ? "*" : ""}</text>`;
  });
  return `<svg class="ch" viewBox="0 0 ${W} ${H}" role="img">${g}</svg>`;
}

// ---------- جداول وأجزاء ----------
function table(cols, rows, empty = "مفيش بيانات") {
  if (!rows.length) return `<div class="empty">${empty}</div>`;
  return `<table class="t"><colgroup>${cols.map((c) => `<col style="width:${c.w}">`).join("")}</colgroup>
    <thead><tr>${cols.map((c) => `<th>${c.h}</th>`).join("")}</tr></thead>
    <tbody>${rows.map((r, i) => `<tr>${cols.map((c) => `<td class="${c.k || ""}">${c.f(r, i)}</td>`).join("")}</tr>`).join("")}</tbody></table>`;
}
const chg = (c) => {
  if (!c) return "—";
  if (c.covered === false) return "لا مقارنة (بيانات الفترة السابقة ناقصة)";
  if (c.change_pct != null) return `${c.change_pct > 0 ? "↑" : c.change_pct < 0 ? "↓" : "="} ${N(Math.abs(c.change_pct) + "%")}`;
  return c.previous === 0 && c.current > 0 ? "جديد" : "—";
};
const sec = (n, title, body, small = "") => `<section class="sec"><h2><span>${n}) ${title}</span>${small ? `<small>${small}</small>` : ""}</h2>${body}</section>`;
const kpi = (l, v, s = "") => `<div class="kpi"><div class="l">${l}</div><div class="v">${v}</div><div class="s">${s || "&nbsp;"}</div></div>`;
const legend = (items) => `<div class="legend">${items.map(([css, t]) => `<span><i style="${css}"></i>${t}</span>`).join("")}</div>`;
const label = (key, group) => (group === "month" ? `${key.slice(5)}/${key.slice(2, 4)}` : `${key.slice(8)}/${key.slice(5, 7)}`);

// ---------- بناء الصفحات ----------
function header(d, company, first, page, total) {
  const p = d.period, logo = company && company.has_logo ? `<img class="logo" alt="" src="${API_BASE}/api/company/logo?v=${encodeURIComponent(company.logo_version || "")}">` : "";
  const name = esc((company && company.name) || "مخزني");
  const when = `طُبع: <span class="num printed-at">${cairoNow()}</span> (توقيت القاهرة)`;
  if (!first) return `<div class="rh small"><div class="t1">${name} — تقرير تنفيذي للإدارة</div><div class="meta">الفترة: ${N(dmy(p.start))} ← ${N(dmy(p.end))}</div></div>`;
  return `<div class="rh"><div class="co">${logo}<div><div class="t1">${name}</div><div class="t2">تقرير تنفيذي للإدارة — نظام مخزني</div></div></div>
    <div class="meta"><div>الفترة: ${N(dmy(p.start))} ← ${N(dmy(p.end))} (${N(p.days)} يوم)</div><div>${when}</div><div>التجميع الزمني: ${p.group === "week" ? "أسبوعي" : "شهري"}</div></div></div>`;
}

function page1(d) {
  const k = d.kpis, c = d.trend.compare, dead = d.dead, a = d.abc;
  const kp = `<div class="kpis">
    ${kpi("إجمالي القطع في المخزون", N(num(k.total_pieces)), "كل الأصناف المفعّلة")}
    ${kpi("قيمة المخزون", money(k.stock_value), "الكمية × سعر الصنف")}
    ${kpi("عدد الأصناف", N(num(k.products_count)), "الأصناف المفعّلة")}
    ${kpi("تحت الحد الأدنى", N(num(k.low_stock_count)), `منها نفد تمامًا: ${N(num(k.out_of_stock_count))}`)}
    ${kpi("وارد الفترة", money(k.in.value), `${N(k.in.invoices)} فاتورة · ${N(num(k.in.qty))} قطعة · ${chg(c.in_value)}`)}
    ${kpi("صادر الفترة", money(k.out.value), `${N(k.out.invoices)} فاتورة · ${N(num(k.out.qty))} قطعة · ${chg(c.out_value)}`)}
    ${kpi("حركات النقل بين المواقع", N(num(k.transfers.count)), `${N(num(k.transfers.qty))} قطعة · ${chg(c.transfers)}`)}
    ${kpi("المخزون الراكد (مجمّد)", money(dead.total_value), `${N(dead.count)} صنف · ${N(dead.pct_of_stock + "%")} من القيمة`)}
  </div><div class="s" style="font-size:7.8pt;margin-top:1mm">النسب بجانب الوارد/الصادر/النقل مقارنة بالفترة السابقة بنفس الطول (${N(dmy(d.period.prev_start))} ← ${N(dmy(d.period.prev_end))}).</div>`;

  const s = a.summary, tv = a.basis === "quantity" ? Object.values(s).reduce((x, y) => x + y.qty, 0) : a.total_value;
  const cls = (L, name) => {
    const x = s[L], v = a.basis === "quantity" ? x.qty : x.value;
    return `<div><b>${L}</b> — ${name}<br>${N(x.count)} صنف · ${a.basis === "quantity" ? N(num(x.qty)) + " قطعة" : money(x.value)}<br><span class="num">${tv ? (v / tv * 100).toFixed(1) : "0.0"}%</span> من ${a.basis === "quantity" ? "كمية" : "قيمة"} المبيعات</div>`;
  };
  const abcBody = `<div class="cols" style="grid-template-columns:1.9fr 1fr;align-items:start">
      <div>${pareto(a.chart, a.basis)}${legend([[`background:${C.a}`, "فئة A (حتى 80%)"], [`background:${C.b}`, "فئة B (حتى 95%)"], [`background:${C.c}`, "فئة C"], [`background:${C.line};height:.7mm;vertical-align:1mm`, "الخط = النسبة التراكمية"]])}</div>
      <div class="sum3" style="grid-template-columns:1fr;margin:0">${cls("A", "الأهم")}${cls("B", "المتوسط")}${cls("C", "الأقل")}</div></div>
    <div class="chart-t">أهم 15 صنف (الأساس: ${a.basis === "quantity" ? "الكمية المباعة" : "قيمة المبيعات"})</div>
    ${table([
      { h: "#", w: "7mm", k: "c", f: (r) => r.rank }, { h: "الصنف", w: "auto", f: (r) => esc(r.name) }, { h: "فئة", w: "10mm", k: "c cls", f: (r) => r.class },
      { h: "الكمية", w: "20mm", k: "n", f: (r) => num(r.quantity) }, { h: "القيمة (ج.م)", w: "28mm", k: "n", f: (r) => m2(r.value) },
      { h: "النسبة", w: "16mm", k: "n", f: (r) => r.share_pct + "%" }, { h: "التراكمي", w: "17mm", k: "n", f: (r) => r.cumulative_pct + "%" },
    ], a.top, "مفيش مبيعات في الفترة دي")}`;
  return sec(1, "ملخص تنفيذي", kp) + sec(2, "تحليل ABC (باريتو)", abcBody, `${N(a.products_count)} صنف مُباع`);
}

function page2(d) {
  const dead = d.dead, ls = d.low_stock, W = d.warehouses;
  const deadBody = `<div class="sum3"><div>القيمة المجمّدة<br><b>${money(dead.total_value)}</b></div><div>عدد الأصناف الراكدة<br><b>${N(dead.count)}</b> (${N(num(dead.total_quantity))} قطعة)</div><div>لم تُبَع أبدًا<br><b>${N(dead.never_sold)}</b> صنف</div></div>
    ${table([{ h: "الصنف", w: "auto", f: (r) => esc(r.name) }, { h: "الكمية", w: "22mm", k: "n", f: (r) => num(r.quantity) }, { h: "سعر الوحدة", w: "26mm", k: "n", f: (r) => m2(r.price) },
      { h: "القيمة المجمّدة", w: "32mm", k: "n", f: (r) => m2(r.value) }, { h: "ملاحظة", w: "24mm", k: "c", f: (r) => (r.never_sold ? "لم يُباع أبدًا" : "") }], dead.top.slice(0, 6), "مفيش أصناف راكدة")}`;
  const lowBody = table([{ h: "#", w: "7mm", k: "c", f: (r, i) => i + 1 }, { h: "الصنف", w: "auto", f: (r) => esc(r.name) }, { h: "الوحدة", w: "18mm", k: "c", f: (r) => esc(r.unit || "") },
    { h: "الكمية الحالية", w: "26mm", k: "n", f: (r) => num(r.quantity) }, { h: "الحد الأدنى", w: "22mm", k: "n", f: (r) => num(r.min_quantity) }, { h: "العجز", w: "22mm", k: "n", f: (r) => num(r.shortage) }], ls.items, "مفيش أصناف تحت الحد الأدنى");
  const party = (rows, what) => table([{ h: "#", w: "6mm", k: "c", f: (r, i) => i + 1 }, { h: what, w: "auto", f: (r) => esc(r.name) }, { h: "الكمية", w: "17mm", k: "n", f: (r) => num(r.quantity) }, { h: "القيمة (ج.م)", w: "25mm", k: "n", f: (r) => m2(r.value) }], rows.slice(0, 7), "مفيش حركة في الفترة");
  const partiesBody = `<div class="cols"><div><div class="chart-t">أفضل الموردين (وارد)</div>${party(d.top_suppliers, "المورد")}</div><div><div class="chart-t">أفضل الجهات المستلمة (صادر)</div>${party(d.top_recipients, "الجهة")}</div></div>`;
  let whBody;
  if (!W.show) whBody = `<div class="empty">مخزن واحد فقط — قيمة المخزون كلها: ${money(d.kpis.stock_value)}</div>`;
  else {
    let rows = [...W.items].sort((a, b) => b.value - a.value);
    const extra = rows.slice(6); rows = rows.slice(0, 6);
    if (extra.length) rows.push({ name: `مخازن أخرى (${extra.length})`, code: "", value: extra.reduce((s, x) => s + x.value, 0), quantity: extra.reduce((s, x) => s + x.quantity, 0), pct_value: +extra.reduce((s, x) => s + x.pct_value, 0).toFixed(1), other: true });
    if (W.unassigned) rows.push({ name: "غير موزّع على مواقع", code: "", ...W.unassigned, hatch: true });
    const bar = (r) => `<div class="hb"><span class="hb-l">${r.code ? esc(r.code) + " — " : ""}${esc(r.name)}</span><span class="hb-bar"><i class="${r.hatch || r.other ? "hatch" : ""}" style="width:${Math.min(100, r.pct_value)}%"></i></span><span class="hb-v">${N(r.pct_value + "%")} · ${money(r.value)}</span></div>`;
    whBody = rows.map(bar).join("");
  }
  return sec(3, "المخزون الراكد", deadBody, `بدون صادر آخر ${N(dead.window_days)} يوم`) + sec(4, "أصناف تحت الحد الأدنى", lowBody, `الإجمالي ${N(num(ls.count))} صنف · أكبر 15 عجزًا`)
    + sec(5, "أفضل الموردين والجهات المستلمة", partiesBody) + sec(6, "قيمة المخزون لكل مخزن", whBody);
}

function page3(d) {
  const t = d.trend, c = t.compare, g = t.group, unit = g === "week" ? "أسبوع" : "شهر";
  const cats = (v, q) => t.series.map((s) => ({ label: label(s.period, g), bar: s[v], line: s[q], partial: s.partial }));
  const cmpBox = (l, cur, cm, isMoney) => `<div><div class="l">${l}</div><div class="v">${isMoney ? money(cur) : N(num(cur))}</div><div class="s">${chg(cm)} · السابقة: ${isMoney ? money(cm.previous) : N(num(cm.previous))}</div></div>`;
  const plabel = (s) => (g === "week" ? `أسبوع يبدأ ${N(dmy(s.period))}` : `شهر ${N(s.period.slice(5) + "/" + s.period.slice(0, 4))}`);
  const pk = t.peak && t.trough && (t.peak.out_value > 0 || t.trough.out_value > 0)
    ? `<div class="note" style="margin:0 0 1.5mm"><b>أعلى ${unit} صادر:</b> ${plabel(t.peak)} — ${money(t.peak.out_value)} (${N(num(t.peak.out_qty))} قطعة) &nbsp;|&nbsp; <b>أقل ${unit} صادر:</b> ${plabel(t.trough)} — ${money(t.trough.out_value)} (${N(num(t.trough.out_qty))} قطعة)</div>` : "";
  const hasPartial = t.series.some((s) => s.partial);
  let yoyHtml = "";
  if (t.yoy.available) {
    const ms = t.yoy.months, done = ms.filter((m) => m.change_pct != null && !m.partial);
    const best = done.length ? done.reduce((a, b) => (b.change_pct > a.change_pct ? b : a)) : null, worst = done.length ? done.reduce((a, b) => (b.change_pct < a.change_pct ? b : a)) : null;
    yoyHtml = `<div class="chart-t">مقارنة قيمة الصادر الشهري بنفس الشهر في السنة السابقة (آخر 12 شهر)</div>${yoyChart(ms)}
      ${legend([[`background:${C.out}`, "الشهر الحالي"], [`background:${C.outSoft}`, "نفس الشهر السنة السابقة"]])}
      ${best ? `<div style="font-size:8.6pt;margin-top:.8mm">أكبر زيادة: شهر ${N(best.month.slice(5) + "/" + best.month.slice(0, 4))} (${N("+" + best.change_pct + "%")}) · أكبر نقص: شهر ${N(worst.month.slice(5) + "/" + worst.month.slice(0, 4))} (${N(worst.change_pct + "%")}) ${ms.some((m) => m.partial) ? " · * شهر ناقص" : ""}</div>` : ""}`;
  }
  const alerts = t.stockout_alerts.length ? `<div class="note"><b>تنبيه نفاد:</b> أصناف فئة A كميتها الحالية صفر أو قريبة منه — ركود الصادر ممكن يكون سببه نفاد الصنف مش قلة الطلب: ${t.stockout_alerts.slice(0, 6).map((a) => `${esc(a.name)} (${N(num(a.quantity))})`).join("، ")}${t.stockout_alerts.length > 6 ? ` و${N(t.stockout_alerts.length - 6)} أصناف أخرى` : ""}.</div>` : "";
  const body = `<div class="cmp">${cmpBox("قيمة الصادر", d.kpis.out.value, c.out_value, true)}${cmpBox("كمية الصادر", d.kpis.out.qty, c.out_qty, false)}${cmpBox("قيمة الوارد", d.kpis.in.value, c.in_value, true)}${cmpBox("كمية الوارد", d.kpis.in.qty, c.in_qty, false)}</div>
    ${pk}
    <div class="chart-t">الصادر عبر الزمن (تجميع ${g === "week" ? "أسبوعي" : "شهري"})</div>${barLine({ cats: cats("out_value", "out_qty"), barColor: C.out })}
    ${legend([[`background:${C.out}`, "قيمة الصادر (ج.م) — المحور الأيسر"], [`background:${C.line};height:.7mm;vertical-align:1mm`, "كمية الصادر — المحور الأيمن (الخط)"]])}
    <div class="chart-t">الوارد عبر الزمن</div>${barLine({ cats: cats("in_value", "in_qty"), barColor: C.inn })}
    ${legend([[`background:${C.inn}`, "قيمة الوارد (ج.م)"], [`background:${C.line};height:.7mm;vertical-align:1mm`, "كمية الوارد (الخط)"]])}
    ${hasPartial ? `<div style="font-size:7.8pt;margin-top:.6mm">* الأعمدة المهشّرة = ${unit} ناقص (بداية أو نهاية الفترة) ولا يدخل في أعلى/أقل ${unit}.</div>` : ""}
    ${yoyHtml}
    ${t.seasonality_note ? `<div class="note"><b>ملاحظة:</b> ${esc(t.seasonality_note)}</div>` : ""}${alerts}`;
  return sec(7, "اتجاه الحركة عبر الزمن", body, `المقارنة مع الفترة السابقة بنفس الطول`);
}

function render(d, company) {
  const pages = [page1, page2, page3];
  $("#sheets").innerHTML = pages.map((fn, i) => `<section class="sheet">${header(d, company, i === 0)}${fn(d)}<div class="pgno"><span>تقرير تنفيذي للإدارة</span><span>صفحة ${N(i + 1)} من ${N(pages.length)}</span></div></section>`).join("");
  const over = [...document.querySelectorAll(".sheet")].some((s) => s.scrollHeight > s.clientHeight + 1);
  document.body.dataset.overflow = over ? "1" : "0";
  document.body.dataset.ready = "1";
}

// ---------- الفترة والتشغيل ----------
const PRESETS = [7, 30, 90, 365];
function params() {
  const f = getQuery("date_from"), t = getQuery("date_to");
  return f && t ? { date_from: f, date_to: t } : { days: Number(getQuery("days")) || 30 };
}
function paintControls() {
  const p = params();
  $("#presets").innerHTML = PRESETS.map((n) => `<button type="button" data-d="${n}" class="${p.days === n ? "on" : ""}">${n === 365 ? "سنة" : `${n} يوم`}</button>`).join("");
  $("#presets").querySelectorAll("button").forEach((b) => b.addEventListener("click", () => { setQuery({ days: b.dataset.d, date_from: "", date_to: "" }); load(); }));
  $("#dFrom").value = p.date_from || ""; $("#dTo").value = p.date_to || ""; $("#dTo").max = todayISO();
}

let company = null;
async function load() {
  paintControls();
  const st = $("#state"), sheets = $("#sheets"), btn = $("#printBtn");
  st.hidden = false; st.textContent = "جاري تجهيز التقرير..."; sheets.hidden = true; btn.disabled = true; delete document.body.dataset.ready;
  try {
    const [d, c] = await Promise.all([api.get(`/api/analytics/executive-report${qs(params())}`), company ? Promise.resolve(company) : api.get("/api/company").catch(() => null)]);
    company = c;
    if (document.fonts) await document.fonts.ready;
    st.hidden = true; sheets.hidden = false;
    render(d, company);
    document.title = `تقرير تنفيذي ${d.period.start} إلى ${d.period.end}`;
    btn.disabled = false;
  } catch (e) {
    st.hidden = false; st.textContent = e.status === 401 ? "انتهت الجلسة." : (e.message || "تعذّر تحميل التقرير");
  }
}

if (guard({ adminOnly: true })) {
  $("#applyRange").addEventListener("click", () => {
    const f = $("#dFrom").value, t = $("#dTo").value;
    if (!f || !t) return alert("اختار تاريخ البداية والنهاية");
    if (f > t) return alert("تاريخ البداية بعد تاريخ النهاية");
    setQuery({ days: "", date_from: f, date_to: t }); load();
  });
  $("#printBtn").addEventListener("click", () => window.print());
  window.addEventListener("beforeprint", () => document.querySelectorAll(".printed-at").forEach((el) => { el.textContent = cairoNow(); }));
  load();
}

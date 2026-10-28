// =====================================================================
//  reports.js — التقارير المتقدمة (أدمن): كل تبويب بيتحمّل لما تفتحه (lazy) وبيتحدّث لما تغيّر الفترة.
// =====================================================================
import { api, qs } from "./api.js";
import { num, money, pct, compact, fmtDateTime, errMsg, toast, downloadCsv } from "./utils.js";

const TABS = [
  { key: "overview", label: "نظرة عامة", icon: "fa-gauge-high" },
  { key: "abc", label: "تحليل ABC", icon: "fa-ranking-star" },
  { key: "dead", label: "الأصناف الراكدة", icon: "fa-box-archive" },
  { key: "coverage", label: "إعادة الطلب", icon: "fa-cart-flatbed" },
  { key: "margin", label: "الربحية", icon: "fa-coins" },
  { key: "prices", label: "أسعار الموردين", icon: "fa-tags" },
  { key: "employees", label: "الموظفين", icon: "fa-user-clock" },
  { key: "adjustments", label: "التعديلات", icon: "fa-shield-halved" },
];

const DAY_NAMES = ["الاثنين", "الثلاثاء", "الأربعاء", "الخميس", "الجمعة", "السبت", "الأحد"]; // weekday() بايثون: الاثنين = 0
const IN_COLOR = "#10B981";
const OUT_COLOR = "#F97316";

const charts = {};
function colors() {
  const dark = document.documentElement.classList.contains("dark");
  return { grid: dark ? "rgba(148,163,184,.12)" : "rgba(148,163,184,.25)", tick: dark ? "#94a3b8" : "#64748b" };
}
function makeChart(key, canvas, config) {
  if (!canvas || !window.Chart) return;
  window.Chart.defaults.font.family = "Tajawal, sans-serif";
  window.Chart.defaults.font.size = 12;
  if (charts[key]) charts[key].destroy();
  charts[key] = new window.Chart(canvas, config);
}
const baseScales = (c, extra = {}) => ({
  x: { grid: { display: false }, border: { display: false }, ticks: { color: c.tick, maxRotation: 0, autoSkip: true, maxTicksLimit: 12 }, ...(extra.x || {}) },
  y: { beginAtZero: true, border: { display: false }, grid: { color: c.grid, borderDash: [4, 4] }, ticks: { color: c.tick, maxTicksLimit: 5, callback: (v) => compact(v) }, ...(extra.y || {}) },
});
const tooltip = { rtl: true, textDirection: "rtl", padding: 10, cornerRadius: 10 };

const CSV = {
  abc: { name: "abc", cols: [["الصنف", "name"], ["SKU", "sku"], ["الفئة", "class"], ["الكمية", "quantity"], ["القيمة", "value"], ["النسبة %", "share_pct"], ["التراكمي %", "cumulative_pct"]] },
  dead: { name: "dead-stock", cols: [["الصنف", "name"], ["SKU", "sku"], ["الرصيد", "quantity"], ["الوحدة", "unit"], ["السعر", "price"], ["القيمة", "value"], ["أيام من آخر صادر", "days_since_last_out"]] },
  coverage: { name: "reorder", cols: [["الصنف", "name"], ["SKU", "sku"], ["الرصيد", "quantity"], ["متوسط الصرف/يوم", "avg_daily"], ["أيام الكفاية", "days_of_cover"], ["الحالة", "status"], ["الكمية المقترحة للطلب", "suggested_order"], ["الوحدة", "unit"]] },
  margin: { name: "margin", cols: [["الصنف", "name"], ["SKU", "sku"], ["الكمية المباعة", "quantity"], ["الإيراد", "revenue"], ["متوسط التكلفة", "avg_cost"], ["الربح", "profit"], ["الهامش %", "margin_pct"]] },
  prices: { name: "price-variance", cols: [["الصنف", "name"], ["أقل سعر", "min_price"], ["أعلى سعر", "max_price"], ["آخر سعر", "last_price"], ["التذبذب %", "spread_pct"], ["أرخص مورد", "best_supplier"], ["التوفير المحتمل", "potential_saving"]] },
  employees: { name: "employees", cols: [["المستخدم", "username"], ["فواتير وارد", "in_invoices"], ["كمية وارد", "in_qty"], ["فواتير صادر", "out_invoices"], ["كمية صادر", "out_qty"], ["نقل", "transfers"], ["تعديلات", "adjustments"], ["إجمالي العمليات", "operations"]] },
  adjustments: { name: "adjustments", cols: [["الصنف", "name"], ["قبل", "old_quantity"], ["بعد", "new_quantity"], ["الفرق", "difference"], ["الأثر المالي", "value_impact"], ["بواسطة", "user"], ["السبب", "reason"], ["التاريخ", "date"], ["مشتبه", "flagged"]] },
};

document.addEventListener("alpine:init", () => {
  window.Alpine.data("reports", () => ({
    num, money, pct, fmtDateTime,
    tabs: TABS, tab: location.hash.slice(1) || "overview", days: 90, leadDays: 14,
    periods: [{ days: 30, label: "30 يوم" }, { days: 90, label: "90 يوم" }, { days: 180, label: "6 شهور" }, { days: 365, label: "سنة" }],
    d: {}, st: {}, err: {}, loadedFor: {}, q: {}, open: {}, busy: false, busyTab: {}, page: {}, size: 10,
    adjFlagged: false, covStatus: "", marginSort: "profit", priceSort: "saving",
    covStatuses: [{ key: "", label: "الكل" }, { key: "critical", label: "حرج" }, { key: "warning", label: "تحذير" }, { key: "ok", label: "آمن" }],
    kpis: null, trend: null, heat: null, metric: "value", group: "day",
    metrics: [{ key: "value", label: "القيمة" }, { key: "qty", label: "الكمية" }],
    groups: [{ key: "day", label: "يومي" }, { key: "week", label: "أسبوعي" }, { key: "month", label: "شهري" }],
    dayNames: DAY_NAMES, dayOrder: [5, 6, 0, 1, 2, 3, 4],

    init() {
      if (!TABS.some((t) => t.key === this.tab)) this.tab = "overview";
      TABS.forEach((t) => { this.st[t.key] = "idle"; this.err[t.key] = ""; this.q[t.key] = ""; this.page[t.key] = 1; this.busyTab[t.key] = false; });
      window.addEventListener("app-theme", () => this.redraw());
      this.ensure(this.tab);
    },

    go(key) { this.tab = key; history.replaceState(null, "", `#${key}`); this.ensure(key); },
    setDays(n) { this.days = n; this.loadedFor = {}; TABS.forEach((t) => (this.page[t.key] = 1)); this.ensure(this.tab, true); },
    refresh() { api.invalidate(); this.loadedFor = {}; this.ensure(this.tab, true); },
    setGroup(g) { this.group = g; this.loadTrend(); },
    toggleOpen(id) { this.open[id] = !this.open[id]; },

    // ---------- التحميل ----------
    // كل تبويب جدولي بيطلب صفحة واحدة بس من السيرفر (ترقيم + بحث + ترتيب في قاعدة البيانات)
    endpoint(key, extra = {}) {
      const ep = { abc: "abc", dead: "dead-stock", coverage: "coverage", margin: "margin", prices: "price-variance", employees: "employees", adjustments: "adjustments" }[key];
      const params = {
        days: key === "coverage" ? Math.min(this.days, 365) : this.days,
        page: this.page[key], page_size: this.size, search: (this.q[key] || "").trim(), ...extra,
      };
      if (key === "coverage") { params.lead_days = this.leadDays; params.status = this.covStatus; }
      if (key === "margin") params.sort = this.marginSort;
      if (key === "prices") params.sort = this.priceSort;
      if (key === "adjustments" && this.adjFlagged) params.flagged = 1;
      return `/api/analytics/${ep}${qs(params)}`;
    },

    async ensure(key, force = false) {
      const stamp = `${this.days}|${this.leadDays}`;
      if (!force && this.loadedFor[key] === stamp && this.st[key] === "ok") { this.$nextTick(() => this.redraw()); return; }
      const first = !this.d[key] && key !== "overview";
      if (first || key === "overview") { this.st[key] = "loading"; this.err[key] = ""; } else this.busyTab[key] = true;
      this.busy = true;
      try {
        if (key === "overview") await this.loadOverview();
        else this.d[key] = await api.get(this.endpoint(key));
        this.loadedFor[key] = stamp; this.st[key] = "ok";
        await this.$nextTick(); this.redraw();
      } catch (e) {
        if (first || key === "overview") { this.st[key] = "error"; this.err[key] = errMsg(e); } else toast(errMsg(e), "error");
      }
      this.busyTab[key] = false; this.busy = false;
    },

    goPage(key, page) { this.page[key] = page; this.loadedFor[key] = null; this.ensure(key, true); },
    onSearch(key) { this.goPage(key, 1); },

    // تصدير CSV: طلب واحد صريح بـ export=1 (حتى 5000 صف) بدل تصدير الصفحة الحالية بس
    async exportCsv(key) {
      this.busy = true;
      try {
        const r = await api.get(this.endpoint(key, { export: 1, page: 1, page_size: 100 }));
        const c = CSV[key];
        if (!r.items.length) { toast("مفيش بيانات للتصدير", "error"); }
        else downloadCsv(`${c.name}-${this.days}d.csv`, c.cols.map(([label, k]) => ({ label, key: k })), r.items);
      } catch (e) { toast(errMsg(e), "error"); }
      this.busy = false;
    },

    async loadOverview() {
      const [k, t, h] = await Promise.all([
        api.get(`/api/analytics/kpis?days=${this.days}`, { ttl: 30_000 }),
        api.get(`/api/analytics/trend?days=${this.days}&group=${this.group}`, { ttl: 30_000 }),
        api.get(`/api/analytics/heatmap?days=${this.days}`, { ttl: 30_000 }),
      ]);
      this.kpis = k; this.trend = t; this.heat = h;
    },
    async loadTrend() {
      try { this.trend = await api.get(`/api/analytics/trend?days=${this.days}&group=${this.group}`, { ttl: 30_000 }); this.drawTrend(); }
      catch (e) { toast(errMsg(e), "error"); }
    },

    // ---------- عرض ----------
    get kpiCards() {
      const k = this.kpis; if (!k) return [];
      return [
        { label: "قيمة المخزون الحالية", value: money(k.stock_value), sub: "بأسعار المنتجات", icon: "fa-vault", tone: "bg-brand-500/10 text-brand-600" },
        { label: "قيمة الصادر", value: money(k.out_value), sub: `آخر ${k.days} يوم · ${num(k.out_qty)} قطعة`, icon: "fa-arrow-up", tone: "bg-orange-500/10 text-orange-600" },
        { label: "قيمة الوارد", value: money(k.in_value), sub: `آخر ${k.days} يوم`, icon: "fa-arrow-down", tone: "bg-emerald-500/10 text-emerald-600" },
        { label: "معدل دوران المخزون", value: k.turnover != null ? `${num(k.turnover, 2)}×` : "—", sub: k.days_of_inventory != null ? `المخزون بيكفي ~${num(k.days_of_inventory, 0)} يوم` : "مفيش صادر كفاية", icon: "fa-arrows-rotate", tone: "bg-blue-500/10 text-blue-600" },
        { label: "مخزون راكد", value: money(k.dead_stock_value), sub: `${num(k.dead_stock_count)} صنف · ${pct(k.dead_stock_pct)} من المخزون`, icon: "fa-box-archive", tone: "bg-amber-500/10 text-amber-600" },
        { label: "تحت الحد الأدنى", value: num(k.low_stock_count), sub: `منهم ${num(k.out_of_stock_count)} نفد تمامًا`, icon: "fa-triangle-exclamation", tone: "bg-red-500/10 text-red-600" },
        { label: "تعديلات الجرد", value: num(k.adjustments_count), sub: `صافي الأثر ${money(k.adjustments_net_value)}`, icon: "fa-pen-to-square", tone: "bg-violet-500/10 text-violet-600" },
        { label: "فترة التحليل", value: `${k.days} يوم`, sub: "غيّرها من الأعلى", icon: "fa-calendar-days", tone: "bg-slate-500/10 text-slate-600" },
      ];
    },
    classTone(c) { return c === "A" ? "bg-brand-500/10 text-brand-600 dark:text-brand-300" : c === "B" ? "bg-blue-500/10 text-blue-600 dark:text-blue-300" : "bg-slate-500/10 text-slate-600 dark:text-slate-300"; },
    hourLabel(h) { return `${h % 12 === 0 ? 12 : h % 12} ${h < 12 ? "ص" : "م"}`; },
    heatOpacity(d, h) {
      if (!this.heat) return 0.05;
      const max = Math.max(...this.heat.grid.flat(), 1);
      const v = this.heat.grid[d][h];
      return v ? Math.max(0.15, v / max) : 0.06;
    },
    // ---------- الرسوم ----------
    redraw() {
      if (this.tab === "overview") this.drawTrend();
      if (this.tab === "abc") this.drawAbc();
      if (this.tab === "employees") this.drawEmployees();
    },

    drawTrend() {
      if (!this.trend) return;
      const c = colors(); const byValue = this.metric === "value";
      const items = this.trend.items;
      const label = (p) => (this.group === "month" ? p : new Date(`${p}T12:00:00`).toLocaleDateString("ar-EG-u-nu-latn", { day: "numeric", month: "short" }));
      makeChart("trend", this.$refs.trendCanvas, {
        type: "bar",
        data: { labels: items.map((i) => label(i.period)), datasets: [
          { label: "وارد", data: items.map((i) => (byValue ? i.in_value : i.in_qty)), backgroundColor: IN_COLOR, borderRadius: 5, maxBarThickness: 22 },
          { label: "صادر", data: items.map((i) => (byValue ? i.out_value : i.out_qty)), backgroundColor: OUT_COLOR, borderRadius: 5, maxBarThickness: 22 },
        ] },
        options: { responsive: true, maintainAspectRatio: false, interaction: { mode: "index", intersect: false },
          plugins: { legend: { position: "top", align: "end", labels: { usePointStyle: true, boxWidth: 8, color: c.tick } }, tooltip: { ...tooltip, callbacks: { label: (x) => ` ${x.dataset.label}: ${byValue ? money(x.parsed.y) : num(x.parsed.y)}` } } },
          scales: baseScales(c) },
      });
    },

    drawAbc() {
      const a = this.d.abc; if (!a) return;
      const c = colors(); const top = a.chart;
      makeChart("abc", this.$refs.abcCanvas, {
        data: { labels: top.map((i) => (i.name.length > 14 ? i.name.slice(0, 14) + "…" : i.name)), datasets: [
          { type: "bar", label: "النسبة %", data: top.map((i) => i.share_pct), backgroundColor: top.map((i) => (i.class === "A" ? IN_COLOR : i.class === "B" ? "#3B82F6" : "#94A3B8")), borderRadius: 4, yAxisID: "y" },
          { type: "line", label: "التراكمي %", data: top.map((i) => i.cumulative_pct), borderColor: OUT_COLOR, backgroundColor: OUT_COLOR, tension: 0.3, pointRadius: 3, yAxisID: "y1" },
        ] },
        options: { responsive: true, maintainAspectRatio: false, interaction: { mode: "index", intersect: false },
          plugins: { legend: { position: "top", align: "end", labels: { usePointStyle: true, boxWidth: 8, color: c.tick } }, tooltip: { ...tooltip, callbacks: { label: (x) => ` ${x.dataset.label}: ${pct(x.parsed.y)}` } } },
          scales: { x: { grid: { display: false }, ticks: { color: c.tick, maxRotation: 60, minRotation: 40, font: { size: 10 } } }, y: { beginAtZero: true, grid: { color: c.grid, borderDash: [4, 4] }, ticks: { color: c.tick, callback: (v) => v + "%" } }, y1: { position: "right", min: 0, max: 100, grid: { display: false }, ticks: { color: c.tick, callback: (v) => v + "%" } } } },
      });
    },

    drawEmployees() {
      const e = this.d.employees; if (!e) return;
      const c = colors(); const items = e.chart;
      const ds = (label, key, color) => ({ label, data: items.map((i) => i[key]), backgroundColor: color, borderRadius: 3, maxBarThickness: 36 });
      makeChart("emp", this.$refs.empCanvas, {
        type: "bar",
        data: { labels: items.map((i) => i.username), datasets: [ds("فواتير وارد", "in_invoices", IN_COLOR), ds("فواتير صادر", "out_invoices", OUT_COLOR), ds("نقل", "transfers", "#3B82F6"), ds("تعديلات", "adjustments", "#8B5CF6")] },
        options: { responsive: true, maintainAspectRatio: false, interaction: { mode: "index", intersect: false },
          plugins: { legend: { position: "top", align: "end", labels: { usePointStyle: true, boxWidth: 8, color: c.tick } }, tooltip },
          scales: { x: { stacked: true, grid: { display: false }, ticks: { color: c.tick } }, y: { stacked: true, beginAtZero: true, grid: { color: c.grid, borderDash: [4, 4] }, ticks: { color: c.tick, precision: 0 } } } },
      });
    },
  }));
});

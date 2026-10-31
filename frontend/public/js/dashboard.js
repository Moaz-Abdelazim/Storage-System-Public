// =====================================================================
//  dashboard.js — لوحة القيادة (index.html)
//  كل ودجت بتتحمّل لوحدها (لو واحدة فشلت الباقي يشتغل) وفيها skeleton وزر إعادة المحاولة.
// =====================================================================
import { api } from "./api.js";
import { can, getUser } from "./auth.js";
import { num, money, fmtDateTime, longToday, errMsg } from "./utils.js";

const PALETTE = ["#10B981", "#3B82F6", "#F59E0B", "#8B5CF6", "#EC4899", "#94A3B8"];
const IN_COLOR = "#10B981";
const OUT_COLOR = "#F97316";

const TONES = {
  blue:  { card: "border-blue-100 bg-blue-50/60 dark:border-blue-500/15 dark:bg-blue-500/5", chip: "bg-blue-100 text-blue-600 dark:bg-blue-500/15 dark:text-blue-300" },
  amber: { card: "border-amber-100 bg-amber-50/60 dark:border-amber-500/15 dark:bg-amber-500/5", chip: "bg-amber-100 text-amber-600 dark:bg-amber-500/15 dark:text-amber-300" },
  red:   { card: "border-red-100 bg-red-50/60 dark:border-red-500/15 dark:bg-red-500/5", chip: "bg-red-100 text-red-600 dark:bg-red-500/15 dark:text-red-300" },
  brand: { card: "border-brand-100 bg-brand-50/60 dark:border-brand-500/15 dark:bg-brand-500/5", chip: "bg-brand-100 text-brand-600 dark:bg-brand-500/15 dark:text-brand-300" },
};

// Chart objects برّه Alpine (الـ reactive proxy بيبوّظ Chart.js)
let flowChart = null;
let distChart = null;

function themeColors() {
  const dark = document.documentElement.classList.contains("dark");
  return {
    grid: dark ? "rgba(148,163,184,.12)" : "rgba(148,163,184,.25)",
    tick: dark ? "#94a3b8" : "#64748b",
    surface: dark ? "#18202c" : "#ffffff",
  };
}

function setupChartDefaults() {
  if (!window.Chart) return false;
  window.Chart.defaults.font.family = "Tajawal, sans-serif";
  window.Chart.defaults.font.size = 12;
  return true;
}

// تسمية سطرين (اليوم فوق والشهر تحت) عشان ماتتميّلش على الموبايل
const dayLabel = (iso) => {
  const d = new Date(`${iso}T12:00:00`);
  return [String(d.getDate()), d.toLocaleDateString("ar-EG-u-nu-latn", { month: "short" })];
};

function drawFlow(canvas, rows) {
  if (!canvas || !setupChartDefaults()) return;
  const t = themeColors();
  if (flowChart) flowChart.destroy();
  flowChart = new window.Chart(canvas, {
    type: "bar",
    data: {
      labels: rows.map((r) => dayLabel(r.date)),
      datasets: [
        { label: "وارد", data: rows.map((r) => r.inQty), backgroundColor: IN_COLOR, borderRadius: 6, borderSkipped: false, maxBarThickness: 20 },
        { label: "صادر", data: rows.map((r) => r.outQty), backgroundColor: OUT_COLOR, borderRadius: 6, borderSkipped: false, maxBarThickness: 20 },
      ],
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      interaction: { mode: "index", intersect: false },
      plugins: {
        legend: { display: false },
        tooltip: {
          rtl: true, textDirection: "rtl", padding: 10, cornerRadius: 10,
          callbacks: { label: (c) => ` ${c.dataset.label}: ${num(c.parsed.y)}` },
        },
      },
      scales: {
        x: { grid: { display: false }, border: { display: false }, ticks: { color: t.tick, maxRotation: 0, autoSkip: false } },
        y: {
          beginAtZero: true, border: { display: false },
          grid: { color: t.grid, borderDash: [4, 4] },
          ticks: { color: t.tick, maxTicksLimit: 5, callback: (v) => num(v, 0) },
        },
      },
    },
  });
}

function drawDist(canvas, items) {
  if (!canvas || !setupChartDefaults()) return;
  const t = themeColors();
  if (distChart) distChart.destroy();
  if (!items.length) { distChart = null; return; }
  distChart = new window.Chart(canvas, {
    type: "doughnut",
    data: {
      labels: items.map((i) => i.label),
      datasets: [{ data: items.map((i) => i.quantity), backgroundColor: items.map((i) => i.color), borderColor: t.surface, borderWidth: 3, hoverOffset: 4 }],
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      cutout: "72%",
      plugins: {
        legend: { display: false },
        tooltip: {
          rtl: true, textDirection: "rtl", padding: 10, cornerRadius: 10,
          callbacks: { label: (c) => ` ${num(c.parsed)} قطعة (${items[c.dataIndex].pct}%)` },
        },
      },
    },
  });
}

// ---------- وصف صف الحركة للعرض ----------
function describeMovement(r, i) {
  if (r.kind === "transfer") {
    return {
      key: `t-${r.id ?? i}`, kind: "transfer", label: "نقل", icon: "fa-right-left",
      tile: "bg-blue-500/10 text-blue-600 dark:text-blue-300",
      ref: r.product_name || "-", sub: `الكمية: ${num(r.quantity)}`,
      href: "movements.html#kind=transfer", from: r.from_code, to: r.to_code,
      user: r.user_name || "-", date: fmtDateTime(r.date), value: null,
    };
  }
  const isIn = r.kind === "in";
  return {
    key: `${r.kind}-${r.invoice_id ?? i}`, kind: r.kind,
    label: isIn ? "وارد" : "صادر",
    icon: isIn ? "fa-arrow-down" : "fa-arrow-up",
    tile: isIn ? "bg-brand-500/10 text-brand-600 dark:text-brand-300" : "bg-orange-500/10 text-orange-600 dark:text-orange-300",
    ref: `فاتورة #${r.invoice_id}`, sub: r.party_role || "",
    href: `invoice.html#kind=${r.kind}&id=${r.invoice_id}`,
    party: r.party_name || "-", user: r.user_name || "-", date: fmtDateTime(r.date),
    value: r.total_value != null ? money(r.total_value) : null,
  };
}

document.addEventListener("alpine:init", () => {
  window.Alpine.data("dashboard", () => ({
    canMovements: can("invoices.view", "transfers.view"), canProducts: can("products.view"),
    admin: can("finance.view"),  // كتلة القيم المالية (قيمة المخزون وإجماليات الصادر)
    user: getUser(),
    today: longToday(),

    summary: null, out: null, adminData: null,
    flow: [], dist: [], distTotal: 0, low: [], lowTotal: 0, recent: [],
    state: { summary: "loading", flow: "loading", dist: "loading", low: "loading", recent: "loading" },
    errors: {},
    refreshing: false,

    quick: [
      { label: "إضافة منتج", href: "products.html#new=1", icon: "fa-plus", tone: "bg-brand-500/10 text-brand-600 dark:text-brand-300" },
      { label: "إدخال مخزون (وارد)", href: "stock-in.html", icon: "fa-arrow-down", tone: "bg-emerald-500/10 text-emerald-600 dark:text-emerald-300" },
      { label: "إخراج مخزون (صادر)", href: "stock-out.html", icon: "fa-arrow-up", tone: "bg-orange-500/10 text-orange-600 dark:text-orange-300" },
      { label: "نقل بين المواقع", href: "transfers.html", icon: "fa-right-left", tone: "bg-blue-500/10 text-blue-600 dark:text-blue-300" },
      { label: "استيراد من Excel", href: "settings.html#import", icon: "fa-file-excel", tone: "bg-green-600/10 text-green-700 dark:text-green-300" },
    ],

    init() {
      window.addEventListener("app-user", (e) => { this.user = e.detail; });
      window.addEventListener("app-theme", () => this.drawCharts());
      this.loadAll();
    },

    num,

    get firstName() { return (this.user.name || "").split(" ")[0]; },

    get todayFlow() {
      const last = this.flow[this.flow.length - 1];
      return last || { inQty: 0, outQty: 0 };
    },

    get cards() {
      const s = this.summary || {};
      const f = this.todayFlow;
      const lowN = Number(s.low_stock_count) || 0;
      const loading = this.state.summary === "loading";
      return [
        { label: "إجمالي المنتجات", value: num(s.total_products), sub: "منتج مسجّل في النظام", icon: "fa-cube", tone: TONES.blue, href: "products.html", loading },
        { label: "إجمالي الكميات", value: num(s.total_quantity), sub: "قطعة متوفرة بالمخازن", icon: "fa-warehouse", tone: TONES.amber, href: "locations.html", loading },
        { label: "وصلت للحد الأدنى", value: num(lowN), sub: lowN ? "منتج محتاج توريد" : "كل المنتجات تمام", icon: lowN ? "fa-triangle-exclamation" : "fa-circle-check", tone: lowN ? TONES.red : TONES.brand, href: "products.html#stock=low", loading },
        { label: "حركة المخزون اليوم", value: num(f.inQty + f.outQty), sub: `وارد ${num(f.inQty)} · صادر ${num(f.outQty)}`, icon: "fa-arrow-right-arrow-left", tone: TONES.brand, href: "movements.html", loading: this.state.flow === "loading" },
      ];
    },

    get finance() {
      const o = this.out || {};
      const v = (x) => (x && x.value != null ? money(x.value) : "—");
      return [
        { label: "قيمة المخزون الحالية", value: this.adminData ? money(this.adminData.total_stock_value) : "—", icon: "fa-vault" },
        { label: "صادر اليوم", value: v(o.today), icon: "fa-calendar-day" },
        { label: "صادر الشهر", value: v(o.month), icon: "fa-calendar-days" },
        { label: "صادر السنة", value: v(o.year), icon: "fa-calendar" },
      ];
    },

    // ---------- التحميل (طلب واحد /api/dashboard/overview) ----------
    async loadAll() {
      this.refreshing = true;
      Object.keys(this.state).forEach((k) => (this.state[k] = "loading"));
      try {
        const d = await api.get("/api/dashboard/overview?days=7");
        this.summary = d.summary;
        this.out = d.admin ? d.admin.out_totals : null;
        this.adminData = d.admin;
        this.flow = d.flow.map((r) => ({ date: r.date, inQty: Number(r.in_qty) || 0, outQty: Number(r.out_qty) || 0 }));
        this.dist = d.distribution.map((w, i) => ({
          label: `${w.code} — ${w.name}`, quantity: w.quantity, pct: Math.round(w.pct), color: PALETTE[i % PALETTE.length],
        }));
        this.distTotal = d.distribution.reduce((s, w) => s + w.quantity, 0);
        this.lowTotal = d.low_stock.total;
        this.low = d.low_stock.items.map((p) => {
          const min = Number(p.min_quantity) || 0;
          const qty = Number(p.quantity) || 0;
          return { id: p.id, name: p.name, sku: p.sku || "", qty, min, out: qty <= 0, pct: min > 0 ? Math.max(0, Math.min(100, Math.round((qty / min) * 100))) : 0 };
        });
        this.recent = d.recent.map(describeMovement);
        Object.keys(this.state).forEach((k) => (this.state[k] = "ok"));
        await this.$nextTick();
        drawFlow(this.$refs.flowCanvas, this.flow);
        drawDist(this.$refs.distCanvas, this.dist);
      } catch (e) {
        Object.keys(this.state).forEach((k) => { this.state[k] = "error"; this.errors[k] = errMsg(e); });
      }
      this.refreshing = false;
    },

    async refresh() { api.invalidate(); await this.loadAll(); },
    // (أزرار إعادة المحاولة في الصفحة بتستخدم نفس الطلب)
    loadSummary() { return this.loadAll(); },
    loadFlow() { return this.loadAll(); },
    loadDist() { return this.loadAll(); },
    loadLow() { return this.loadAll(); },
    loadRecent() { return this.loadAll(); },

    drawCharts() {
      drawFlow(this.$refs.flowCanvas, this.flow);
      drawDist(this.$refs.distCanvas, this.dist);
    },
  }));
});

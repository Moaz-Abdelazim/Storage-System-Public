// =====================================================================
//  movements.js — سجل الحركات الموحّد (وارد / صادر / نقل) بفلاتر وترقيم من السيرفر.
//  وضعين: "الحركة الفعلية" (إذونات مكتملة + نقل؛ الملغي اختياري بعلامة) و"المستندات" (كل إذونات الإضافة/الصرف بكل حالاتها:
//  عرض سعر / محجوزة / تمت / ملغاة - عروض الأسعار والحجوزات مش حركة مخزون فعلية فمبتظهرش في الوضع الأول).
// =====================================================================
import { can } from "./auth.js";
import { api, qs, download } from "./api.js";
import { num, money, fmtDateTime, toast, errMsg, KIND, getQuery, setQuery, todayISO, daysAgoISO, docStatus } from "./utils.js";

function describe(r, i) {
  const meta = KIND[r.kind] || KIND.in;
  if (r.kind === "transfer") {
    return { key: `t-${r.invoice_id}-${i}`, kind: "transfer", meta, ref: r.product_name || "نقل", sub: `الكمية: ${num(r.quantity)}${r.notes ? " · " + r.notes : ""}`,
      href: `location-lookup.html#code=${encodeURIComponent(r.from_code || "")}`, from: r.from_code, to: r.to_code, user: r.user_name, date: fmtDateTime(r.date), items: 1, value: "—" };
  }
  return { key: `${r.kind}-${r.invoice_id}`, kind: r.kind, meta, ref: `${r.kind === "in" ? "إذن إضافة" : "إذن صرف"} #${r.invoice_id}`, sub: r.status === "voided" && r.void_reason ? `سبب الإلغاء: ${r.void_reason}` : (r.notes || ""),
    href: `invoice.html#kind=${r.kind}&id=${r.invoice_id}`, party: r.party_name, user: r.user_name, date: fmtDateTime(r.date), items: r.items_total, value: money(r.total_value),
    status: r.status || "completed" };
}

// صف من قايمة المستندات (/api/stock/in|out): حقولها مختلفة عن السجل الموحّد
function describeDoc(kind, r) {
  const meta = KIND[kind];
  return { key: `${kind}-${r.id}`, kind, meta, ref: `${kind === "in" ? "إذن إضافة" : "إذن صرف"} #${r.id}`,
    sub: r.status === "voided" && r.void_reason ? `سبب الإلغاء: ${r.void_reason}` : (r.notes || ""), href: `invoice.html#kind=${kind}&id=${r.id}`,
    party: kind === "in" ? r.supplier_name : r.recipient_name, user: kind === "in" ? r.received_by_name : r.delivered_by_name,
    date: fmtDateTime(r.date), items: r.items_total, value: money(r.total_value), status: r.status || "completed" };
}

document.addEventListener("alpine:init", () => {
  window.Alpine.data("movements", () => ({
    num, money,
    rows: [], totals: null, loading: true, error: "", exporting: false,
    page: 1, pages: 1, total: 0, size: 15,
    docStatus,
    view: getQuery("view") === "docs" ? "docs" : "log",
    docStatuses: [{ key: "", label: "كل الحالات" }, { key: "quote", label: "عرض سعر" }, { key: "reserved", label: "محجوزة" }, { key: "completed", label: "تمت" }, { key: "voided", label: "ملغاة" }],
    f: { status: getQuery("status"), voided: getQuery("voided") === "1", kind: getQuery("kind"), date_from: getQuery("date_from"), date_to: getQuery("date_to"), party: getQuery("party"), user: "" },
    kinds: [{ key: "", label: "الكل" }, { key: "in", label: "وارد" }, { key: "out", label: "صادر" }, { key: "transfer", label: "نقل" }]
      .filter((k) => !k.key || (k.key === "transfer" ? can("transfers.view") : can("invoices.view"))),
    get docKinds() { return this.kinds.filter((k) => k.key === "in" || k.key === "out"); },
    get canDocs() { return can("invoices.view"); },
    presets: [{ label: "اليوم", days: 0 }, { label: "آخر 7 أيام", days: 6 }, { label: "آخر 30 يوم", days: 29 }, { label: "كل الفترة", days: null }],

    init() { this.load(); },

    preset(days) {
      this.f.date_from = days === null ? "" : daysAgoISO(days);
      this.f.date_to = days === null ? "" : todayISO();
      this.apply();
    },
    setView(v) { this.view = v; if (v === "docs" && !["in", "out"].includes(this.f.kind)) this.f.kind = "out"; this.apply(); },
    apply() {
      this.page = 1;
      setQuery({ view: this.view === "docs" ? "docs" : "", kind: this.f.kind, party: this.f.party.trim(), date_from: this.f.date_from, date_to: this.f.date_to, status: this.view === "docs" ? this.f.status : "", voided: this.view === "log" && this.f.voided ? "1" : "" });
      this.load();
    },

    async load() {
      this.loading = true; this.error = "";
      const { kind, date_from, date_to, party, user } = this.f;
      try {
        if (this.view === "docs") {  // قايمة المستندات بكل حالاتها (بدون فلاتر التاريخ/الجهة: دي بتتطبق على السجل الموحّد بس)
          const k = kind === "in" ? "in" : "out";
          const r = await api.get(`/api/stock/${k}${qs({ page: this.page, page_size: this.size, status: this.f.status })}`);
          this.rows = r.items.map((x) => describeDoc(k, x)); this.total = r.total; this.pages = r.total_pages; this.page = r.page;
        } else {
          const r = await api.get(`/api/stock/log${qs({ kind, date_from, date_to, party: party.trim(), user, include_voided: this.f.voided ? 1 : "", page: this.page, page_size: this.size })}`);
          this.rows = r.items.map(describe); this.totals = r.totals; this.total = r.total; this.pages = r.total_pages; this.page = r.page;
        }
      } catch (e) { this.error = errMsg(e); this.rows = []; }
      this.loading = false;
    },

    async exportExcel() {
      this.exporting = true;
      try { await download(`/api/excel/movements/export${qs({ date_from: this.f.date_from, date_to: this.f.date_to })}`, "movements.xlsx"); }
      catch (e) { toast(errMsg(e), "error"); }
      this.exporting = false;
    },
  }));
});

// =====================================================================
//  movements.js — سجل الحركات الموحّد (وارد / صادر / نقل) بفلاتر وترقيم من السيرفر.
// =====================================================================
import { can } from "./auth.js";
import { api, qs, download } from "./api.js";
import { num, money, fmtDateTime, toast, errMsg, KIND, getQuery, setQuery, todayISO, daysAgoISO } from "./utils.js";

function describe(r, i) {
  const meta = KIND[r.kind] || KIND.in;
  if (r.kind === "transfer") {
    return { key: `t-${r.invoice_id}-${i}`, kind: "transfer", meta, ref: r.product_name || "نقل", sub: `الكمية: ${num(r.quantity)}${r.notes ? " · " + r.notes : ""}`,
      href: `location-lookup.html#code=${encodeURIComponent(r.from_code || "")}`, from: r.from_code, to: r.to_code, user: r.user_name, date: fmtDateTime(r.date), items: 1, value: "—" };
  }
  return { key: `${r.kind}-${r.invoice_id}`, kind: r.kind, meta, ref: `فاتورة #${r.invoice_id}`, sub: r.notes || "", href: `invoice.html#kind=${r.kind}&id=${r.invoice_id}`,
    party: r.party_name, user: r.user_name, date: fmtDateTime(r.date), items: r.items_total, value: money(r.total_value), held: r.status === "held" };
}

document.addEventListener("alpine:init", () => {
  window.Alpine.data("movements", () => ({
    num, money,
    rows: [], totals: null, loading: true, error: "", exporting: false,
    page: 1, pages: 1, total: 0, size: 15,
    f: { kind: getQuery("kind"), date_from: getQuery("date_from"), date_to: getQuery("date_to"), party: getQuery("party"), user: "" },
    kinds: [{ key: "", label: "الكل" }, { key: "in", label: "وارد" }, { key: "out", label: "صادر" }, { key: "transfer", label: "نقل" }]
      .filter((k) => !k.key || (k.key === "transfer" ? can("transfers.view") : can("invoices.view"))),
    presets: [{ label: "اليوم", days: 0 }, { label: "آخر 7 أيام", days: 6 }, { label: "آخر 30 يوم", days: 29 }, { label: "كل الفترة", days: null }],

    init() { this.load(); },

    preset(days) {
      this.f.date_from = days === null ? "" : daysAgoISO(days);
      this.f.date_to = days === null ? "" : todayISO();
      this.apply();
    },
    apply() { this.page = 1; setQuery({ kind: this.f.kind, party: this.f.party.trim(), date_from: this.f.date_from, date_to: this.f.date_to }); this.load(); },

    async load() {
      this.loading = true; this.error = "";
      const { kind, date_from, date_to, party, user } = this.f;
      try {
        const r = await api.get(`/api/stock/log${qs({ kind, date_from, date_to, party: party.trim(), user, page: this.page, page_size: this.size })}`);
        this.rows = r.items.map(describe); this.totals = r.totals; this.total = r.total; this.pages = r.total_pages; this.page = r.page;
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

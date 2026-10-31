// =====================================================================
//  lookup.js — البحث عن موقع: تقرير المخزون حسب المخزن/الموقع مع drill-down.
// =====================================================================
import { api, qs } from "./api.js";
import { can } from "./auth.js";
import { num, money, errMsg, getQuery, setQuery, downloadCsv, toast } from "./utils.js";

document.addEventListener("alpine:init", () => {
  window.Alpine.data("lookup", () => ({
    admin: can("finance.view"), num, money,
    scope: { warehouse_id: null, location_id: null, code: "" },
    code: "", codeError: "", resolving: false,
    data: null, loading: true, error: "", search: "", page: 1, pages: 1, total: 0, size: 10,
    bd: { page: 1, pages: 1, total: 0, size: 8 },

    init() {
      const c = getQuery("code");
      if (c) { this.code = c; this.byCode(); } else this.load();
    },

    get crumbs() {
      const parts = (this.scope.code || "").split("-").filter(Boolean);
      return parts.map((_, i) => parts.slice(0, i + 1).join("-"));
    },
    share(b) {
      const max = Math.max(...this.data.breakdown.map((x) => x.quantity), 1);
      return Math.max(4, Math.round((b.quantity / max) * 100));
    },

    async load() {
      this.loading = true; this.error = "";
      const { warehouse_id, location_id } = this.scope;
      try {
        const r = await api.get(`/api/locations/reports/stock${qs({ warehouse_id, location_id, page: this.page, page_size: this.size, search: this.search.trim(), breakdown_page: this.bd.page, breakdown_page_size: this.bd.size })}`);
        this.data = r; this.total = r.total; this.pages = r.total_pages; this.page = r.page;
        Object.assign(this.bd, { page: r.breakdown_page, pages: r.breakdown_total_pages, total: r.breakdown_total });
      } catch (e) { this.error = errMsg(e); }
      this.loading = false;
    },

    setScope(s) {
      this.scope = s; this.page = 1; this.bd.page = 1; setQuery({ code: s.code }); this.load();
    },
    reset() { this.$refs.nodeSel.clear(); this.code = ""; this.codeError = ""; this.setScope({ warehouse_id: null, location_id: null, code: "" }); },

    pickNode(opt) {
      this.codeError = "";
      if (!opt) { this.reset(); return; }
      const n = opt.data;
      this.setScope({ warehouse_id: n.warehouse_id, location_id: n.kind === "location" ? n.id : null, code: n.full_code });
      this.code = n.full_code;
    },

    async byCode() { return this.goCode(this.code); },

    async goCode(code) {
      code = (code || "").trim();
      if (!code) { this.reset(); return; }
      this.resolving = true; this.codeError = "";
      try {
        const r = await api.get(`/api/locations/resolve?code=${encodeURIComponent(code)}`);
        this.code = r.full_code;
        this.setScope({ warehouse_id: r.warehouse_id, location_id: r.location_id, code: r.full_code });
      } catch (e) { this.codeError = errMsg(e); }
      this.resolving = false;
    },

    async exportCsv() {
      try {
        const r = await api.get(`/api/locations/reports/stock${qs({ warehouse_id: this.scope.warehouse_id, location_id: this.scope.location_id, page: 1, page_size: 100, search: this.search.trim(), breakdown_page_size: 1 })}`);
        downloadCsv(`stock-${this.scope.code || "all"}.csv`, [
          { label: "المنتج", key: "name" }, { label: "SKU", key: "sku" }, { label: "الكمية", key: "quantity" },
          { label: "الوحدة", key: "unit" }, { label: "الأماكن", value: (p) => p.locations.join(" | ") },
        ], r.items);
        if (r.total_pages > 1) toast("اتصدّرت أول 100 منتج بس", "error");
      } catch (e) { toast(errMsg(e), "error"); }
    },
  }));
});

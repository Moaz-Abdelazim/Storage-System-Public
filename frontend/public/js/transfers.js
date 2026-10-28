// =====================================================================
//  transfers.js — النقل بين المواقع + قايمة الرص + سجل النقل.
// =====================================================================
import { api, qs } from "./api.js";
import { invalidateData } from "./data.js";
import { num, fmtDateTime, toast, errMsg } from "./utils.js";

document.addEventListener("alpine:init", () => {
  window.Alpine.data("transfers", () => ({
    num, fmtDateTime,
    product: null, fromOptions: [], from: null, to: null, qty: null, notes: "", saving: false, error: "",
    putaway: { items: [], total: 0, page: 1, total_pages: 1, page_size: 6, unallocated_total: 0 },
    h: { rows: [], page: 1, pages: 1, total: 0, size: 10, loading: true, search: "", date_from: "", date_to: "" },

    init() { this.loadPutaway(); this.loadHistory(); },

    async pickProduct(option, preselectLocationId = null) {
      this.from = null; this.qty = null; this.fromOptions = []; this.error = "";
      const sel = this.$refs.fromSel;
      sel.clear();
      if (!option) { this.product = null; sel.setOptions([]); return; }
      try {
        const p = await api.get(`/api/products/${option.value}`);
        this.product = p;
        this.fromOptions = p.locations.map((l) => ({ value: l.location_id, label: l.full_code, sub: `${num(l.quantity)} ${p.unit || ""}`, data: l }));
        sel.setOptions(this.fromOptions);
        sel.setAttribute("placeholder", this.fromOptions.length ? "اختار المكان المنقول منه" : "مفيش أماكن موزعة");
        const auto = preselectLocationId ? this.fromOptions.find((o) => o.value === preselectLocationId) : this.fromOptions.length === 1 ? this.fromOptions[0] : null;
        if (auto) { sel.setSelected(auto); this.from = auto.data; }
      } catch (e) { toast(errMsg(e), "error"); }
    },

    // من قايمة الرص: بيجهّز النموذج
    async prefill(p) {
      this.$refs.prodSel.setSelected({ value: p.product_id, label: p.name, sub: p.sku || "", data: { id: p.product_id } });
      await this.pickProduct({ value: p.product_id }, p.location_id);
      this.qty = p.quantity;
      this.$refs.toSel.open();
      window.scrollTo({ top: 0, behavior: "smooth" });
    },

    async submit() {
      this.error = "";
      if (!this.product) { this.error = "اختار المنتج"; return; }
      if (!this.from) { this.error = "اختار المكان المنقول منه"; return; }
      if (!this.to) { this.error = "اختار المكان المنقول إليه"; return; }
      if (this.from.location_id === this.to.id) { this.error = "المكانين نفس بعض"; return; }
      const q = Number(this.qty);
      if (!(q > 0)) { this.error = "اكتب كمية صحيحة"; return; }
      if (q > this.from.quantity) { this.error = `الكمية أكبر من الموجود (${num(this.from.quantity)})`; return; }
      this.saving = true;
      try {
        await api.post("/api/locations/transfer", {
          product_id: this.product.id, from_location_id: this.from.location_id, to_location_id: this.to.id, quantity: q, notes: this.notes || null,
        });
        toast(`تم نقل ${num(q)} ${this.product.unit || ""} من ${this.from.full_code} إلى ${this.to.full_code}`);
        invalidateData();
        const pid = this.product.id;
        this.qty = null; this.notes = ""; this.to = null; this.$refs.toSel.clear();
        await this.pickProduct({ value: pid });
        this.loadPutaway(); this.h.page = 1; this.loadHistory();
      } catch (e) { this.error = errMsg(e); }
      this.saving = false;
    },

    async loadPutaway(page = 1) {
      try { this.putaway = await api.get(`/api/locations/putaway${qs({ page, page_size: 6 })}`); } catch (e) { /* مش حرج */ }
    },

    async loadHistory() {
      this.h.loading = true;
      const { page, size, search, date_from, date_to } = this.h;
      try {
        const r = await api.get(`/api/locations/transfers${qs({ page, page_size: size, search: search.trim(), date_from, date_to })}`);
        Object.assign(this.h, { rows: r.items, total: r.total, pages: r.total_pages, page: r.page });
      } catch (e) { toast(errMsg(e), "error"); }
      this.h.loading = false;
    },
  }));
});

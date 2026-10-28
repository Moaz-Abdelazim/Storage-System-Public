// =====================================================================
//  parties.js — الموردين وجهات الاستلام (نفس الصفحة بإعدادات مختلفة).
// =====================================================================
import { api, qs } from "./api.js";
import { isAdmin } from "./auth.js";
import { invalidateData } from "./data.js";
import { num, fmtDateTime, money, toast, errMsg, confirmAction } from "./utils.js";

export function registerParties(name, cfg) {
  // cfg: { base, productsPath, invoicesPath, noun, deleteHint }
  document.addEventListener("alpine:init", () => {
    window.Alpine.data(name, () => ({
      admin: isAdmin(), num, money, fmtDateTime,
      rows: [], loading: true, error: "", search: "",
      page: 1, pages: 1, total: 0, size: 10, totalInvoices: 0,
      form: { open: false, id: null, name: "", phone: "", address: "", notes: "", saving: false, error: "" },
      detail: {
        open: false, party: null, search: "", loading: false, openId: null,
        products: { items: [], page: 1, total: 0, total_pages: 1, page_size: 8 },
        invoices: { items: [], page: 1, total: 0, total_pages: 1, page_size: 6 }, invLoading: false,
      },

      init() { this.load(); },
      apply() { this.page = 1; this.load(); },

      async load() {
        this.loading = true; this.error = "";
        try {
          const r = await api.get(`${cfg.base}${qs({ page: this.page, page_size: this.size, search: this.search.trim() })}`);
          this.rows = r.items; this.total = r.total; this.pages = r.total_pages; this.page = r.page; this.totalInvoices = r.total_invoices;
        } catch (e) { this.error = errMsg(e); this.rows = []; }
        this.loading = false;
      },

      openForm(r) {
        this.form = { open: true, id: r ? r.id : null, name: r ? r.name : "", phone: r ? r.phone || "" : "", address: r ? r.address || "" : "", notes: r ? r.notes || "" : "", saving: false, error: "" };
      },
      async save() {
        const f = this.form;
        if (!f.name) { f.error = "اكتب الاسم"; return; }
        f.saving = true; f.error = "";
        const body = { name: f.name, phone: f.phone || null, address: f.address || null, notes: f.notes || null };
        try {
          if (f.id) await api.put(`${cfg.base}/${f.id}`, body); else await api.post(cfg.base, body);
          toast(f.id ? "تم الحفظ" : "تمت الإضافة"); f.open = false; invalidateData(); this.load();
        } catch (e) { f.error = errMsg(e); }
        f.saving = false;
      },

      async remove(r) {
        const ok = await confirmAction({ title: `حذف ${cfg.noun}؟`, danger: true, confirmText: "حذف", message: `"${r.name}" هيتحذف. ${cfg.deleteHint}` });
        if (!ok) return;
        try { await api.del(`${cfg.base}/${r.id}`); toast("تم الحذف"); invalidateData(); this.load(); }
        catch (e) { toast(errMsg(e), "error"); }
      },

      // ---------- التفاصيل (منتجات الجهة ثم فواتير كل منتج — كلهم بترقيم من السيرفر) ----------
      openDetail(r) {
        this.detail.open = true; this.detail.party = r; this.detail.search = ""; this.detail.openId = null;
        this.detail.products = { items: [], page: 1, total: 0, total_pages: 1, page_size: 8 };
        this.loadProducts(1);
      },
      async loadProducts(page = 1) {
        const d = this.detail;
        d.loading = true;
        try { d.products = await api.get(`${cfg.productsPath}/${d.party.id}${qs({ page, page_size: d.products.page_size, search: d.search.trim() })}`); }
        catch (e) { toast(errMsg(e), "error"); }
        d.loading = false;
      },
      async toggleProduct(p) {
        const d = this.detail;
        if (d.openId === p.product_id) { d.openId = null; return; }
        d.openId = p.product_id;
        await this.loadInvoices(1);
      },
      async loadInvoices(page = 1) {
        const d = this.detail;
        d.invLoading = true;
        try { d.invoices = await api.get(`${cfg.invoicesPath}/${d.party.id}/${d.openId}${qs({ page, page_size: d.invoices.page_size })}`); }
        catch (e) { toast(errMsg(e), "error"); }
        d.invLoading = false;
      },
    }));
  });
}

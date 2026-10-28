// =====================================================================
//  simple-crud.js — صفحة بسيطة لكيان (اسم فقط) زي التصنيفات والماركات.
// =====================================================================
import { api, qs } from "./api.js";
import { isAdmin } from "./auth.js";
import { invalidateData } from "./data.js";
import { num, fmtDate, toast, errMsg, confirmAction } from "./utils.js";

export function registerCrud(componentName, cfg) {
  // cfg: { base: "/api/categories", noun: "تصنيف", filterParam: "category_id" }
  document.addEventListener("alpine:init", () => {
    window.Alpine.data(componentName, () => ({
      admin: isAdmin(), noun: cfg.noun,
      rows: [], loading: true, error: "", search: "",
      page: 1, pages: 1, total: 0, size: 10, classified: 0,
      form: { open: false, id: null, name: "", saving: false, error: "" },
      num, fmtDate,

      init() { this.load(); },

      link(r) { return `products.html#${cfg.filterParam}=${r.id}`; },
      apply() { this.page = 1; this.load(); },

      async load() {
        this.loading = true; this.error = "";
        try {
          const r = await api.get(`${cfg.base}/stats${qs({ page: this.page, page_size: this.size, search: this.search.trim() })}`);
          this.rows = r.items; this.total = r.total; this.pages = r.total_pages; this.page = r.page; this.classified = r.classified_products;
        } catch (e) { this.error = errMsg(e); this.rows = []; }
        this.loading = false;
      },

      openForm(r) { this.form = { open: true, id: r ? r.id : null, name: r ? r.name : "", saving: false, error: "" }; },

      async save() {
        const f = this.form;
        if (!f.name.trim()) { f.error = `اكتب اسم ال${cfg.noun}`; return; }
        f.saving = true; f.error = "";
        try {
          if (f.id) await api.put(`${cfg.base}/${f.id}`, { name: f.name.trim() }); else await api.post(cfg.base, { name: f.name.trim() });
          toast(f.id ? "تم الحفظ" : `تمت إضافة ال${cfg.noun}`);
          f.open = false; invalidateData(); this.load();
        } catch (e) { f.error = errMsg(e); }
        f.saving = false;
      },

      async remove(r) {
        const ok = await confirmAction({
          title: `حذف ال${cfg.noun}؟`, danger: true, confirmText: "حذف",
          message: `"${r.name}" هيتحذف${r.products_count ? ` والـ ${r.products_count} منتج المرتبطين بيه هيفضلوا من غير ${cfg.noun}` : ""}.`,
        });
        if (!ok) return;
        try { await api.del(`${cfg.base}/${r.id}`); toast("تم الحذف"); invalidateData(); this.load(); }
        catch (e) { toast(errMsg(e), "error"); }
      },
    }));
  });
}

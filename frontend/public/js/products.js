// =====================================================================
//  products.js — صفحة المنتجات (ترقيم وفلترة وترتيب من السيرفر).
// =====================================================================
import { api, qs, download } from "./api.js";
import { can } from "./auth.js";
import { invalidateData } from "./data.js";
import { num, money, fmtDateTime, toast, errMsg, productStatus, confirmAction, getQuery, setQuery } from "./utils.js";

let rowKey = 0;
const newRow = () => ({ key: ++rowKey, location_id: null, quantity: null });
const blankForm = () => ({
  open: false, saving: false, errors: {}, id: null,
  sku: "", barcode: "", name: "", category_id: null, brand_id: null, unit: "قطعة", price: 0, min_quantity: 0, rows: [],
});

document.addEventListener("alpine:init", () => {
  window.Alpine.data("products", () => ({
    canCreate: can("products.create"), canEdit: can("products.edit"), canPrice: can("products.price"), canAdjust: can("products.adjust"),
    canToggle: can("products.toggle"), canMovements: can("invoices.view"), canFinance: can("finance.view"),
    rows: [], total: 0, page: 1, pages: 1, size: 25,
    loading: true, error: "", exporting: false,
    filters: { search: getQuery("search"), category_id: Number(getQuery("category_id")) || null, brand_id: Number(getQuery("brand_id")) || null, stock: getQuery("stock") || "", status: "active", sort: "name", order: "asc" },
    stockTabs: [{ key: "", label: "الكل" }, { key: "ok", label: "متوفر" }, { key: "low", label: "تحت الحد" }, { key: "out", label: "نفد" }],
    form: blankForm(),
    loc: { open: false, saving: false, error: "", product: null, rows: [], reason: "" },
    detail: { open: false, loading: false, tab: "locations", product: null, mv: { items: [], page: 1, total_pages: 1, total: 0, page_size: 8 } },

    num, money, fmtDateTime,
    statusOf: productStatus,

    init() {
      this.load();
      if (getQuery("new") === "1") { this.openForm(); setQuery({ new: "" }); }
    },

    // ---------- القايمة ----------
    async load() {
      this.loading = true; this.error = "";
      const f = this.filters;
      const params = { page: this.page, page_size: this.size, search: f.search.trim(), category_id: f.category_id, brand_id: f.brand_id, stock: f.stock, status: f.status, sort: f.sort, order: f.order };
      try {
        const r = await api.get(`/api/products${qs(params)}`);
        this.rows = r.items; this.total = r.total; this.pages = r.total_pages; this.page = r.page;
      } catch (e) { this.error = errMsg(e); this.rows = []; }
      this.loading = false;
    },
    applyFilters() { this.page = 1; setQuery({ search: this.filters.search.trim(), stock: this.filters.stock }); this.load(); },
    go(p) { this.page = p; this.load(); window.scrollTo({ top: 0, behavior: "smooth" }); },
    sortBy(col) {
      this.filters.order = this.filters.sort === col && this.filters.order === "asc" ? "desc" : "asc";
      this.filters.sort = col; this.page = 1; this.load();
    },
    sortIcon(col) { return this.filters.sort !== col ? "fa-sort opacity-30" : this.filters.order === "asc" ? "fa-sort-up" : "fa-sort-down"; },

    async exportExcel() {
      this.exporting = true;
      try { await download("/api/excel/products/export", "products.xlsx"); } catch (e) { toast(errMsg(e), "error"); }
      this.exporting = false;
    },

    // ---------- إضافة / تعديل ----------
    addRow(list) { list.push(newRow()); },
    rowsTotal(list) { return list.reduce((s, r) => s + (Number(r.quantity) || 0), 0); },

    openForm(p) {
      this.form = blankForm();
      if (p) Object.assign(this.form, { id: p.id, sku: p.sku || "", barcode: p.barcode || "", name: p.name, category_id: p.category_id, brand_id: p.brand_id, unit: p.unit || "قطعة", price: p.price, min_quantity: p.min_quantity });
      else this.form.rows = [newRow()];
      this.form.open = true;
    },

    async saveForm() {
      const f = this.form;
      f.errors = {};
      if (!f.name) { f.errors.name = "اكتب اسم المنتج"; return; }
      const body = {
        sku: f.sku || null, barcode: (f.barcode || "").trim() || null, name: f.name, category_id: f.category_id || null, brand_id: f.brand_id || null,
        unit: f.unit || "قطعة", price: Number(f.price) || 0, min_quantity: Number(f.min_quantity) || 0,
      };
      if (!f.id) {
        const locs = f.rows.filter((r) => r.location_id && Number(r.quantity) > 0).map((r) => ({ location_id: r.location_id, quantity: Number(r.quantity) }));
        if (f.rows.some((r) => Number(r.quantity) > 0 && !r.location_id)) { f.errors.general = "اختار مكان تخزين لكل كمية"; return; }
        body.quantity = locs.reduce((s, r) => s + r.quantity, 0);
        body.locations = locs;
      }
      f.saving = true;
      try {
        if (f.id) await api.put(`/api/products/${f.id}`, body); else await api.post("/api/products", body);
        toast(f.id ? "تم تعديل المنتج" : "تمت إضافة المنتج");
        f.open = false; invalidateData(); this.load();
      } catch (e) { f.errors.general = errMsg(e); }
      f.saving = false;
    },

    // ---------- توزيع الكمية ----------
    openLocations(p) {
      this.loc = {
        open: true, saving: false, error: "", product: p, reason: "",
        rows: p.locations.map((l) => ({ key: ++rowKey, location_id: l.location_id, quantity: l.quantity })),
      };
      if (!this.loc.rows.length) this.loc.rows.push(newRow());
    },
    async saveLocations() {
      const l = this.loc;
      const rows = l.rows.filter((r) => r.location_id && Number(r.quantity) > 0);
      if (l.rows.some((r) => Number(r.quantity) > 0 && !r.location_id)) { l.error = "اختار مكان تخزين لكل كمية"; return; }
      if (new Set(rows.map((r) => r.location_id)).size !== rows.length) { l.error = "في مكان متكرر — ادمج الكميات في صف واحد"; return; }
      l.error = ""; l.saving = true;
      try {
        await api.put(`/api/products/${l.product.id}/locations`, {
          locations: rows.map((r) => ({ location_id: r.location_id, quantity: Number(r.quantity) })), reason: l.reason || null,
        });
        toast("تم حفظ التوزيع"); l.open = false; invalidateData(); this.load();
      } catch (e) { l.error = errMsg(e); }
      l.saving = false;
    },

    // ---------- التفاصيل (حركات المنتج بترقيم من السيرفر) ----------
    openDetail(p) {
      this.detail = { open: true, loading: false, tab: "locations", product: p, mv: { items: [], page: 1, total_pages: 1, total: 0, page_size: 8 } };
    },
    async loadMovements(kind, page = 1) {
      const d = this.detail;
      d.tab = kind; d.loading = true;
      try {
        const r = await api.get(`/api/products/${d.product.id}/movements${qs({ kind, page, page_size: d.mv.page_size })}`);
        d.mv = r;
      } catch (e) { toast(errMsg(e), "error"); }
      d.loading = false;
    },

    // ---------- تفعيل / تعطيل (أدمن) ----------
    async toggleActive(p) {
      const disable = p.is_active !== false;
      const ok = await confirmAction({
        title: disable ? "تعطيل المنتج؟" : "تفعيل المنتج؟",
        message: disable ? `"${p.name}" هيختفي من القوايم والفواتير الجديدة، وسجلّه بيفضل محفوظ.` : `"${p.name}" هيرجع يظهر في القوايم والفواتير.`,
        confirmText: disable ? "تعطيل" : "تفعيل", danger: disable,
      });
      if (!ok) return;
      try { await api.patch(`/api/products/${p.id}/active`, { is_active: !disable }); toast(disable ? "تم التعطيل" : "تم التفعيل"); invalidateData(); this.load(); }
      catch (e) { toast(errMsg(e), "error"); }
    },
  }));
});

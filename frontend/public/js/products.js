// =====================================================================
//  products.js — صفحة المنتجات (ترقيم وفلترة وترتيب من السيرفر).
// =====================================================================
import { api, apiUrl, qs, download } from "./api.js";
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
    canToggle: can("products.toggle"), canFinance: can("finance.view"),
    rows: [], total: 0, page: 1, pages: 1, size: 25,
    loading: true, error: "", exporting: false,
    filters: { search: getQuery("search"), category_id: Number(getQuery("category_id")) || null, brand_id: Number(getQuery("brand_id")) || null, stock: getQuery("stock") || "", status: "active", sort: "name", order: "asc",
      warehouse_id: Number(getQuery("warehouse_id")) || null, location_id: null },
    // فلتر الموقع المتتالي: مخزن ← مستوى 1 ← مستوى 2 ... (بيظهر المستوى اللي بعده بس لو الموقع المختار ليه أبناء)
    canLoc: ["locations.view", "stock_in.create", "stock_out.create", "stock_out.quote", "stock_out.reserve", "transfers.create", "transfers.view", "products.create", "products.adjust", "stocktake.manage"].some((p) => can(p)),
    whs: [], levels: [], locLoading: false,
    stockTabs: [{ key: "", label: "الكل" }, { key: "ok", label: "متوفر" }, { key: "low", label: "تحت الحد" }, { key: "out", label: "نفد" }],
    form: blankForm(),
    loc: { open: false, saving: false, error: "", product: null, rows: [], reason: "" },

    num, money, fmtDateTime, imgUrl: apiUrl,
    statusOf: productStatus,

    init() {
      this.initLocationFilter().finally(() => this.load());
      if (getQuery("new") === "1") { this.openForm(); setQuery({ new: "" }); }
    },

    // ---------- القايمة ----------
    async load() {
      this.loading = true; this.error = "";
      const f = this.filters;
      const params = { page: this.page, page_size: this.size, search: f.search.trim(), category_id: f.category_id, brand_id: f.brand_id, stock: f.stock, status: f.status, sort: f.sort, order: f.order,
        warehouse_id: f.warehouse_id, location_id: f.location_id };
      try {
        const r = await api.get(`/api/products${qs(params)}`);
        this.rows = r.items; this.total = r.total; this.pages = r.total_pages; this.page = r.page;
      } catch (e) { this.error = errMsg(e); this.rows = []; }
      this.loading = false;
    },
    applyFilters() {
      this.page = 1;
      setQuery({ search: this.filters.search.trim(), stock: this.filters.stock, warehouse_id: this.filters.warehouse_id || "", loc: this.levels.map((l) => l.value).filter(Boolean).join(",") });
      this.load();
    },

    // ---------- فلتر المخزن والموقع المتتالي ----------
    locLabel(l) { return l.name && l.name !== l.code ? `${l.code} — ${l.name}` : l.code; },
    async childrenOf(params) {
      const r = await api.get(`/api/locations/children${qs(params)}`, { ttl: 30_000 });
      return r.map((l) => ({ id: l.id, label: this.locLabel(l) }));
    },
    async initLocationFilter() {
      if (!this.canLoc) { this.filters.warehouse_id = null; return; }  // من غير صلاحية قراءة المواقع الفلتر مخفي (والـ API كان هيرفض)
      try {
        this.whs = await api.get("/api/locations/warehouses", { ttl: 30_000 });
        const wid = this.filters.warehouse_id;
        if (wid && !this.whs.some((w) => w.id === wid)) { this.filters.warehouse_id = null; return; }
        if (!wid) return;
        // استرجاع المسار من الرابط (loc=12,15,20)
        const path = getQuery("loc").split(",").map(Number).filter(Boolean);
        let opts = await this.childrenOf({ warehouse_id: wid });
        for (let i = 0; opts.length; i++) {
          const want = path[i];
          const pick = opts.find((o) => o.id === want);
          this.levels.push({ options: opts, value: pick ? pick.id : "" });
          if (!pick) break;
          this.filters.location_id = pick.id;
          opts = await this.childrenOf({ parent_id: pick.id });
        }
      } catch (e) { toast(errMsg(e), "error"); this.filters.warehouse_id = null; this.levels = []; this.filters.location_id = null; }
    },
    async setWarehouse(id) {
      this.filters.warehouse_id = Number(id) || null; this.filters.location_id = null; this.levels = [];
      if (this.filters.warehouse_id) {
        this.locLoading = true;
        try { const opts = await this.childrenOf({ warehouse_id: this.filters.warehouse_id }); if (opts.length) this.levels = [{ options: opts, value: "" }]; }
        catch (e) { toast(errMsg(e), "error"); }
        this.locLoading = false;
      }
      this.applyFilters();
    },
    async setLevel(i, id) {
      this.levels = this.levels.slice(0, i + 1);
      const val = Number(id) || "";
      this.levels[i].value = val;
      this.filters.location_id = val || (i > 0 ? this.levels[i - 1].value : null) || null;
      if (val) {
        this.locLoading = true;
        try { const opts = await this.childrenOf({ parent_id: val }); if (opts.length) this.levels.push({ options: opts, value: "" }); }
        catch (e) { toast(errMsg(e), "error"); }
        this.locLoading = false;
      }
      this.applyFilters();
    },
    clearLocation() { return this.setWarehouse(""); },
    go(p) { this.page = p; this.load(); window.scrollTo({ top: 0, behavior: "smooth" }); },
    sortBy(col) {
      this.filters.order = this.filters.sort === col && this.filters.order === "asc" ? "desc" : "asc";
      this.filters.sort = col; this.page = 1; this.load();
    },
    sortIcon(col) { return this.filters.sort !== col ? "fa-sort opacity-30" : this.filters.order === "asc" ? "fa-sort-up" : "fa-sort-down"; },

    async exportExcel() {
      this.exporting = true;
      try {
        const h = await download("/api/excel/products/export", "products.xlsx");
        const omitted = Number(h && h.get("X-Images-Omitted")) || 0;
        if (omitted) toast(`${omitted} صورة مش متضمنة في الملف (السقف الأقصى للصور في التصدير أو ملف صورة ناقص)`, "error");
      } catch (e) { toast(errMsg(e), "error"); }
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

    // ---------- صفحة التفاصيل ----------
    pageUrl(p) { return `product.html#id=${p.id}`; },
    // الضغط على الصف يفتح صفحة التفاصيل (إلا لو الضغطة على زرار/رابط/حقل جوه الصف)
    rowClick(p, ev) {
      if (ev.target.closest("a, button, input, select, label")) return;
      location.href = this.pageUrl(p);
    },

    // ---------- تفعيل / تعطيل (أدمن) ----------
    async toggleActive(p) {
      const disable = p.is_active !== false;
      const ok = await confirmAction({
        title: disable ? "تعطيل المنتج؟" : "تفعيل المنتج؟",
        message: disable ? `"${p.name}" هيختفي من القوايم والمستندات الجديدة، وسجلّه بيفضل محفوظ.` : `"${p.name}" هيرجع يظهر في القوايم والمستندات.`,
        confirmText: disable ? "تعطيل" : "تفعيل", danger: disable,
      });
      if (!ok) return;
      try { await api.patch(`/api/products/${p.id}/active`, { is_active: !disable }); toast(disable ? "تم التعطيل" : "تم التفعيل"); invalidateData(); this.load(); }
      catch (e) { toast(errMsg(e), "error"); }
    },
  }));
});

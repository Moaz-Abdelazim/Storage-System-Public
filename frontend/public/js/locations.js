// =====================================================================
//  locations.js — شجرة المخازن والمواقع (تحميل كسول): بنحمّل المخازن بس، وأبناء أي فرع بيتحمّلوا لما تفتحه (بترقيم).
//  البحث بيتم على السيرفر (حد أقصى للنتايج)، ومنتجات كل موقع بترقيم من السيرفر.
// =====================================================================
import { api, qs } from "./api.js";
import { isAdmin } from "./auth.js";
import { invalidateData } from "./data.js";
import { num, toast, errMsg, confirmAction } from "./utils.js";

const CHILD_PAGE = 50;
const keyOf = (n) => `${n.kind}:${n.id}`;
const decorate = (n) => ({ ...n, _open: false, _kids: [], _page: 0, _total: 0, _loading: false, _loaded: false });

document.addEventListener("alpine:init", () => {
  window.Alpine.data("locations", () => ({
    admin: isAdmin(), num,
    tree: [], summary: null, loading: true, error: "",
    search: "", results: [], resultsTruncated: false, searching: false,
    selected: null,
    det: { items: [], page: 1, total_pages: 1, total: 0, page_size: 6, loading: false, search: "" },
    wh: { open: false, id: null, name: "", code: "", saving: false, error: "" },
    lc: { open: false, id: null, name: "", code: "", type: "", parentCode: "", warehouse_id: null, parent_id: null, saving: false, error: "" },

    init() { this.load(); },

    // ---------- التحميل ----------
    async load() {
      this.loading = true; this.error = "";
      try { await this.reloadTree(); } catch (e) { this.error = errMsg(e); }
      this.loading = false;
    },

    // بيعيد تحميل المخازن + الفروع اللي كانت مفتوحة (بعد أي تعديل)
    async reloadTree() {
      const openKeys = new Set();
      const collect = (n) => { if (n._open) { openKeys.add(keyOf(n)); n._kids.forEach(collect); } };
      this.tree.forEach(collect);
      const [roots, summary] = await Promise.all([api.get("/api/locations/nav/roots"), api.get("/api/locations/nav/summary")]);
      this.summary = summary;
      this.tree = roots.map(decorate);
      if (!openKeys.size && !this.tree.length) return;
      if (!openKeys.size) this.tree.forEach((w) => { w._open = true; });
      const restore = async (nodes) => {
        for (const n of nodes) {
          if (openKeys.size ? openKeys.has(keyOf(n)) : n._open) {
            n._open = true;
            await this.loadChildren(n, false, true);
            await restore(n._kids);
          }
        }
      };
      await restore(this.tree);
      if (this.selected) { const fresh = this.find(this.selected.kind, this.selected.id); this.selected = fresh; if (fresh) this.loadProducts(fresh, this.det.page); }
    },

    find(kind, id) {
      const stack = [...this.tree];
      while (stack.length) { const n = stack.pop(); if (n.kind === kind && n.id === id) return n; stack.push(...n._kids); }
      return null;
    },

    async loadChildren(n, more = false, silent = false) {
      if (n._loading) return;
      n._loading = true;
      try {
        const page = more ? n._page + 1 : 1;
        const r = await api.get(`/api/locations/nav/children${qs({ warehouse_id: n.warehouse_id, parent_id: n.kind === "location" ? n.id : "", page, page_size: CHILD_PAGE })}`);
        const items = r.items.map(decorate);
        n._kids = more ? [...n._kids, ...items] : items;
        n._page = r.page; n._total = r.total; n._loaded = true;
      } catch (e) { if (!silent) toast(errMsg(e), "error"); }
      n._loading = false;
    },

    // ---------- العرض ----------
    get kpis() {
      const s = this.summary || {};
      return [
        { label: "المخازن", value: s.warehouses ?? "—", icon: "fa-warehouse", tone: "bg-brand-500/10 text-brand-600" },
        { label: "المواقع", value: s.locations != null ? num(s.locations) : "—", icon: "fa-layer-group", tone: "bg-blue-500/10 text-blue-600" },
        { label: "أماكن التخزين النهائية", value: s.leaves != null ? num(s.leaves) : "—", icon: "fa-box-archive", tone: "bg-amber-500/10 text-amber-600" },
        { label: "إجمالي الكمية", value: s.quantity != null ? num(s.quantity) : "—", icon: "fa-cubes", tone: "bg-violet-500/10 text-violet-600" },
      ];
    },
    iconOf(n) { return n.kind === "warehouse" ? "fa-warehouse" : n.is_leaf ? "fa-box-archive" : "fa-layer-group"; },
    iconTone(n) { return n.kind === "warehouse" ? "bg-brand-500/10 text-brand-600" : n.is_leaf ? "bg-amber-500/10 text-amber-600" : "bg-blue-500/10 text-blue-600"; },
    isSelected(n) { return !!this.selected && this.selected.kind === n.kind && this.selected.id === n.id; },
    isDefault(n) { return n.kind === "warehouse" ? !!n.default_location_id : !!n.is_default_receiving; },

    async toggle(n) {
      n._open = !n._open;
      if (n._open && !n._loaded) await this.loadChildren(n);
    },
    async expandAll(open) {
      if (!open) { const close = (n) => { n._open = false; n._kids.forEach(close); }; this.tree.forEach(close); return; }
      // بنفتح أول مستوى بس (فتح كل الشجرة هيحمّلها كلها)
      for (const w of this.tree) { w._open = true; if (!w._loaded) await this.loadChildren(w); }
    },

    get visibleRows() {
      const rows = [];
      const walk = (n, depth) => {
        rows.push({ type: "node", key: keyOf(n), node: n, depth });
        if (n._open) {
          n._kids.forEach((c) => walk(c, depth + 1));
          if (n._kids.length < n._total) rows.push({ type: "more", key: `more:${keyOf(n)}`, parent: n, depth: depth + 1 });
        }
      };
      this.tree.forEach((w) => walk(w, 0));
      return rows;
    },

    // ---------- البحث (من السيرفر) ----------
    async runSearch() {
      const q = this.search.trim();
      if (!q) { this.results = []; return; }
      this.searching = true;
      try {
        const r = await api.get(`/api/locations/nav/search${qs({ q, include_warehouses: 1, limit: 40 })}`);
        if (q !== this.search.trim()) return;
        this.results = r.items; this.resultsTruncated = r.truncated;
      } catch (e) { toast(errMsg(e), "error"); }
      this.searching = false;
    },

    // ---------- التفاصيل ----------
    async select(n) {
      this.selected = n;
      this.det.search = "";
      await this.loadProducts(n, 1);
    },
    async loadProducts(n, page = 1) {
      if (!n) return;
      this.det.loading = true;
      try {
        const r = await api.get(`/api/locations/reports/stock${qs({
          warehouse_id: n.warehouse_id, location_id: n.kind === "location" ? n.id : "", page, page_size: this.det.page_size, search: this.det.search.trim(),
          breakdown_page_size: 1,
        })}`);
        Object.assign(this.det, { items: r.items, page: r.page, total_pages: r.total_pages, total: r.total });
      } catch (e) { toast(errMsg(e), "error"); }
      this.det.loading = false;
    },

    // ---------- مخزن ----------
    openWarehouse(w) { this.wh = { open: true, id: w ? w.id : null, name: w ? w.name : "", code: w ? w.code : "", saving: false, error: "" }; },
    async saveWarehouse() {
      const f = this.wh;
      if (!f.name || !f.code) { f.error = "اكتب اسم وكود المخزن"; return; }
      f.saving = true; f.error = "";
      try {
        const body = { name: f.name, code: f.code };
        if (f.id) await api.put(`/api/locations/warehouses/${f.id}`, body); else await api.post("/api/locations/warehouses", body);
        toast(f.id ? "تم تعديل المخزن" : "تمت إضافة المخزن");
        f.open = false; invalidateData(); this.reloadTree();
      } catch (e) { f.error = errMsg(e); }
      f.saving = false;
    },

    // ---------- موقع ----------
    // openLocation(loc) => تعديل | openLocation(null, parentNode) => إضافة تحت parentNode (مخزن أو موقع)
    openLocation(loc, parent) {
      if (loc) this.lc = { open: true, id: loc.id, name: loc.name, code: loc.code, type: loc.type || "", parentCode: "", warehouse_id: loc.warehouse_id, parent_id: loc.parent_id, saving: false, error: "" };
      else this.lc = {
        open: true, id: null, name: "", code: "", type: "", parentCode: parent.full_code, saving: false, error: "",
        warehouse_id: parent.warehouse_id, parent_id: parent.kind === "warehouse" ? null : parent.id,
      };
      if (!loc && parent) { parent._open = true; }
    },
    async saveLocation() {
      const f = this.lc;
      if (!f.name || !f.code) { f.error = "اكتب اسم وكود الموقع"; return; }
      f.saving = true; f.error = "";
      try {
        if (f.id) await api.put(`/api/locations/${f.id}`, { name: f.name, code: f.code, type: f.type || null });
        else {
          const body = { name: f.name, code: f.code, type: f.type || null };
          if (f.parent_id) body.parent_id = f.parent_id; else body.warehouse_id = f.warehouse_id;
          await api.post("/api/locations", body);
        }
        toast(f.id ? "تم تعديل الموقع" : "تمت إضافة الموقع");
        f.open = false; invalidateData(); this.reloadTree();
      } catch (e) { f.error = errMsg(e); }
      f.saving = false;
    },

    async setDefault(n) {
      const clear = n.is_default_receiving;
      try {
        await api.put(`/api/locations/warehouses/${n.warehouse_id}/default-location`, { location_id: clear ? null : n.id });
        toast(clear ? "تم إلغاء مكان الاستلام الافتراضي" : "تم تعيين مكان الاستلام الافتراضي");
        invalidateData(); this.reloadTree();
      } catch (e) { toast(errMsg(e), "error"); }
    },

    async remove(n) {
      const isWh = n.kind === "warehouse";
      const ok = await confirmAction({
        title: isWh ? "حذف المخزن؟" : "حذف الموقع؟", danger: true, confirmText: "حذف",
        message: `"${n.name}" (${n.full_code}) هيتحذف. ${isWh ? "لازم يكون من غير مواقع." : "لازم يكون فاضي ومن غير حركات مسجلة."}`,
      });
      if (!ok) return;
      try {
        await api.del(isWh ? `/api/locations/warehouses/${n.id}` : `/api/locations/${n.id}`);
        toast("تم الحذف"); this.selected = null; invalidateData(); this.reloadTree();
      } catch (e) { toast(errMsg(e), "error"); }
    },
  }));
});

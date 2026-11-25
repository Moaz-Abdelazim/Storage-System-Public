// =====================================================================
//  product.js — صفحة تفاصيل المنتج: البيانات، الصورة، توزيع المخزون، وسجل الحركة (ترقيم وفلترة من السيرفر).
//  الرابط: product.html#id=12
// =====================================================================
import { api, apiUrl, qs } from "./api.js";
import { can } from "./auth.js";
import { num, money, fmtDateTime, toast, errMsg, productStatus, confirmAction, getQuery, historyKind, signedQty, HISTORY_KIND } from "./utils.js";

const MAX_DIRECT_BYTES = 2 * 1024 * 1024;   // أكبر من كده بنصغّر الصورة في المتصفح قبل الرفع (صور الموبايل كبيرة)
const MAX_SIDE = 2048;

// تصغير صورة كبيرة قبل الرفع (JPEG). لو أي حاجة فشلت بنرجّع الملف الأصلي والسيرفر هو اللي بيحكم عليه.
async function prepareImage(file) {
  if (!file || file.size <= MAX_DIRECT_BYTES || !/^image\/(jpeg|png|webp)$/.test(file.type) || typeof createImageBitmap !== "function") return file;
  try {
    const bmp = await createImageBitmap(file);
    const scale = Math.min(1, MAX_SIDE / Math.max(bmp.width, bmp.height));
    const canvas = document.createElement("canvas");
    canvas.width = Math.round(bmp.width * scale); canvas.height = Math.round(bmp.height * scale);
    canvas.getContext("2d").drawImage(bmp, 0, 0, canvas.width, canvas.height);
    if (bmp.close) bmp.close();
    const blob = await new Promise((res) => canvas.toBlob(res, "image/jpeg", 0.9));
    return blob && blob.size < file.size ? new File([blob], (file.name || "photo").replace(/\.\w+$/, "") + ".jpg", { type: "image/jpeg" }) : file;
  } catch (e) { return file; }
}

const blankHistory = () => ({ items: [], total: 0, page: 1, total_pages: 1, summary: { in_quantity: 0, out_quantity: 0, transfers_quantity: 0, adjustments_net: 0, net_change: 0 }, opening_balance: null, balances_available: false });
const blankFilters = (size = 15) => ({ kind: "", warehouse_id: null, location_id: null, date_from: "", date_to: "", include_voided: false, order: "desc", size });

document.addEventListener("alpine:init", () => {
  window.Alpine.data("productPage", () => ({
    id: Number(getQuery("id")) || 0,
    product: null, loading: true, error: "",
    image: { busy: false, error: "" },
    canEdit: can("products.edit"),
    canHistory: ["invoices.view", "transfers.view", "reports.view", "stocktake.manage"].some((p) => can(p)),
    canLoc: ["locations.view", "stock_in.create", "stock_out.create", "transfers.create", "transfers.view", "products.adjust", "stocktake.manage"].some((p) => can(p)),
    whs: [], levels: [],
    hf: blankFilters(), hist: blankHistory(), loadingHist: false, histError: "",

    num, money, fmtDateTime, statusOf: productStatus,
    imgUrl: apiUrl,
    kindOf: historyKind, signed: signedQty,

    get kindOptions() {
      const keys = [];
      if (can("invoices.view")) keys.push("in", "out");
      if (can("transfers.view")) keys.push("transfer");
      if (can("stocktake.manage") || can("reports.view")) keys.push("stocktake");
      if (can("reports.view")) keys.push("adjustment");
      return keys.map((key) => ({ key, label: HISTORY_KIND[key].label }));
    },
    get distributionRows() {
      const locs = (this.product && this.product.locations) || [];
      const sorted = [...locs].sort((a, b) => (a.full_code || "").localeCompare(b.full_code || "", "en", { numeric: true }));
      let last = null;
      return sorted.map((l) => {
        const label = l.warehouse_name ? (l.warehouse_code && l.warehouse_code !== l.warehouse_name ? `${l.warehouse_name} (${l.warehouse_code})` : l.warehouse_name) : "—";
        const groupLabel = l.warehouse_id === last ? "" : label;
        last = l.warehouse_id;
        return { ...l, groupLabel };
      });
    },
    get distributionTotal() {
      const sum = ((this.product && this.product.locations) || []).reduce((s, l) => s + (Number(l.quantity) || 0), 0);
      return Math.round((sum + (Number(this.product && this.product.unassigned_quantity) || 0)) * 1000) / 1000;
    },
    get distributionMismatch() { return !!this.product && Math.abs(this.distributionTotal - (Number(this.product.quantity) || 0)) > 0.0005; },
    get hasFilters() { const f = this.hf; return !!(f.kind || f.warehouse_id || f.location_id || f.date_from || f.date_to || f.include_voided || f.order !== "desc"); },

    async init() {
      if (!this.id) { this.error = "رابط المنتج غير صحيح"; this.loading = false; return; }
      await this.loadProduct();
      if (this.product && this.canHistory) { this.initLocations(); this.loadHistory(); }
    },
    async reload() { await this.loadProduct(); if (this.product && this.canHistory) this.loadHistory(); },

    // ---------- المنتج ----------
    async loadProduct() {
      this.loading = true; this.error = "";
      try { this.product = await api.get(`/api/products/${this.id}`); document.title = `${this.product.name} — تفاصيل المنتج`; }
      catch (e) { this.product = null; this.error = e.status === 404 ? "المنتج مش موجود (ممكن يكون اتحذف)" : errMsg(e); }
      this.loading = false;
    },

    // ---------- الصورة ----------
    async pickImage(file) {
      if (!file || this.image.busy) return;
      this.image = { busy: true, error: "" };
      try {
        const ready = await prepareImage(file);
        const fd = new FormData();
        fd.append("file", ready, ready.name || "photo.jpg");
        const r = await api.put(`/api/products/${this.id}/image`, fd);
        this.product = { ...this.product, has_image: true, image_url: r.image_url, thumb_url: r.thumb_url };
        toast("تم حفظ الصورة");
      } catch (e) { this.image.error = errMsg(e); }
      this.image.busy = false;
    },
    async removeImage() {
      const ok = await confirmAction({ title: "حذف صورة المنتج؟", message: "الصورة هتتمسح نهائيًا من النظام.", confirmText: "حذف", danger: true });
      if (!ok) return;
      this.image = { busy: true, error: "" };
      try { await api.del(`/api/products/${this.id}/image`); this.product = { ...this.product, has_image: false, image_url: null, thumb_url: null }; toast("تم حذف الصورة"); }
      catch (e) { this.image.error = errMsg(e); }
      this.image.busy = false;
    },

    // ---------- سجل الحركة ----------
    historyParams() {
      const f = this.hf;
      return { kind: f.kind, warehouse_id: f.warehouse_id, location_id: f.location_id, date_from: f.date_from, date_to: f.date_to, include_voided: f.include_voided ? 1 : "", order: f.order, page: this.hist.page, page_size: f.size };
    },
    async loadHistory() {
      this.loadingHist = true; this.histError = "";
      try { this.hist = await api.get(`/api/products/${this.id}/history${qs(this.historyParams())}`); }
      catch (e) { this.histError = errMsg(e); this.hist = blankHistory(); }
      this.loadingHist = false;
    },
    applyHistory() { this.hist.page = 1; this.loadHistory(); },
    goHistory(p) { this.hist.page = p; this.loadHistory(); },
    toggleOrder() { this.hf.order = this.hf.order === "desc" ? "asc" : "desc"; this.applyHistory(); },
    resetHistory() { this.hf = blankFilters(this.hf.size); this.levels = []; this.applyHistory(); },

    // فلتر المخزن والموقع المتتالي (نفس منطق صفحة المنتجات)
    locLabel(l) { return l.name && l.name !== l.code ? `${l.code} — ${l.name}` : l.code; },
    async childrenOf(params) {
      const r = await api.get(`/api/locations/children${qs(params)}`, { ttl: 30_000 });
      return r.map((l) => ({ id: l.id, label: this.locLabel(l) }));
    },
    async initLocations() {
      if (!this.canLoc) return;
      try { this.whs = await api.get("/api/locations/warehouses", { ttl: 30_000 }); } catch (e) { this.whs = []; }
    },
    async setWarehouse(id) {
      this.hf.warehouse_id = Number(id) || null; this.hf.location_id = null; this.levels = [];
      if (this.hf.warehouse_id) {
        try { const opts = await this.childrenOf({ warehouse_id: this.hf.warehouse_id }); if (opts.length) this.levels = [{ options: opts, value: "" }]; }
        catch (e) { toast(errMsg(e), "error"); }
      }
      this.applyHistory();
    },
    async setLevel(i, id) {
      this.levels = this.levels.slice(0, i + 1);
      const val = Number(id) || "";
      this.levels[i].value = val;
      this.hf.location_id = val || (i > 0 ? this.levels[i - 1].value : null) || null;
      if (val) {
        try { const opts = await this.childrenOf({ parent_id: val }); if (opts.length) this.levels.push({ options: opts, value: "" }); }
        catch (e) { toast(errMsg(e), "error"); }
      }
      this.applyHistory();
    },
  }));
});

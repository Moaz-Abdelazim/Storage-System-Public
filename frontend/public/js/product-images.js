// =====================================================================
//  product-images.js — رفع صور المنتجات بالجملة (ZIP / صور / مجلد) + شاشة المطابقة.
//  1) رفع => السيرفر بيعالج الصور ويطابقها بالـ SKU/الباركود  2) مراجعة واختيار اليدوي  3) تطبيق.
// =====================================================================
import { api, apiUrl, qs } from "./api.js";
import { toast, errMsg, confirmAction } from "./utils.js";

const MAX_TOTAL = 64 * 1024 * 1024;
const STATUS = {
  matched: { label: "متطابقة", cls: "badge-ok" },
  unmatched: { label: "غير متطابقة", cls: "badge-warn" },
  ambiguous: { label: "تحتاج اختيار", cls: "badge-warn" },
  duplicate: { label: "مكررة لنفس المنتج", cls: "badge-mute" },
  invalid: { label: "فيها مشكلة", cls: "badge-bad" },
};

document.addEventListener("alpine:init", () => {
  window.Alpine.data("imageBatch", () => ({
    step: "upload", drag: false, uploading: false, applying: false, error: "",
    batch: null, tab: "all", replaceExisting: false, result: null,
    picker: { open: false, item: null, q: "", results: [], loading: false },
    imgUrl: apiUrl,
    statusOf: (it) => STATUS[it.status] || STATUS.invalid,

    get todoCount() { return this.batch ? this.batch.counts.unmatched + this.batch.counts.ambiguous + this.batch.counts.duplicate : 0; },
    get visibleItems() {
      if (!this.batch) return [];
      const f = { all: () => true, matched: (i) => i.status === "matched", invalid: (i) => i.status === "invalid", todo: (i) => ["unmatched", "ambiguous", "duplicate"].includes(i.status) }[this.tab];
      return this.batch.items.filter(f);
    },

    async upload(fileList) {
      const files = Array.from(fileList || []);
      if (!files.length || this.uploading) return;
      this.error = "";
      if (files.reduce((s, f) => s + f.size, 0) > MAX_TOTAL) { this.error = "حجم الملفات أكبر من 64 ميجا، قسّمها على دفعات."; return; }
      const fd = new FormData();
      files.forEach((f) => fd.append("files", f, f.name));
      this.uploading = true;
      try {
        this.batch = await api.upload("/api/product-images/batches", fd);
        this.tab = this.todoCount ? "todo" : "all";
        this.step = "review";
      } catch (e) { this.error = errMsg(e); }
      this.uploading = false;
    },

    async assign(item, productId) {
      this.error = "";
      try { this.batch = await api.put(`/api/product-images/batches/${this.batch.token}/items/${item.id}`, { product_id: productId }); }
      catch (e) { toast(errMsg(e), "error"); }
    },

    openPicker(item) {
      this.picker = { open: true, item, q: "", results: [], loading: false };
      this.$nextTick(() => this.$refs.pickerInput && this.$refs.pickerInput.focus());
    },
    async searchProducts() {
      const q = this.picker.q.trim();
      if (!q) { this.picker.results = []; return; }
      this.picker.loading = true;
      try { this.picker.results = await api.get(`/api/products/lookup${qs({ q, limit: 15 })}`); } catch (e) { toast(errMsg(e), "error"); }
      this.picker.loading = false;
    },

    async apply() {
      const n = this.batch.counts.matched;
      const ok = await confirmAction({
        title: "تطبيق الصور؟",
        message: `هيتم ربط ${n} صورة بمنتجاتها${this.replaceExisting ? " (واستبدال الصور الموجودة)" : ""}. الصور غير المتطابقة (${this.batch.total - n}) مش هتتحفظ.`,
        confirmText: "تطبيق",
      });
      if (!ok) return;
      this.applying = true; this.error = "";
      try {
        this.result = await api.post(`/api/product-images/batches/${this.batch.token}/apply`, { replace_existing: this.replaceExisting });
        this.step = "done"; this.batch = null;
        toast(`تم ربط ${this.result.applied} صورة`);
      } catch (e) { this.error = errMsg(e); }
      this.applying = false;
    },

    async cancel() {
      const ok = await confirmAction({ title: "إلغاء الدفعة؟", message: "الصور المرفوعة هتتمسح ومش هتتحفظ.", confirmText: "إلغاء الدفعة", danger: true });
      if (!ok) return;
      try { await api.del(`/api/product-images/batches/${this.batch.token}`); } catch (e) { /* الدفعة ممكن تكون انتهت: مش مشكلة */ }
      this.reset();
    },
    reset() { this.step = "upload"; this.batch = null; this.result = null; this.error = ""; this.tab = "all"; },
  }));
});

// =====================================================================
//  invoice.js — تفاصيل فاتورة وارد/صادر (قابلة للطباعة).
// =====================================================================
import { api } from "./api.js";
import { num, money, fmtDateTime, errMsg, KIND, getQuery, toast, confirmAction } from "./utils.js";
import { invalidateData } from "./data.js";

document.addEventListener("alpine:init", () => {
  window.Alpine.data("invoice", () => ({
    num, money, fmtDateTime,
    kind: getQuery("kind") === "out" ? "out" : "in", id: Number(getQuery("id")) || 0,
    inv: null, loading: true, error: "", busy: false,

    get meta() { return KIND[this.kind]; },
    get partyName() { return this.inv && (this.kind === "in" ? this.inv.supplier_name : this.inv.recipient_name); },
    get userName() { return this.inv && (this.kind === "in" ? this.inv.received_by_name : this.inv.delivered_by_name); },
    get totalQty() { return this.inv ? this.inv.items.reduce((s, i) => s + i.quantity, 0) : 0; },

    // فاتورة صادر معلّقة: الكمية اتخصمت، وممكن تتعدّل أو تتمسح أو تتقفل
    get isHeld() { return !!this.inv && this.kind === "out" && this.inv.status === "held"; },
    get hasAdj() { return !!this.inv && this.kind === "out" && (this.inv.discount_amount > 0 || this.inv.tax_amount > 0); },
    get finalTotal() { return this.inv && this.kind === "out" && this.inv.grand_total != null ? this.inv.grand_total : this.inv.total_value; },
    adjLabel(base, type, value) { return type === "percent" ? `${base} (${num(value)}%)` : base; },

    async complete() {
      const ok = await confirmAction({ title: "إتمام الفاتورة", message: "الفاتورة هتتقفل نهائيًا وما هينفعش تتعدّل أو تتمسح بعد كده. تكمل؟", confirmText: "إتمام" });
      if (!ok) return;
      this.busy = true;
      try { this.inv = await api.post(`/api/stock/out/${this.id}/complete`); invalidateData(); toast("تم إتمام الفاتورة"); }
      catch (e) { toast(errMsg(e), "error"); }
      this.busy = false;
    },
    async remove() {
      const ok = await confirmAction({ title: "مسح الفاتورة المعلّقة", message: "الفاتورة هتتمسح نهائيًا والكميات هترجع للمخزون في نفس أماكنها. مفيش رجوع في الخطوة دي.", confirmText: "مسح", danger: true });
      if (!ok) return;
      this.busy = true;
      try { await api.del(`/api/stock/out/${this.id}`); invalidateData(); toast("تم مسح الفاتورة وإرجاع الكميات"); location.replace("movements.html#kind=out"); return; }
      catch (e) { toast(errMsg(e), "error"); }
      this.busy = false;
    },

    init() { this.load(); },
    async load() {
      if (!this.id) { this.loading = false; this.error = "الرابط مش فيه رقم فاتورة. افتح الفاتورة من سجل الحركات أو من آخر الفواتير."; return; }
      this.loading = true; this.error = "";
      try { this.inv = await api.get(`/api/stock/${this.kind}/${this.id}`); } catch (e) { this.error = errMsg(e); }
      this.loading = false;
    },
  }));
});

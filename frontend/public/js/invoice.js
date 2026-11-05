// =====================================================================
//  invoice.js — تفاصيل إذن إضافة/صرف (قابل للطباعة) + انتقالات الحالة بحسب الصلاحيات.
//  إذن الصرف: عرض سعر ← محجوزة ← تمت (+ ملغاة بسبب إلزامي). السيرفر هو اللي بيفرض الصلاحيات والحالات؛ الأزرار هنا تلميحات.
// =====================================================================
import { api } from "./api.js";
import { can } from "./auth.js";
import { num, money, fmtDateTime, errMsg, KIND, getQuery, toast, confirmAction, docStatus } from "./utils.js";
import { invalidateData } from "./data.js";

document.addEventListener("alpine:init", () => {
  window.Alpine.data("invoice", () => ({
    num, money, fmtDateTime, docStatus,
    canQuote: can("stock_out.quote"), canReserve: can("stock_out.reserve"), canComplete: can("stock_out.complete"),
    canVoid: can("invoices.void"), canPrint: can("invoices.print"),
    kind: getQuery("kind") === "out" ? "out" : "in", id: Number(getQuery("id")) || 0,
    inv: null, loading: true, error: "", busy: false,
    voidDlg: { open: false, reason: "", recreate: false, error: "" },

    get meta() { return KIND[this.kind]; },
    get status() { return (this.inv && this.inv.status) || "completed"; },
    get isOut() { return this.kind === "out"; },
    get partyName() { return this.inv && (this.kind === "in" ? this.inv.supplier_name : this.inv.recipient_name); },
    get userName() { return this.inv && (this.kind === "in" ? this.inv.received_by_name : this.inv.delivered_by_name); },
    get totalQty() { return this.inv ? this.inv.items.reduce((s, i) => s + i.quantity, 0) : 0; },
    get docTitle() { return this.kind === "in" ? "إذن إضافة" : ({ quote: "عرض سعر", reserved: "إذن حجز" }[this.status] || "إذن صرف"); },
    get notice() {
      if (this.status === "quote") return "عرض سعر — غير مؤثر على المخزون";
      if (this.status === "reserved") return "إذن حجز — الكميات محجوزة ولم تخرج من المخزن";
      return "";
    },

    // أزرار الانتقال (بحسب الحالة والصلاحية)
    get canEdit() { return this.isOut && ((this.status === "quote" && this.canQuote) || (this.status === "reserved" && this.canReserve)); },
    get canDoReserve() { return this.isOut && this.status === "quote" && this.canReserve; },
    get canDoRelease() { return this.isOut && this.status === "reserved" && this.canReserve; },
    get canDoComplete() { return this.isOut && this.status === "reserved" && this.canComplete; },
    get canDoVoid() {
      if (!this.inv || this.status === "voided") return false;
      if (this.kind === "in") return this.canVoid;
      return this.canVoid || (this.status === "quote" && this.canQuote) || (this.status === "reserved" && this.canReserve);
    },
    get canRecreate() { return this.isOut && this.status === "voided" && this.canVoid && this.canQuote; },

    get hasAdj() { return !!this.inv && this.isOut && (this.inv.discount_amount > 0 || this.inv.tax_amount > 0); },
    get finalTotal() { return this.inv && this.isOut && this.inv.grand_total != null ? this.inv.grand_total : this.inv && this.inv.total_value; },
    adjLabel(base, type, value) { return type === "percent" ? `${base} (${num(value)}%)` : base; },

    async act(path, { title, message, confirmText, ok }) {
      if (!(await confirmAction({ title, message, confirmText }))) return;
      this.busy = true;
      try { this.inv = await api.post(`/api/stock/out/${this.id}/${path}`); invalidateData(); toast(ok); }
      catch (e) { toast(errMsg(e), "error"); }
      this.busy = false;
    },
    reserve() { return this.act("reserve", { title: "حجز الكميات", message: "هنحجز الكميات من الأماكن المحددة (المتاح هيقل، والكمية الفعلية في المخزن هتفضل زي ما هي). نكمل؟", confirmText: "حجز", ok: "تم حجز الكميات" }); },
    release() { return this.act("release", { title: "فك الحجز", message: "الكميات هترجع متاحة والمستند هيرجع عرض سعر.", confirmText: "فك الحجز", ok: "تم فك الحجز" }); },
    complete() { return this.act("complete", { title: "إتمام الصرف", message: "الكميات المحجوزة هتخرج من المخزن فعليًا من نفس الأماكن، والمستند هيتقفل. تكمل؟", confirmText: "إتمام الصرف", ok: "تم إتمام الصرف" }); },

    openVoid() { this.voidDlg = { open: true, reason: "", recreate: false, error: "" }; },
    async submitVoid() {
      const d = this.voidDlg, reason = d.reason.trim();
      if (reason.length < 3) { d.error = "اكتب سبب الإلغاء (3 حروف على الأقل)"; return; }
      this.busy = true; d.error = "";
      try {
        const r = await api.post(`/api/stock/${this.kind}/${this.id}/void`, { reason, recreate: this.isOut && d.recreate });
        invalidateData(); d.open = false;
        if (r.recreated_invoice_id) { toast("تم الإلغاء وإنشاء عرض سعر جديد"); location.href = `invoice.html#kind=out&id=${r.recreated_invoice_id}`; location.reload(); return; }
        this.inv = r; toast("تم إلغاء المستند");
      } catch (e) { d.error = errMsg(e); }
      this.busy = false;
    },
    async recreate() {
      if (!(await confirmAction({ title: "إعادة إنشاء كعرض سعر", message: "هيتعمل عرض سعر جديد بنفس الأصناف وبأسعار المنتجات الحالية (مفيش أي حركة مخزون).", confirmText: "إنشاء" }))) return;
      this.busy = true;
      try { const r = await api.post(`/api/stock/out/${this.id}/recreate`); invalidateData(); toast("تم إنشاء عرض السعر"); location.href = `invoice.html#kind=out&id=${r.id}`; location.reload(); return; }
      catch (e) { toast(errMsg(e), "error"); }
      this.busy = false;
    },

    init() { this.load(); },
    async load() {
      if (!this.id) { this.loading = false; this.error = "الرابط مش فيه رقم مستند. افتحه من سجل الحركات أو من آخر المستندات."; return; }
      this.loading = true; this.error = "";
      try { this.inv = await api.get(`/api/stock/${this.kind}/${this.id}`); } catch (e) { this.error = errMsg(e); }
      this.loading = false;
    },
  }));
});

// =====================================================================
//  invoice-form.js — شاشة فاتورة وارد/صادر (واجهة تشبه الفاتورة بسطور منتجات).
//  kind: "in" (مورد + مكان استلام) | "out" (جهة مستلمة + صرف من مكان فيه رصيد).
// =====================================================================
import { api } from "./api.js";
import { invalidateData } from "./data.js";
import { num, money, fmtDateTime, toast, errMsg, getQuery } from "./utils.js";

let lineKey = 0;
const round2 = (v) => Math.round((Number(v) + Number.EPSILON) * 100) / 100;
const blankLine = (loc = null) => ({
  key: ++lineKey, product: null, option: null, quantity: null, unit_price: null,
  location: loc, locData: null, locOptions: [], over: false,
});

export function registerInvoice(name, kind) {
  const isIn = kind === "in";
  const partyEndpoint = isIn ? "/api/suppliers" : "/api/recipients";

  document.addEventListener("alpine:init", () => {
    window.Alpine.data(name, () => ({
      num, money, fmtDateTime,
      partyId: null, notes: "", lines: [blankLine()],
      saving: false, error: "", done: null,
      recent: [], recentLoading: true,
      newParty: { open: false, name: "", phone: "", error: "", saving: false },
      // ---- خاص بالصادر: تعليق الفاتورة + خصم/ضريبة على إجمالي الفاتورة + تعديل فاتورة معلّقة (stock-out.html#edit=ID) ----
      finalize: "completed", discount: { type: "amount", value: null }, tax: { type: "percent", value: null },
      editId: isIn ? 0 : Number(getQuery("edit")) || 0, editLoading: false, editError: "",

      init() { this.loadRecent(); if (this.editId) this.loadForEdit(); },

      // ---------- السطور ----------
      get filledLines() { return this.lines.filter((l) => l.product && Number(l.quantity) > 0); },
      get totalQty() { return this.filledLines.reduce((s, l) => s + Number(l.quantity), 0); },
      get totalValue() { return this.filledLines.reduce((s, l) => s + this.lineTotal(l), 0); },
      lineTotal(l) { return (Number(l.quantity) || 0) * (Number(l.unit_price) || 0); },

      // معاينة الإجماليات (الحساب الأساسي في الباك اند - ده للعرض بس)
      get subtotal() { return round2(this.totalValue); },
      adjAmount(a, base) { const v = Number(a.value) || 0; return v > 0 ? round2(a.type === "percent" ? (base * v) / 100 : v) : 0; },
      get discountAmt() { return this.adjAmount(this.discount, this.subtotal); },
      get taxAmt() { return this.adjAmount(this.tax, round2(this.subtotal - this.discountAmt)); },
      get grandTotal() { return round2(this.subtotal - this.discountAmt + this.taxAmt); },
      adjPayload() {
        const out = {};
        const d = Number(this.discount.value) || 0, t = Number(this.tax.value) || 0;
        if (d > 0) { out.discount_type = this.discount.type; out.discount_value = d; }
        if (t > 0) { out.tax_type = this.tax.type; out.tax_value = t; }
        return out;
      },

      addLine() {
        const last = this.lines[this.lines.length - 1];
        // في الوارد: السطر الجديد بياخد نفس مكان الاستلام بتاع اللي قبله (تسريع الإدخال)
        this.lines.push(blankLine(isIn && last ? last.location : null));
      },
      removeLine(i) { if (this.lines.length > 1) this.lines.splice(i, 1); },

      async pickProduct(l, option) {
        l.option = option || null;
        l.product = option ? option.data : null;
        l.locOptions = []; l.locData = null; l.over = false;
        if (!option) { if (!isIn) l.location = null; return; }
        if (!isIn) {
          l.unit_price = l.product.price || 0;
          l.location = null;
          try {
            const p = await api.get(`/api/products/${l.product.id}`);
            l.locOptions = p.locations.map((x) => ({ value: x.location_id, label: x.full_code, sub: `المتاح ${num(x.quantity)} ${p.unit || ""}`, data: x }));
            if (l.locOptions.length === 1) { l.location = l.locOptions[0].value; l.locData = l.locOptions[0].data; }
          } catch (e) { toast(errMsg(e), "error"); }
        }
      },

      // ---------- التحقق ----------
      validate() {
        this.lines.forEach((l) => (l.over = false));
        const used = this.lines.filter((l) => l.product || Number(l.quantity) > 0);
        if (!used.length) return "أضف منتج واحد على الأقل";
        const used_qty = new Map();
        for (const [i, l] of used.entries()) {
          const n = `السطر ${this.lines.indexOf(l) + 1}`;
          if (!l.product) return `${n}: اختار المنتج`;
          if (!(Number(l.quantity) > 0)) return `${n}: اكتب كمية صحيحة`;
          if (l.unit_price != null && l.unit_price !== "" && Number(l.unit_price) < 0) return `${n}: السعر لا يمكن يكون سالب`;
          if (!l.location) return `${n}: ${isIn ? "اختار مكان الاستلام" : "اختار مكان الصرف"}`;
          if (!isIn) {
            const k = `${l.product.id}:${l.location}`;
            used_qty.set(k, (used_qty.get(k) || 0) + Number(l.quantity));
            const avail = l.locData ? l.locData.quantity : 0;
            if (used_qty.get(k) > avail) { l.over = true; return `${n}: الكمية أكبر من المتاح (${num(avail)}) في ${l.locData.full_code}`; }
          }
        }
        if (!isIn) {
          for (const [label, a] of [["الخصم", this.discount], ["الضريبة", this.tax]]) {
            const v = Number(a.value) || 0;
            if (v < 0) return `قيمة ${label} لا يمكن تكون سالبة`;
            if (a.type === "percent" && v > 100) return `نسبة ${label} لا يمكن تكون أكبر من 100%`;
          }
          if (this.discountAmt > this.subtotal) return "الخصم أكبر من إجمالي الفاتورة";
        }
        return "";
      },

      async submit(completeAfter = false) {
        this.error = this.validate();
        if (this.error) return;
        const items = this.lines.filter((l) => l.product).map((l) => {
          const item = { product_id: l.product.id, quantity: Number(l.quantity) };
          // الصادر: السعر بيتاخد من المنتج في السيرفر (مش بنبعته). الوارد: سعر الشراء اختياري
          if (isIn) item.unit_price = l.unit_price === null || l.unit_price === "" ? null : Number(l.unit_price);
          if (isIn) {
            const [t, id] = String(l.location).split(":");
            if (t === "w") item.warehouse_id = Number(id); else item.location_id = Number(id);
          } else item.location_id = Number(l.location);
          return item;
        });
        const body = { notes: this.notes || null, items };
        body[isIn ? "supplier_id" : "recipient_id"] = this.partyId || null;
        if (!isIn) { Object.assign(body, this.adjPayload()); if (!this.editId) body.status = this.finalize; }
        this.saving = true;
        try {
          if (this.editId) {
            await api.put(`/api/stock/out/${this.editId}`, body);
            if (completeAfter) {
              try { await api.post(`/api/stock/out/${this.editId}/complete`); }
              catch (e) { toast("اتحفظت التعديلات بس الإتمام فشل: " + errMsg(e), "error"); }
            }
            invalidateData();
            toast("تم حفظ التعديلات");
            location.href = `invoice.html#kind=out&id=${this.editId}`;
            return;
          }
          const inv = await api.post(isIn ? "/api/stock/in" : "/api/stock/out", body);
          this.done = { id: inv.id, held: inv.status === "held" };
          toast(isIn ? "تم تسجيل فاتورة الوارد" : "تم تسجيل فاتورة الصادر");
          invalidateData();
          this.reset(true);
          this.loadRecent();
          window.scrollTo({ top: 0, behavior: "smooth" });
        } catch (e) { this.error = errMsg(e); }
        this.saving = false;
      },

      reset(keepDone = false) {
        this.partyId = null; this.notes = ""; this.lines = [blankLine()]; this.error = "";
        this.finalize = "completed"; this.discount = { type: "amount", value: null }; this.tax = { type: "percent", value: null };
        if (!keepDone) this.done = null;
        this.$refs.partySel.clear();
      },

      // تحميل فاتورة صادر معلّقة للتعديل (طلب واحد: الأسطر + أماكن السحب المتاحة بعد إرجاع كمياتها)
      async loadForEdit() {
        this.editLoading = true; this.editError = "";
        try {
          const d = await api.get(`/api/stock/out/${this.editId}/edit-data`);
          this.partyId = d.recipient_id; this.notes = d.notes || "";
          this.discount = { type: d.discount_type || "amount", value: d.discount_value };
          this.tax = { type: d.tax_type || "percent", value: d.tax_value };
          this.lines = d.lines.map((x) => {
            const product = { id: x.product_id, name: x.product_name, sku: x.sku, unit: x.unit, price: x.product_price, quantity: x.product_quantity };
            const locOptions = x.locations.map((o) => ({ value: o.location_id, label: o.full_code, sub: `المتاح ${num(o.quantity)} ${x.unit || ""}`, data: o }));
            const cur = locOptions.find((o) => o.value === x.location_id);
            return { ...blankLine(), product, option: { value: product.id, label: product.name, sub: `${product.sku ? product.sku + " · " : ""}${num(product.quantity)} ${product.unit || ""}`, data: product },
              quantity: x.quantity, unit_price: x.unit_price, location: x.location_id, locData: cur ? cur.data : null, locOptions };
          });
          if (!this.lines.length) this.lines = [blankLine()];
          this.$nextTick(() => { if (d.recipient_id) this.$refs.partySel.setSelected({ value: d.recipient_id, label: d.recipient_name || "", sub: "" }); });
        } catch (e) { this.editError = errMsg(e); }
        this.editLoading = false;
      },

      async loadRecent() {
        this.recentLoading = true;
        try { this.recent = (await api.get(`/api/stock/log?kind=${kind}&page=1&page_size=6`)).items; } catch (e) { /* مش حرج */ }
        this.recentLoading = false;
      },

      // ---------- جهة جديدة ----------
      async saveParty() {
        const p = this.newParty;
        if (!p.name) { p.error = "اكتب الاسم"; return; }
        p.saving = true; p.error = "";
        try {
          const created = await api.post(partyEndpoint, { name: p.name, phone: p.phone || null, address: null, notes: null });
          invalidateData();
          await this.$refs.partySel.reload();
          this.$refs.partySel.setSelected({ value: created.id, label: created.name, sub: created.phone || "" });
          this.partyId = created.id;
          p.open = false;
          toast("تمت الإضافة");
        } catch (e) { p.error = errMsg(e); }
        p.saving = false;
      },
    }));
  });
}

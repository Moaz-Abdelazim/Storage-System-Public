// =====================================================================
//  stocktake.js — الجرد: إنشاء جلسة ← إدخال/استيراد الكميات المعدودة ← مراجعة الفروقات ← اعتماد (يسوّي الأرصدة).
//  التسوية بتحصل وقت الاعتماد بس (stocktake.approve). الفرق بيتطبّق على الرصيد الحالي فحركة وقت العدّ مبتضيعش.
// =====================================================================
import { api, qs, download } from "./api.js";
import { can } from "./auth.js";
import { invalidateData } from "./data.js";
import { num, money, fmtDateTime, toast, errMsg, getQuery, setQuery, confirmAction } from "./utils.js";

const STATUS = {
  draft: { label: "مسودة", cls: "badge-mute" }, counting: { label: "جاري العدّ", cls: "badge-info" },
  review: { label: "مراجعة", cls: "badge-warn" }, approved: { label: "معتمد", cls: "badge-ok" }, cancelled: { label: "ملغي", cls: "badge-bad" },
};

document.addEventListener("alpine:init", () => {
  window.Alpine.data("stocktake", () => ({
    num, money, fmtDateTime, STATUS,
    canApprove: can("stocktake.approve"), canFinance: can("finance.view"),
    // القايمة
    list: { rows: [], page: 1, pages: 1, total: 0, size: 10, loading: true, status: "", error: "" },
    create: { open: false, scope: null, note: "", saving: false, error: "" },
    // التفاصيل
    id: Number(getQuery("id")) || 0, st: null, sum: null, lines: [], page: 1, pages: 1, total: 0, size: 50, loading: false, error: "",
    f: { search: "", diff: false, uncounted: false }, dirty: {}, saving: false, importing: false, importErrors: null, busy: false,
    add: { open: false, product: null, location: null, qty: null, error: "" },
    cancelDlg: { open: false, reason: "", saving: false, error: "" },
    adj: { rows: [], loading: false, error: "" }, // سجل التسوية (للجلسات المعتمدة)

    get editable() { return !!this.st && ["draft", "counting"].includes(this.st.status); },
    get dirtyCount() { return Object.keys(this.dirty).length; },

    init() {
      this.id ? this.load() : this.loadList();
      window.addEventListener("beforeunload", (e) => { if (this.dirtyCount) { e.preventDefault(); e.returnValue = ""; } });
      // زرار الرجوع/التقدّم في المتصفح أو تغيير الـ hash: نتزامن مع الـ URL من غير reload
      window.addEventListener("popstate", () => this.syncFromLocation());
      window.addEventListener("hashchange", () => this.syncFromLocation());
    },

    // ---------- التنقل بين القايمة والتفاصيل (من غير location.reload: كان بيلغي التنقل على Vercel بسبب cleanUrls) ----------
    resetDetail() {
      this.st = null; this.sum = null; this.lines = []; this.page = 1; this.pages = 1; this.total = 0; this.error = "";
      this.f = { search: "", diff: false, uncounted: false }; this.dirty = {}; this.importErrors = null;
      this.add = { open: false, product: null, location: null, qty: null, error: "" };
      this.cancelDlg = { open: false, reason: "", saving: false, error: "" };
      this.adj = { rows: [], loading: false, error: "" };
    },
    async syncFromLocation() {
      const next = Number(getQuery("id")) || 0;
      if (next === this.id) return;
      if (this.id && this.dirtyCount) await this.flush(); // نحفظ العدّ المعلّق قبل ما نسيب الجلسة (بالـ id القديم)
      this.resetDetail(); this.id = next;
      this.id ? this.load() : this.loadList();
    },
    navigate(id) {
      history.pushState(null, "", location.pathname + location.search + (id ? `#id=${id}` : ""));
      return this.syncFromLocation();
    },

    // ---------- القايمة ----------
    async loadList() {
      const l = this.list; l.loading = true; l.error = "";
      try { const r = await api.get(`/api/stocktakes${qs({ page: l.page, page_size: l.size, status: l.status })}`); l.rows = r.items; l.total = r.total; l.pages = r.total_pages; l.page = r.page; }
      catch (e) { l.error = errMsg(e); }
      l.loading = false;
    },
    setStatus(s) { this.list.status = s; this.list.page = 1; this.loadList(); },
    open(id) { return this.navigate(id); },
    openFromRow(e, id) { if (e.target.closest("a, button, input, select, textarea, label")) return; this.open(id); },
    async back() { if (!(await this.flush())) return; this.navigate(0); },

    async createSession() {
      const c = this.create; c.error = "";
      if (!c.scope) { c.error = "اختار المخزن أو الموقع المطلوب جرده"; return; }
      const [kind, id] = String(c.scope).split(":");
      c.saving = true;
      try {
        const r = await api.post("/api/stocktakes", { [kind === "w" ? "warehouse_id" : "location_id"]: Number(id), note: c.note.trim() || null });
        toast(`تم إنشاء جلسة الجرد (${r.lines_total} صنف)`); this.create = { open: false, scope: null, note: "", saving: false, error: "" }; this.open(r.id); return;
      } catch (e) { c.error = errMsg(e); }
      c.saving = false;
    },

    // ---------- التفاصيل ----------
    async load() {
      this.loading = true; this.error = "";
      try {
        const r = await api.get(`/api/stocktakes/${this.id}${qs({ page: this.page, page_size: this.size, search: this.f.search.trim(), only_diff: this.f.diff ? 1 : "", only_uncounted: this.f.uncounted ? 1 : "" })}`);
        this.st = r.stocktake; this.sum = r.summary; this.lines = r.items; this.total = r.total; this.pages = r.total_pages; this.page = r.page;
        if (this.st.status === "approved" && !this.adj.rows.length) this.loadAdjustments();
      } catch (e) { this.error = errMsg(e); }
      this.loading = false;
    },
    async loadAdjustments() {
      const a = this.adj; a.loading = true; a.error = "";
      try { a.rows = (await api.get(`/api/stocktakes/${this.id}/adjustments`)).items; } catch (e) { a.error = errMsg(e); }
      a.loading = false;
    },
    apply() { this.page = 1; this.load(); },
    async go(p) { if (await this.flush()) { this.page = p; this.load(); } },

    // القيمة المعروضة في خانة العدّ: التعديل غير المحفوظ له الأولوية
    shown(l) { return l.line_id in this.dirty ? this.dirty[l.line_id] : (l.counted_qty ?? ""); },
    setCount(l, v) { const t = String(v).trim(); this.dirty = { ...this.dirty, [l.line_id]: t === "" ? null : Number(t) }; },
    diffOf(l) { const v = this.shown(l); return v === "" || v === null ? null : Math.round((Number(v) - l.system_qty) * 1000) / 1000; },
    // حالة السطر (بتتحسب من القيمة المعروضة، فبتتحدّث مع الكتابة قبل الحفظ)
    lineStatus(l) {
      const d = this.diffOf(l);
      if (d === null) return { label: "لم يُعدّ", cls: "badge-mute" };
      if (d === 0) return { label: "مطابق", cls: "badge-ok" };
      return d > 0 ? { label: "زيادة", cls: "badge-info" } : { label: "عجز", cls: "badge-bad" };
    },
    // Enter في خانة العدّ بينقل للسطر اللي بعده، وفي آخر سطر بيحفظ
    focusNext(e) {
      const all = [...document.querySelectorAll("[data-count-input]:not([disabled])")];
      const nxt = all[all.indexOf(e.target) + 1];
      if (nxt) { nxt.focus(); nxt.select(); } else this.save();
    },
    get progressPct() { const s = this.sum; return s && s.lines_total ? Math.round((s.counted / s.lines_total) * 100) : 0; },
    diffCls(l) { const d = this.diffOf(l); return d === null || d === 0 ? "" : d > 0 ? "text-brand-600" : "text-red-600"; },

    // حفظ التعديلات المعلّقة (بيرجع false لو فشل)
    async flush() {
      const ids = Object.keys(this.dirty); if (!ids.length) return true;
      for (const k of ids) { const v = this.dirty[k]; if (v !== null && (!Number.isFinite(v) || v < 0)) { toast("في كمية غير صالحة (لازم رقم موجب)", "error"); return false; } }
      this.saving = true;
      try {
        const all = ids.map((k) => ({ line_id: Number(k), counted_qty: this.dirty[k] }));
        for (let i = 0; i < all.length; i += 500) await api.put(`/api/stocktakes/${this.id}/counts`, { counts: all.slice(i, i + 500) });
        this.dirty = {}; this.saving = false; return true;
      } catch (e) { toast(errMsg(e), "error"); this.saving = false; return false; }
    },
    async save() { if (await this.flush()) { toast("تم حفظ العدّ"); this.load(); } },

    async transition(path, { title, message, confirmText, ok, danger = false }) {
      if (this.busy) return; // يمنع الضغط المزدوج (اعتماد مرتين)
      this.busy = true;
      try {
        if (!(await this.flush())) return;
        if (!(await confirmAction({ title, message, confirmText, danger }))) return;
        try { await api.post(`/api/stocktakes/${this.id}/${path}`); invalidateData(); toast(ok); await this.load(); }
        catch (e) { toast(errMsg(e), "error"); await this.load(); } // نعيد التحميل: ممكن حد غيّر الحالة قبلنا
      } finally { this.busy = false; }
    },
    submit() { return this.transition("submit", { title: "إرسال للمراجعة", message: "هتتقفل خانات العدّ لحد ما يتم الاعتماد أو الرجوع للعدّ. الأصناف اللي ماتعدّتش هتفضل كما هي.", confirmText: "إرسال", ok: "اتبعت للمراجعة" }); },
    reopen() { return this.transition("reopen", { title: "الرجوع للعدّ", message: "هترجع الجلسة لحالة العدّ وتقدر تعدّل الكميات.", confirmText: "رجوع", ok: "رجعت للعدّ" }); },
    // إلغاء الجلسة: نافذة بسبب اختياري (مفيش أي تسوية بتحصل)
    async cancel() { if (this.busy || !(await this.flush())) return; this.cancelDlg = { open: true, reason: "", saving: false, error: "" }; },
    async confirmCancel() {
      const c = this.cancelDlg; if (c.saving) return;
      c.saving = true; c.error = "";
      try {
        await api.post(`/api/stocktakes/${this.id}/cancel`, { reason: c.reason.trim() || null });
        invalidateData(); c.open = false; toast("تم إلغاء الجرد"); await this.load();
      } catch (e) { c.error = errMsg(e); }
      c.saving = false;
    },
    approve() {
      const s = this.sum || {};
      const unc = s.uncounted || 0;
      const warn = unc ? `تنبيه: فيه ${num(unc)} سطر لسه ماتعدّش، وهيفضل رصيده زي ما هو من غير أي تسوية.` : "كل الأسطر معدودة.";
      return this.transition("approve", { title: "اعتماد الجرد وتسوية الأرصدة", danger: true, confirmText: "اعتماد وتسوية",
        message: `${warn} هيتعدّل رصيد ${s.diff_lines || 0} سطر (زيادة ${num(s.surplus_qty || 0)} وعجز ${num(s.shortage_qty || 0)}) ويتسجل لكل سطر في سجل التعديلات. العملية مش بترجع تاني. تكمل؟`, ok: "تم اعتماد الجرد وتسوية الأرصدة" });
    },

    // ---------- Excel ----------
    async exportSheet() {
      try { await download(`/api/stocktakes/${this.id}/export`, `stocktake_${this.id}.xlsx`); } catch (e) { toast(errMsg(e), "error"); }
    },
    async importSheet(file) {
      if (!file) return;
      if (!(await this.flush())) return;
      this.importing = true; this.importErrors = null;
      try {
        const fd = new FormData(); fd.append("file", file);
        const r = await api.upload(`/api/stocktakes/${this.id}/import`, fd);
        this.importErrors = r.errors_total ? { rows: r.errors, total: r.errors_total, truncated: r.errors_truncated } : null;
        toast(`اتسجل العدّ لـ ${r.applied} سطر${r.errors_total ? ` (${r.errors_total} خطأ)` : ""}`, r.errors_total ? "error" : "success");
        this.load();
      } catch (e) { toast(errMsg(e), "error"); }
      this.importing = false;
    },

    // ---------- صنف لقيته ----------
    async addLine() {
      const a = this.add; a.error = "";
      if (!a.product || !a.location || !(Number(a.qty) >= 0) || a.qty === null || a.qty === "") { a.error = "اختار الصنف والمكان واكتب الكمية"; return; }
      try {
        await api.post(`/api/stocktakes/${this.id}/lines`, { product_id: a.product, location_id: Number(String(a.location).split(":")[1]), counted_qty: Number(a.qty) });
        this.add = { open: false, product: null, location: null, qty: null, error: "" }; toast("اتضاف الصنف"); this.load();
      } catch (e) { a.error = errMsg(e); }
    },
  }));
});

// settings.js — الحساب والمظهر وتغيير كلمة المرور وتصدير/استيراد Excel
import { api, download } from "./api.js";
import { getUser, can } from "./auth.js";
import { invalidateData } from "./data.js";
import { toast, errMsg, confirmAction } from "./utils.js";
import { API_BASE } from "./config.js";
import { applyTheme } from "./theme.js";

const currentTheme = () => localStorage.getItem("theme") || "auto";

document.addEventListener("alpine:init", () => {
  window.Alpine.data("settings", () => ({
    canCompany: can("settings.company"), canImport: can("products.import"), user: getUser(),
    canExport(x) { return can(x.perm); },
    theme: currentTheme(),
    themes: [{ key: "light", label: "نهاري", icon: "fa-sun" }, { key: "dark", label: "ليلي", icon: "fa-moon" }, { key: "auto", label: "تلقائي", icon: "fa-circle-half-stroke" }],
    pw: { current: "", next: "", confirm: "", saving: false, error: "" },
    // بيانات الشركة (للأدمن): الاسم + رقمين + اللوجو. بتظهر في المستندات المطبوعة
    publicUrl: { value: "", source: "none", saving: false, error: "" },
    company: { name: "", phone1: "", phone2: "", has_logo: false, logo_version: null, saving: false, error: "", logoBusy: false },
    busy: "", file: null, drag: false, importing: false, result: null,
    exports: [
      { path: "/api/excel/products/export", name: "products.xlsx", label: "المنتجات", sub: "كل المنتجات وكمياتها", icon: "fa-boxes-stacked", perm: "products.export" },
      { path: "/api/excel/products/template", name: "products-template.xlsx", label: "قالب الاستيراد", sub: "ملف فاضي بالأعمدة المطلوبة", icon: "fa-table", perm: "products.import" },
      { path: "/api/excel/movements/export", name: "movements.xlsx", label: "حركات المخزون", sub: "الوارد والصادر والنقل", icon: "fa-right-left", perm: "invoices.view" },
      { path: "/api/excel/quantity-adjustments/export", name: "adjustments.xlsx", label: "تعديلات الكميات", sub: "سجل التصحيحات اليدوية", icon: "fa-pen-to-square", perm: "reports.view" },
    ],

    get logoSrc() { return `${API_BASE}/api/company/logo?v=${encodeURIComponent(this.company.logo_version || "")}`; },
    applyCompany(r) {
      Object.assign(this.company, { name: r.name || "", phone1: r.phone1 || "", phone2: r.phone2 || "", has_logo: !!r.has_logo, logo_version: r.logo_version });
    },
    async loadCompany() {
      try { this.applyCompany(await api.get("/api/company")); } catch (e) { toast(errMsg(e), "error"); }
    },
    async saveCompany() {
      const c = this.company;
      c.error = ""; c.saving = true;
      try { this.applyCompany(await api.put("/api/company", { name: c.name, phone1: c.phone1, phone2: c.phone2 })); toast("تم حفظ بيانات الشركة"); }
      catch (e) { c.error = errMsg(e); }
      c.saving = false;
    },
    // اللوجو: PNG أو JPG بس وبحد أقصى 1 ميجا (WebP/GIF/SVG مرفوضين). الصور الأكبر بتتصغّر هنا في المتصفح قبل الرفع:
    // PNG (بشفافيته) أولًا، ولو لسه كبير JPEG على خلفية بيضاء، مع تقليل الأبعاد تدريجيًا.
    async shrinkLogo(f) {
      const SAFE = 0.95 * 1024 * 1024;
      if (f.size <= SAFE) return f;
      const bmp = await createImageBitmap(f);
      let scale = Math.min(1, 1600 / Math.max(bmp.width, bmp.height));
      const toBlob = (cv, type, q) => new Promise((r) => cv.toBlob(r, type, q));
      for (let i = 0; i < 8; i++) {
        const w = Math.max(1, Math.round(bmp.width * scale)), h = Math.max(1, Math.round(bmp.height * scale));
        const cv = document.createElement("canvas"); cv.width = w; cv.height = h;
        cv.getContext("2d").drawImage(bmp, 0, 0, w, h);
        const png = await toBlob(cv, "image/png");
        if (png && png.type === "image/png" && png.size <= SAFE) return new File([png], "logo.png", { type: "image/png" });
        const bg = document.createElement("canvas"); bg.width = w; bg.height = h;
        const g = bg.getContext("2d"); g.fillStyle = "#fff"; g.fillRect(0, 0, w, h); g.drawImage(bmp, 0, 0, w, h);
        const jpg = await toBlob(bg, "image/jpeg", 0.88);
        if (jpg && jpg.type === "image/jpeg" && jpg.size <= SAFE) return new File([jpg], "logo.jpg", { type: "image/jpeg" });
        scale *= 0.75;
      }
      return null;
    },
    async uploadLogo(f) {
      const c = this.company;
      if (!f) return;
      if (!["image/png", "image/jpeg"].includes(f.type)) { toast("اللوجو لازم يكون PNG أو JPG", "error"); return; }
      if (f.size > 20 * 1024 * 1024) { toast("الصورة كبيرة جدًا", "error"); return; }
      c.logoBusy = true;
      try {
        const file = await this.shrinkLogo(f);
        if (!file) { toast("مقدرتش أصغّر الصورة لحجم مناسب (1 ميجا) - جرّب صورة أصغر", "error"); c.logoBusy = false; return; }
        const fd = new FormData(); fd.append("file", file);
        this.applyCompany(await api.upload("/api/company/logo", fd)); toast("تم رفع اللوجو");
      } catch (e) { toast(errMsg(e), "error"); }
      c.logoBusy = false;
    },
    async loadPublicUrl() {
      try { const r = await api.get("/api/settings/public-url"); this.publicUrl.value = r.public_base_url || ""; this.publicUrl.source = r.source; } catch (e) { this.publicUrl.error = errMsg(e); }
    },
    async savePublicUrl() {
      const u = this.publicUrl; u.error = ""; u.saving = true;
      try {
        const r = await api.put("/api/settings/public-url", { public_base_url: (u.value || "").trim() });
        u.value = r.public_base_url || ""; u.source = r.source; toast("تم حفظ العنوان العام");
      } catch (e) { u.error = errMsg(e); }
      u.saving = false;
    },
    async removeLogo() {
      if (!(await confirmAction({ title: "مسح اللوجو", message: "اللوجو هيتشال من المستندات المطبوعة.", confirmText: "مسح", danger: true }))) return;
      this.company.logoBusy = true;
      try { this.applyCompany(await api.del("/api/company/logo")); toast("تم مسح اللوجو"); }
      catch (e) { toast(errMsg(e), "error"); }
      this.company.logoBusy = false;
    },

    init() {
      if (this.canCompany) { this.loadCompany(); this.loadPublicUrl(); }
      window.addEventListener("app-user", (e) => { this.user = e.detail; });
      window.addEventListener("app-theme", (e) => { if (this.theme !== "auto") this.theme = e.detail.dark ? "dark" : "light"; });
      if (location.hash === "#import") this.$nextTick(() => document.getElementById("import")?.scrollIntoView({ behavior: "smooth" }));
    },

    setTheme(t, origin) {
      this.theme = t;
      const dark = t === "dark" || (t === "auto" && matchMedia("(prefers-color-scheme: dark)").matches);
      try { t === "auto" ? localStorage.removeItem("theme") : localStorage.setItem("theme", t); } catch (e) { /* ignore */ }
      applyTheme(dark, origin);
    },

    async changePassword() {
      const p = this.pw;
      p.error = "";
      if (!p.current || !p.next) { p.error = "اكتب كلمة المرور الحالية والجديدة"; return; }
      if (p.next.length < 8) { p.error = "كلمة المرور الجديدة لازم تكون 8 حروف على الأقل"; return; }
      if (p.next !== p.confirm) { p.error = "تأكيد كلمة المرور مش مطابق"; return; }
      p.saving = true;
      try {
        await api.post("/api/auth/change-password", { current_password: p.current, new_password: p.next });
        toast("تم تغيير كلمة المرور");
        Object.assign(p, { current: "", next: "", confirm: "" });
      } catch (e) { p.error = errMsg(e); }
      p.saving = false;
    },

    async exportFile(x) {
      this.busy = x.path;
      try { await download(x.path, x.name); } catch (e) { toast(errMsg(e), "error"); }
      this.busy = "";
    },

    pick(f) {
      if (!f) return;
      if (!/\.xlsx$/i.test(f.name)) { toast("لازم الملف يكون Excel (.xlsx)", "error"); return; }
      if (f.size > 10 * 1024 * 1024) { toast("الملف أكبر من 10 ميجا", "error"); return; }
      this.file = f; this.result = null;
    },

    async importFile() {
      if (!this.file) return;
      this.importing = true; this.result = null;
      const fd = new FormData();
      fd.append("file", this.file);
      try {
        const r = await api.upload("/api/excel/products/import", fd);
        this.result = { created: r.created || 0, updated: r.updated || 0, errors: r.errors || [], errors_total: r.errors_total ?? (r.errors || []).length, truncated: !!r.errors_truncated };
        toast(r.message || "تم الاستيراد", this.result.errors_total ? "error" : "success");
        this.file = null; invalidateData();
      } catch (e) { toast(errMsg(e), "error"); }
      this.importing = false;
    },

    errText(e) { return typeof e === "string" ? e : e.message || e.detail || JSON.stringify(e); },
  }));
});

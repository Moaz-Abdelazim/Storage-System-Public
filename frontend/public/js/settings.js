// settings.js — الحساب والمظهر وتغيير كلمة المرور وتصدير/استيراد Excel
import { api, download } from "./api.js";
import { getUser, isAdmin } from "./auth.js";
import { invalidateData } from "./data.js";
import { toast, errMsg, confirmAction } from "./utils.js";
import { API_BASE } from "./config.js";
import { applyTheme } from "./theme.js";

const currentTheme = () => localStorage.getItem("theme") || "auto";

document.addEventListener("alpine:init", () => {
  window.Alpine.data("settings", () => ({
    admin: isAdmin(), user: getUser(),
    theme: currentTheme(),
    themes: [{ key: "light", label: "نهاري", icon: "fa-sun" }, { key: "dark", label: "ليلي", icon: "fa-moon" }, { key: "auto", label: "تلقائي", icon: "fa-circle-half-stroke" }],
    pw: { current: "", next: "", confirm: "", saving: false, error: "" },
    // بيانات الشركة (للأدمن): الاسم + رقمين + اللوجو. بتظهر في الفواتير المطبوعة
    company: { name: "", phone1: "", phone2: "", has_logo: false, logo_version: null, saving: false, error: "", logoBusy: false },
    busy: "", file: null, drag: false, importing: false, result: null,
    exports: [
      { path: "/api/excel/products/export", name: "products.xlsx", label: "المنتجات", sub: "كل المنتجات وكمياتها", icon: "fa-boxes-stacked" },
      { path: "/api/excel/products/template", name: "products-template.xlsx", label: "قالب الاستيراد", sub: "ملف فاضي بالأعمدة المطلوبة", icon: "fa-table" },
      { path: "/api/excel/movements/export", name: "movements.xlsx", label: "حركات المخزون", sub: "الوارد والصادر والنقل", icon: "fa-right-left" },
      { path: "/api/excel/quantity-adjustments/export", name: "adjustments.xlsx", label: "تعديلات الكميات", sub: "سجل التصحيحات اليدوية", icon: "fa-pen-to-square", admin: true },
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
    // الحد الأقصى للوجو 5 ميجا. الاستضافة على Vercel بترفض أي طلب أكبر من ~4.5 ميجا، فالصور الأكبر من 3.5 ميجا
    // بتتصغّر هنا في المتصفح (نفس الشكل، أبعاد أقل) قبل الرفع. على VPS الحد ده مش موجود بس التصغير مش بيضر.
    async shrinkLogo(f) {
      const SAFE = 3.5 * 1024 * 1024;
      if (f.size <= SAFE) return f;
      const bmp = await createImageBitmap(f);
      let scale = Math.min(1, 2400 / Math.max(bmp.width, bmp.height));
      for (let i = 0; i < 6; i++) {
        const cv = document.createElement("canvas");
        cv.width = Math.max(1, Math.round(bmp.width * scale)); cv.height = Math.max(1, Math.round(bmp.height * scale));
        cv.getContext("2d").drawImage(bmp, 0, 0, cv.width, cv.height);
        const blob = await new Promise((r) => cv.toBlob(r, "image/webp", 0.9));
        if (blob && ["image/webp", "image/png"].includes(blob.type) && blob.size <= SAFE) return new File([blob], blob.type === "image/webp" ? "logo.webp" : "logo.png", { type: blob.type });
        scale *= 0.75;
      }
      return null;
    },
    async uploadLogo(f) {
      const c = this.company;
      if (!f) return;
      if (!["image/png", "image/jpeg", "image/webp"].includes(f.type)) { toast("اللوجو لازم يكون PNG أو JPEG أو WebP", "error"); return; }
      if (f.size > 5 * 1024 * 1024) { toast("حجم اللوجو أكبر من 5 ميجا", "error"); return; }
      c.logoBusy = true;
      try {
        const file = await this.shrinkLogo(f);
        if (!file) { toast("مقدرتش أصغّر الصورة لحجم مناسب - جرّب صورة أصغر", "error"); c.logoBusy = false; return; }
        const fd = new FormData(); fd.append("file", file);
        this.applyCompany(await api.upload("/api/company/logo", fd)); toast("تم رفع اللوجو");
      } catch (e) { toast(errMsg(e), "error"); }
      c.logoBusy = false;
    },
    async removeLogo() {
      if (!(await confirmAction({ title: "مسح اللوجو", message: "اللوجو هيتشال من الفواتير المطبوعة.", confirmText: "مسح", danger: true }))) return;
      this.company.logoBusy = true;
      try { this.applyCompany(await api.del("/api/company/logo")); toast("تم مسح اللوجو"); }
      catch (e) { toast(errMsg(e), "error"); }
      this.company.logoBusy = false;
    },

    init() {
      if (this.admin) this.loadCompany();
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
      if (!/\.xlsx?$/i.test(f.name)) { toast("لازم الملف يكون Excel (.xlsx)", "error"); return; }
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
        this.result = { created: r.created || 0, updated: r.updated || 0, errors: r.errors || [] };
        toast(r.message || "تم الاستيراد", this.result.errors.length ? "error" : "success");
        this.file = null; invalidateData();
      } catch (e) { toast(errMsg(e), "error"); }
      this.importing = false;
    },

    errText(e) { return typeof e === "string" ? e : e.message || e.detail || JSON.stringify(e); },
  }));
});

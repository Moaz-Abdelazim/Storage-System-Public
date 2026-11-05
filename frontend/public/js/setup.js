// الإعداد الأول (من غير الـ shell): بيشتغل بس لو مفيش أي مستخدم. بعد النجاح بيدخّل الأدمن الجديد ويروح للرئيسية.
import { api, ApiError } from "./api.js";
import { storeUser } from "./auth.js";
import { errMsg } from "./utils.js";
import { toggleTheme } from "./theme.js";

document.addEventListener("alpine:init", () => {
  window.Alpine.data("setup", () => ({
    checking: true, loading: false, error: "", show: false,
    f: { username: "", full_name: "", password: "", password2: "", company_name: "", warehouse_name: "", warehouse_code: "", create_receiving_location: true },

    async init() {
      try {
        const st = await api.get("/api/setup/status");
        if (st && !st.needs_setup) { location.replace("login.html"); return; }  // الإعداد اتعمل قبل كده
      } catch (e) { this.error = errMsg(e); }
      this.checking = false;
    },
    toggleTheme,

    validate() {
      const f = this.f;
      if (!/^[A-Za-z0-9_.-]{3,50}$/.test(f.username)) return "اسم المستخدم 3 إلى 50 حرف إنجليزي أو أرقام أو _ . -";
      if (f.password.length < 8) return "كلمة المرور 8 حروف على الأقل";
      if (new TextEncoder().encode(f.password).length > 72) return "كلمة المرور طويلة جدًا (أقصى حاجة 72 بايت)";
      if (f.password !== f.password2) return "تأكيد كلمة المرور مش مطابق";
      if (!f.warehouse_name.trim() || !f.warehouse_code.trim()) return "اكتب اسم وكود أول مخزن";
      return "";
    },

    async submit() {
      this.error = this.validate();
      if (this.error) return;
      this.loading = true;
      const f = this.f;
      try {
        await api.post("/api/setup", {
          username: f.username.trim(), password: f.password, full_name: f.full_name.trim() || null, company_name: f.company_name.trim() || null,
          warehouse_name: f.warehouse_name.trim(), warehouse_code: f.warehouse_code.trim(), create_receiving_location: f.create_receiving_location,
        });
        const r = await api.post("/api/auth/login", { username: f.username.trim(), password: f.password });
        storeUser(r);
        location.replace("index.html");
      } catch (e) {
        this.error = e instanceof ApiError && e.status === 409 ? "الإعداد الأول اتعمل بالفعل - سجّل الدخول." : errMsg(e);
      }
      this.loading = false;
    },
  }));
});

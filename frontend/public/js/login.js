// صفحة تسجيل الدخول (من غير الـ shell)
import { api, ApiError } from "./api.js";
import { storeUser } from "./auth.js";
import { getQuery, safeNext } from "./utils.js";
import { toggleTheme } from "./theme.js";

// بنسمح بالرجوع لصفحة داخلية بسيطة بس (مفيش open redirect) - المنطق في utils.safeNext ومتغطى باختبار node --test
const nextPage = () => safeNext(getQuery("next"));

document.addEventListener("alpine:init", () => {
  window.Alpine.data("login", () => ({
    username: "", password: "", show: false, loading: false, error: "",

    async init() {
      // أول تشغيل: مفيش مستخدمين = صفحة الإعداد الأول
      try {
        const st = await api.get("/api/setup/status");
        if (st && st.needs_setup) { location.replace("setup.html"); return; }
      } catch (e) { /* نكمل عادي */ }
      // لو فيه جلسة شغالة نروح للصفحة الرئيسية على طول
      try {
        const me = await api.get("/api/auth/me");
        if (me && me.role_active !== false) { this.store(me); location.replace(nextPage()); }  // دور موقوف: نفضل في صفحة الدخول بدل ما ندور في لفة
      } catch (e) { /* مفيش جلسة */ }
    },

    store(u) { storeUser(u); },

    async submit() {
      this.error = "";
      if (!this.username || !this.password) { this.error = "اكتب اسم المستخدم وكلمة المرور"; return; }
      this.loading = true;
      try {
        const data = await api.post("/api/auth/login", { username: this.username, password: this.password });
        localStorage.removeItem("token");
        this.store(data);
        location.replace(nextPage());
      } catch (e) {
        this.error = e instanceof ApiError ? e.message : "حصل خطأ غير متوقع";
        this.password = "";
        this.loading = false;
      }
    },

    toggleTheme(origin) { toggleTheme(origin); },
  }));
});

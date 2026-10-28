// users.js — إدارة المستخدمين (أدمن)
import { api, qs } from "./api.js";
import { getUser } from "./auth.js";
import { invalidateData } from "./data.js";
import { fmtDate, toast, errMsg, confirmAction } from "./utils.js";

document.addEventListener("alpine:init", () => {
  window.Alpine.data("users", () => ({
    fmtDate, me: getUser().username,
    rows: [], stats: null, loading: true, error: "", page: 1, pages: 1, total: 0, size: 10,
    f: { search: "", role: "", status: "" },
    form: { open: false, id: null, username: "", full_name: "", password: "", role: "staff", saving: false, error: "" },
    cards: [
      { key: "all", label: "إجمالي المستخدمين", icon: "fa-user-group", tone: "bg-brand-500/10 text-brand-600", role: "", status: "", value: (s) => s.total },
      { key: "admin", label: "مديرو النظام", icon: "fa-user-shield", tone: "bg-violet-500/10 text-violet-600", role: "admin", status: "", value: (s) => s.admins },
      { key: "staff", label: "موظفو المخازن", icon: "fa-user", tone: "bg-blue-500/10 text-blue-600", role: "staff", status: "", value: (s) => s.staff },
      { key: "inactive", label: "حسابات موقوفة", icon: "fa-user-slash", tone: "bg-red-500/10 text-red-600", role: "", status: "inactive", value: (s) => s.inactive },
    ],

    init() { this.load(); this.loadStats(); },

    isActive(c) { return this.f.role === c.role && this.f.status === c.status; },
    setFilter(c) { this.f.role = c.role; this.f.status = c.status; this.apply(); },
    clearFilter() { this.f.role = ""; this.f.status = ""; this.apply(); },
    get filterLabel() {
      return this.f.role === "admin" ? "المديرين فقط" : this.f.role === "staff" ? "الموظفين فقط" : this.f.status === "inactive" ? "الموقوفين فقط" : "";
    },
    apply() { this.page = 1; this.load(); },

    async loadStats() { try { this.stats = await api.get("/api/auth/users/stats"); } catch (e) { /* مش حرج */ } },

    async load() {
      this.loading = true; this.error = "";
      const { search, role, status } = this.f;
      try {
        const r = await api.get(`/api/auth/users${qs({ page: this.page, page_size: this.size, search: search.trim(), role, status })}`);
        this.rows = r.items; this.total = r.total; this.pages = r.total_pages; this.page = r.page;
      } catch (e) { this.error = errMsg(e); this.rows = []; }
      this.loading = false;
    },

    openForm(u) {
      this.form = { open: true, id: u ? u.id : null, username: u ? u.username : "", full_name: u ? u.full_name || "" : "", password: "", role: u ? u.role : "staff", saving: false, error: "" };
    },

    async save() {
      const f = this.form;
      if (!f.username) { f.error = "اكتب اسم المستخدم"; return; }
      if (!f.id && f.password.length < 6) { f.error = "كلمة المرور لازم تكون 6 حروف على الأقل"; return; }
      if (f.id && f.password && f.password.length < 6) { f.error = "كلمة المرور لازم تكون 6 حروف على الأقل"; return; }
      f.saving = true; f.error = "";
      try {
        const body = { username: f.username, full_name: f.full_name || null, role: f.role };
        if (f.password) body.password = f.password;
        if (f.id) await api.put(`/api/auth/users/${f.id}`, body); else await api.post("/api/auth/users", body);
        toast(f.id ? "تم تعديل المستخدم" : "تمت إضافة المستخدم");
        f.open = false; invalidateData(); this.load(); this.loadStats();
      } catch (e) { f.error = errMsg(e); }
      f.saving = false;
    },

    async toggleActive(u) {
      const stop = u.is_active;
      const ok = await confirmAction({
        title: stop ? "إيقاف الحساب؟" : "تفعيل الحساب؟", danger: stop, confirmText: stop ? "إيقاف" : "تفعيل",
        message: stop ? `"${u.username}" مش هيقدر يدخل، وأي جلسة مفتوحة ليه هتتقفل فورًا.` : `"${u.username}" هيقدر يدخل تاني.`,
      });
      if (!ok) return;
      try { await api.put(`/api/auth/users/${u.id}`, { is_active: !stop }); toast(stop ? "تم الإيقاف" : "تم التفعيل"); this.load(); this.loadStats(); }
      catch (e) { toast(errMsg(e), "error"); }
    },

    async remove(u) {
      const ok = await confirmAction({
        title: "حذف المستخدم؟", danger: true, confirmText: "حذف",
        message: `"${u.username}" هيتحذف نهائيًا. فواتيره وسجلاته بتفضل محفوظة باسمه.`,
      });
      if (!ok) return;
      try { await api.del(`/api/auth/users/${u.id}`); toast("تم الحذف"); invalidateData(); this.load(); this.loadStats(); }
      catch (e) { toast(errMsg(e), "error"); }
    },
  }));
});

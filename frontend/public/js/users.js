// users.js — إدارة المستخدمين (users.manage). الدور بيتختار من الأدوار الفعّالة (roles-lite)
import { api, qs } from "./api.js";
import { getUser, isAdmin } from "./auth.js";
import { invalidateData } from "./data.js";
import { fmtDate, toast, errMsg, confirmAction } from "./utils.js";

document.addEventListener("alpine:init", () => {
  window.Alpine.data("users", () => ({
    fmtDate, me: getUser().username, iAmAdmin: isAdmin(), roles: [],
    rows: [], stats: null, loading: true, error: "", page: 1, pages: 1, total: 0, size: 10,
    f: { search: "", role_id: "", status: "" },
    form: { open: false, id: null, username: "", full_name: "", password: "", role_id: "", saving: false, error: "" },
    get cards() {
      const sys = this.roles.find((r) => r.is_system);
      return [
        { key: "all", label: "إجمالي المستخدمين", icon: "fa-user-group", tone: "bg-brand-500/10 text-brand-600", role_id: "", status: "", value: (s) => s.total },
        { key: "admin", label: "مديرو النظام", icon: "fa-user-shield", tone: "bg-violet-500/10 text-violet-600", role_id: sys ? String(sys.id) : "", status: "", value: (s) => s.admins },
        { key: "other", label: "مستخدمو الأدوار الأخرى", icon: "fa-user", tone: "bg-blue-500/10 text-blue-600", role_id: "", status: "", value: (s) => s.total - s.admins, noFilter: true },
        { key: "inactive", label: "حسابات موقوفة", icon: "fa-user-slash", tone: "bg-red-500/10 text-red-600", role_id: "", status: "inactive", value: (s) => s.inactive },
      ];
    },

    init() { this.loadRoles(); this.load(); this.loadStats(); },

    async loadRoles() { try { this.roles = await api.get("/api/auth/roles-lite"); } catch (e) { /* مش حرج */ } },
    canTouch(u) { return !u.is_admin || this.iAmAdmin; },

    isActive(c) { return !c.noFilter && this.f.role_id === c.role_id && this.f.status === c.status; },
    setFilter(c) { if (c.noFilter) return; this.f.role_id = c.role_id; this.f.status = c.status; this.apply(); },
    clearFilter() { this.f.role_id = ""; this.f.status = ""; this.apply(); },
    get filterLabel() {
      if (this.f.status === "inactive") return "الموقوفين فقط";
      const r = this.roles.find((x) => String(x.id) === String(this.f.role_id));
      return r ? `دور: ${r.name}` : "";
    },
    apply() { this.page = 1; this.load(); },

    async loadStats() { try { this.stats = await api.get("/api/auth/users/stats"); } catch (e) { /* مش حرج */ } },

    async load() {
      this.loading = true; this.error = "";
      const { search, role_id, status } = this.f;
      try {
        const r = await api.get(`/api/auth/users${qs({ page: this.page, page_size: this.size, search: search.trim(), role_id, status })}`);
        this.rows = r.items; this.total = r.total; this.pages = r.total_pages; this.page = r.page;
      } catch (e) { this.error = errMsg(e); this.rows = []; }
      this.loading = false;
    },

    openForm(u) {
      this.form = { open: true, id: u ? u.id : null, username: u ? u.username : "", full_name: u ? u.full_name || "" : "", password: "", role_id: u ? u.role_id : (this.roles.find((r) => !r.is_system) || this.roles[0] || {}).id || "", saving: false, error: "" };
    },

    async save() {
      const f = this.form;
      if (!f.username) { f.error = "اكتب اسم المستخدم"; return; }
      if (!f.role_id) { f.error = "اختار الدور"; return; }
      if (!f.id && f.password.length < 8) { f.error = "كلمة المرور لازم تكون 8 حروف على الأقل"; return; }
      if (f.id && f.password && f.password.length < 8) { f.error = "كلمة المرور لازم تكون 8 حروف على الأقل"; return; }
      f.saving = true; f.error = "";
      try {
        const body = { username: f.username, full_name: f.full_name || null, role_id: Number(f.role_id) };
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

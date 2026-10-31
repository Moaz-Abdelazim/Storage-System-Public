// login-logs.js — سجل جلسات الدخول (أدمن) مع إنهاء جلسة بالقوة
import { can } from "./auth.js";
import { api, qs } from "./api.js";
import { fmtDateTime, toast, errMsg, confirmAction } from "./utils.js";

function duration(l) {
  const end = l.logout_time ? new Date(l.logout_time) : new Date();
  const mins = Math.max(0, Math.round((end - new Date(l.login_time)) / 60000));
  if (mins < 60) return `${mins} د`;
  const h = Math.floor(mins / 60);
  return h < 24 ? `${h} س ${mins % 60} د` : `${Math.floor(h / 24)} يوم ${h % 24} س`;
}

document.addEventListener("alpine:init", () => {
  window.Alpine.data("loginLogs", () => ({
    canRevoke: can("sessions.revoke"),
    fmtDateTime, duration,
    rows: [], loading: true, error: "", page: 1, pages: 1, total: 0, size: 15,
    f: { user: "", ip: "", status: "", date_from: "", date_to: "" },
    statuses: [{ key: "", label: "الكل" }, { key: "active", label: "مفتوحة" }, { key: "closed", label: "مقفولة" }],

    init() { this.load(); },
    apply() { this.page = 1; this.load(); },

    async load() {
      this.loading = true; this.error = "";
      const { user, ip, status, date_from, date_to } = this.f;
      try {
        const r = await api.get(`/api/auth/login-logs${qs({ page: this.page, page_size: this.size, user: user.trim(), ip: ip.trim(), status, date_from, date_to })}`);
        this.rows = r.items; this.total = r.total; this.pages = r.total_pages; this.page = r.page;
      } catch (e) { this.error = errMsg(e); this.rows = []; }
      this.loading = false;
    },

    async revoke(l) {
      const ok = await confirmAction({ title: "إنهاء الجلسة؟", danger: true, confirmText: "إنهاء", message: `جلسة "${l.username}" هتتقفل فورًا وأي طلب جديد بيها هيترفض.` });
      if (!ok) return;
      try { await api.post(`/api/auth/login-logs/${l.id}/revoke`); toast("تم إنهاء الجلسة"); this.load(); }
      catch (e) { toast(errMsg(e), "error"); }
    },
  }));
});

// login-logs.js — سجل جلسات الدخول (أدمن) مع إنهاء جلسة بالقوة
import { can } from "./auth.js";
import { api, qs } from "./api.js";
import { fmtDateTime, toast, errMsg, confirmAction } from "./utils.js";

// حالة الجلسة بتيجي من السيرفر (محسوبة من expires_at و end_reason): مفتوحة / انتهت صلاحيتها / سجّل خروج / أنهاها أدمن / أُغلقت من النظام
const STATUS = {
  active: { label: "مفتوحة", cls: "badge-ok" },
  expired: { label: "انتهت صلاحيتها", cls: "badge-warn" },
  logged_out: { label: "سجّل خروج", cls: "badge-mute" },
  revoked: { label: "أُنهيت إداريًا", cls: "badge-bad" },
  system: { label: "أُغلقت تلقائيًا", cls: "badge-mute" },
};
const statusOf = (l) => STATUS[l.status] || STATUS.logged_out;
const statusLabel = (l) => (l.status === "revoked" && l.closed_by ? `أنهاها ${l.closed_by}` : statusOf(l).label);

function duration(l) {
  const end = l.ended_at ? new Date(l.ended_at) : new Date();
  const mins = Math.max(0, Math.round((end - new Date(l.login_time)) / 60000));
  if (mins < 60) return `${mins} د`;
  const h = Math.floor(mins / 60);
  return h < 24 ? `${h} س ${mins % 60} د` : `${Math.floor(h / 24)} يوم ${h % 24} س`;
}

document.addEventListener("alpine:init", () => {
  window.Alpine.data("loginLogs", () => ({
    canRevoke: can("sessions.revoke"),
    fmtDateTime, duration, statusOf, statusLabel,
    rows: [], loading: true, error: "", page: 1, pages: 1, total: 0, size: 15,
    f: { user: "", ip: "", status: "", date_from: "", date_to: "" },
    statuses: [{ key: "", label: "الكل" }, { key: "active", label: "مفتوحة" }, { key: "expired", label: "منتهية" }, { key: "logged_out", label: "خروج" }, { key: "revoked", label: "إنهاء إداري" }],

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

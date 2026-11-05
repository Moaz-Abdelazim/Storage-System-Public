// =====================================================================
//  utils.js — دوال مشتركة (تنسيق أرقام/تواريخ، debounce، toast)
//  الأرقام دايمًا لاتيني (1,234) والتواريخ بالعربي بأرقام لاتينية.
// =====================================================================
const LOCALE = "ar-EG-u-nu-latn";

export function num(n, max = 3) {
  const v = Number(n);
  if (!Number.isFinite(v)) return "0";
  return v.toLocaleString("en-US", { maximumFractionDigits: max });
}

export function money(n) {
  if (n == null) return "—";  // null = المستخدم ملوش صلاحية finance.view (أو القيمة مش موجودة)
  const v = Number(n);
  return `${(Number.isFinite(v) ? v : 0).toLocaleString("en-US", { maximumFractionDigits: 2 })} ج.م`;
}

// أرقام كبيرة بشكل مختصر (للكروت): 1.2K / 3.4M
export function compact(n) {
  const v = Number(n) || 0;
  return new Intl.NumberFormat("en-US", { notation: "compact", maximumFractionDigits: 1 }).format(v);
}

export function fmtDate(d, opts = { day: "numeric", month: "short", year: "numeric" }) {
  if (!d) return "-";
  const dt = new Date(d);
  return isNaN(dt) ? "-" : dt.toLocaleDateString(LOCALE, opts);
}

export function fmtDateTime(d) {
  if (!d) return "-";
  const dt = new Date(d);
  return isNaN(dt) ? "-" : dt.toLocaleString(LOCALE, { day: "numeric", month: "short", hour: "numeric", minute: "2-digit" });
}

export function longToday() {
  return new Date().toLocaleDateString(LOCALE, { weekday: "long", day: "numeric", month: "long", year: "numeric" });
}

// "YYYY-MM-DD" بالتوقيت المحلي (مش UTC) — مهم عشان فلاتر التاريخ
export function isoDay(d = new Date()) {
  const y = d.getFullYear();
  const m = String(d.getMonth() + 1).padStart(2, "0");
  const day = String(d.getDate()).padStart(2, "0");
  return `${y}-${m}-${day}`;
}

export function lastNDays(n) {
  const out = [];
  for (let i = n - 1; i >= 0; i--) {
    const d = new Date();
    d.setHours(12, 0, 0, 0);
    d.setDate(d.getDate() - i);
    out.push(isoDay(d));
  }
  return out;
}

export function debounce(fn, ms = 300) {
  let t;
  const wrapped = (...args) => { clearTimeout(t); t = setTimeout(() => fn(...args), ms); };
  wrapped.cancel = () => clearTimeout(t);
  return wrapped;
}

// Toast: بيتعرض من الـ shell (layout.js)
export function toast(message, type = "success") {
  window.dispatchEvent(new CustomEvent("app-toast", { detail: { message, type } }));
}

export function errMsg(e, fallback = "حصل خطأ") {
  return (e && e.message) || fallback;
}

// ---------- أدوات إضافية ----------
export const KIND = {
  in: { label: "وارد", icon: "fa-arrow-down", tile: "bg-brand-500/10 text-brand-600 dark:text-brand-300", badge: "badge-ok" },
  out: { label: "صادر", icon: "fa-arrow-up", tile: "bg-orange-500/10 text-orange-600 dark:text-orange-300", badge: "bg-orange-500/10 text-orange-700 dark:text-orange-300" },
  transfer: { label: "نقل", icon: "fa-right-left", tile: "bg-blue-500/10 text-blue-600 dark:text-blue-300", badge: "badge-info" },
};

// الـ CSP (style-src 'self') بيمنع سمة style="..." في أي HTML بيتحط بـ innerHTML. القوالب بتكتب data-s="..." وبعد الإدراج بننقلها لـ element.style
// (تعديل الـ CSSOM مسموح بالـ CSP). مفيش أي 'unsafe-inline'.
export function applyDataStyles(root = document) {
  root.querySelectorAll("[data-s]").forEach((el) => { el.style.cssText = el.dataset.s; el.removeAttribute("data-s"); });
}

// اختصار نص طويل بعلامة حذف (بيعدّ الحروف الحقيقية مش الـ UTF-16 units، فالعربي والإيموجي مبيتقطعوش من النص)
export function ellipsize(text, max = 16) {
  const chars = [...String(text ?? "")];
  if (chars.length <= max) return chars.join("");
  return max <= 1 ? "…" : chars.slice(0, max - 1).join("").trimEnd() + "…";
}

// لف نص طويل على أسطر (للـ tooltip في Chart.js اللي مبيلفّش لوحده). الكلمة الأطول من العرض بتتقطع.
export function wrapLines(text, width = 28) {
  const out = [];
  let line = "";
  for (const word of String(text ?? "").split(/\s+/).filter(Boolean)) {
    let w = [...word];
    while (w.length > width) { if (line) { out.push(line); line = ""; } out.push(w.slice(0, width).join("")); w = w.slice(width); }
    const piece = w.join("");
    if (!line) line = piece;
    else if ([...line].length + 1 + w.length <= width) line += " " + piece;
    else { out.push(line); line = piece; }
  }
  if (line) out.push(line);
  return out.length ? out : [""];
}

// حالات المستندات (إذن صرف: quote/reserved/completed/voided، إذن إضافة: completed/voided) - الاسم المعروض + لون الشارة
export const DOC_STATUS = {
  quote: { label: "عرض سعر", cls: "badge-info" },
  reserved: { label: "محجوزة", cls: "badge-warn" },
  completed: { label: "تمت", cls: "badge-ok" },
  voided: { label: "ملغاة", cls: "badge-bad" },
};
export const docStatus = (s) => DOC_STATUS[s] || DOC_STATUS.completed;

// UUID للـ idempotency: crypto.randomUUID موجودة بس في HTTPS/localhost، فبنرجع لـ getRandomValues على HTTP العادي
export function newIdempotencyKey() {
  const c = globalThis.crypto;
  if (c && typeof c.randomUUID === "function") return c.randomUUID();
  const b = new Uint8Array(16);
  if (c && c.getRandomValues) c.getRandomValues(b); else for (let i = 0; i < 16; i++) b[i] = Math.floor(Math.random() * 256);
  b[6] = (b[6] & 0x0f) | 0x40; b[8] = (b[8] & 0x3f) | 0x80;
  const h = [...b].map((x) => x.toString(16).padStart(2, "0")).join("");
  return `${h.slice(0, 8)}-${h.slice(8, 12)}-${h.slice(12, 16)}-${h.slice(16, 20)}-${h.slice(20)}`;
}

// الرجوع بعد تسجيل الدخول: صفحة .html داخلية بس (اسم ملف + query/hash بسيطين). أي حاجة تانية (//evil.com، javascript:، \\، مسارات، ..) = الافتراضي.
export function safeNext(raw, fallback = "index.html") {
  const n = typeof raw === "string" ? raw.trim() : "";
  if (!n || n.length > 300) return fallback;
  return /^[a-z0-9][a-z0-9-]*\.html(\?[A-Za-z0-9_=&%.-]*)?(#[A-Za-z0-9_=&%.:-]*)?$/i.test(n) ? n : fallback;
}

// حالة المنتج من الكمية والحد الأدنى
export function productStatus(p) {
  if (p.is_active === false) return { key: "inactive", label: "معطّل", cls: "badge-mute" };
  const q = Number(p.quantity) || 0;
  if (q <= 0) return { key: "out", label: "نفد", cls: "badge-bad" };
  if (q <= (Number(p.min_quantity) || 0)) return { key: "low", label: "منخفض", cls: "badge-warn" };
  return { key: "ok", label: "متوفر", cls: "badge-ok" };
}

// الباراميترات بتتقرا من الـ hash (#a=1&b=2) الأول، وبعدين من الـ query string (?a=1).
// الـ hash بيفضل موجود حتى لو السيرفر عمل redirect (clean URLs مثلاً) وقصّ الـ query string.
function paramsFromLocation() {
  const h = location.hash.slice(1);
  return new URLSearchParams(h.includes("=") ? h : location.search);
}

export function getQuery(name) {
  return paramsFromLocation().get(name) || "";
}

// بيحدّث الـ URL (في الـ hash) من غير reload عشان الفلاتر تتحفظ في الرابط
export function setQuery(params) {
  const p = paramsFromLocation();
  Object.entries(params).forEach(([k, v]) => (v === "" || v == null ? p.delete(k) : p.set(k, v)));
  const q = p.toString();
  history.replaceState(null, "", location.pathname + (q ? `#${q}` : ""));
}

// تصدير CSV (UTF-8 مع BOM عشان Excel يقرا العربي صح)
export function downloadCsv(filename, columns, rows) {
  const esc = (v) => {
    let s = v == null ? "" : String(v);
    if (/^[=+@\t\r]/.test(s)) s = "'" + s; // حماية من CSV injection
    return /[",\n]/.test(s) ? `"${s.replace(/"/g, '""')}"` : s;
  };
  const lines = [columns.map((c) => esc(c.label)).join(",")];
  rows.forEach((r) => lines.push(columns.map((c) => esc(typeof c.value === "function" ? c.value(r) : r[c.key])).join(",")));
  const blob = new Blob(["\ufeff" + lines.join("\r\n")], { type: "text/csv;charset=utf-8" });
  const url = URL.createObjectURL(blob);
  const a = Object.assign(document.createElement("a"), { href: url, download: filename });
  document.body.appendChild(a); a.click(); a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

// نافذة تأكيد (بتترسم من الـ shell): بترجّع Promise<boolean>
export function confirmAction({ title = "تأكيد", message = "", confirmText = "تأكيد", danger = false } = {}) {
  return new Promise((resolve) => {
    window.dispatchEvent(new CustomEvent("app-confirm", { detail: { title, message, confirmText, danger, resolve } }));
  });
}

export function pct(n, digits = 1) {
  const v = Number(n);
  return Number.isFinite(v) ? `${v.toLocaleString("en-US", { maximumFractionDigits: digits })}%` : "—";
}

export function todayISO() { return isoDay(new Date()); }
export function daysAgoISO(n) { const d = new Date(); d.setDate(d.getDate() - n); return isoDay(d); }

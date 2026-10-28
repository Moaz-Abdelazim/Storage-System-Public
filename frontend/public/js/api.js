// =====================================================================
//  api.js — كل التعامل مع الـ API من هنا بس.
//  - الجلسة HttpOnly cookie (credentials: include) + هيدر CSRF.
//  - 401 => مسح تلميحات الدخول والرجوع لصفحة login.
//  - GET بيدعم cache قصير + دمج الطلبات المتكررة (dedupe) لتسريع الواجهة.
// =====================================================================
import { API_BASE } from "./config.js";

export class ApiError extends Error {
  constructor(status, message, data = null) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.data = data;
  }
}

const AUTH_KEYS = ["username", "role", "full_name", "token"];

export function clearAuthHints() {
  AUTH_KEYS.forEach((k) => localStorage.removeItem(k));
}

// الصفحة الحالية كـ "next" عشان بعد الدخول نرجع لها (اسم ملف .html داخلي + hash بس - login.js بيرفض أي حاجة تانية)
export function loginUrl() {
  let page = (location.pathname.split("/").pop() || "index").replace(/\.html$/, "");
  page = `${page}.html`;
  if (/^login\./.test(page)) return "login.html";
  return `login.html#next=${encodeURIComponent(page + location.hash)}`;
}

function onUnauthorized() {
  clearAuthHints();
  if (!location.pathname.endsWith("login") && !location.pathname.endsWith("login.html")) location.replace(loginUrl());
}

// FastAPI بيرجّع detail نص أو لستة أخطاء validation
function parseDetail(data) {
  if (!data) return "";
  const d = data.detail ?? data.message;
  if (typeof d === "string") return d;
  if (Array.isArray(d)) return d.map((e) => e.msg || e.message || "").filter(Boolean).join("، ");
  return "";
}

export function qs(params = {}) {
  const p = new URLSearchParams();
  Object.entries(params).forEach(([k, v]) => {
    if (v !== undefined && v !== null && v !== "") p.append(k, v);
  });
  const s = p.toString();
  return s ? `?${s}` : "";
}

export async function request(path, { method = "GET", body, headers = {}, signal, raw = false } = {}) {
  const h = { "X-CSRF-Protection": "1", ...headers };
  let payload = body;
  if (body !== undefined && !(body instanceof FormData)) {
    h["Content-Type"] = "application/json";
    payload = JSON.stringify(body);
  }

  let res;
  try {
    res = await fetch(`${API_BASE}${path}`, { method, headers: h, body: payload, credentials: "include", signal });
  } catch (e) {
    if (e.name === "AbortError") throw e;
    throw new ApiError(0, "معرفش أوصل للسيرفر. اتأكد من الاتصال وإن السيرفر شغال.");
  }

  if (res.status === 401 && !path.startsWith("/api/auth/login")) {
    onUnauthorized();
    throw new ApiError(401, "انتهت الجلسة، سجّل الدخول تاني.");
  }
  if (raw) return res;

  let data = null;
  if ((res.headers.get("content-type") || "").includes("json")) {
    try { data = await res.json(); } catch (e) { /* no body */ }
  }
  if (!res.ok) throw new ApiError(res.status, parseDetail(data) || "حصل خطأ غير متوقع", data);
  return data;
}

// ---- GET مع cache قصير + dedupe ----
const cache = new Map();     // path -> { t, data }
const inflight = new Map();  // path -> Promise

function get(path, { ttl = 0, signal } = {}) {
  const hit = cache.get(path);
  if (ttl && hit && Date.now() - hit.t < ttl) return Promise.resolve(hit.data);
  if (!signal && inflight.has(path)) return inflight.get(path);
  const p = request(path, { signal })
    .then((data) => { if (ttl) cache.set(path, { t: Date.now(), data }); return data; })
    .finally(() => inflight.delete(path));
  if (!signal) inflight.set(path, p);
  return p;
}

export const api = {
  get,
  post: (path, body) => request(path, { method: "POST", body: body ?? {} }),
  put: (path, body) => request(path, { method: "PUT", body }),
  patch: (path, body) => request(path, { method: "PATCH", body }),
  del: (path) => request(path, { method: "DELETE" }),
  upload: (path, formData) => request(path, { method: "POST", body: formData }),
  // أي تعديل بيمسح الكاش (عشان الأرقام ماتبقاش قديمة)
  invalidate: () => cache.clear(),
};

// تنزيل ملف (Excel وغيره) مع احترام اسم الملف من السيرفر
export async function download(path, fallbackName = "export.xlsx") {
  const res = await request(path, { raw: true });
  if (!res.ok) {
    let msg = "مقدرش أنزل الملف";
    try { msg = parseDetail(await res.json()) || msg; } catch (e) { /* ignore */ }
    throw new ApiError(res.status, msg);
  }
  const blob = await res.blob();
  const cd = res.headers.get("Content-Disposition") || "";
  const m = cd.match(/filename\*=UTF-8''([^;]+)/i) || cd.match(/filename="?([^";]+)"?/i);
  const filename = m ? decodeURIComponent(m[1]) : fallbackName;
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url; a.download = filename;
  document.body.appendChild(a); a.click(); a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

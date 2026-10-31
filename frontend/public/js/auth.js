// =====================================================================
//  auth.js — حالة المستخدم الحالي وصلاحياته.
//  ⚠ كل ده تلميحات للواجهة بس (إخفاء أزرار وصفحات). السيرفر هو اللي بيفرض الصلاحيات على كل طلب.
// =====================================================================
import { api, clearAuthHints, loginUrl } from "./api.js";

function readPerms() {
  try {
    const p = JSON.parse(localStorage.getItem("perms") || "[]");
    return Array.isArray(p) ? p : [];
  } catch (e) { return []; }
}

export function getUser() {
  const username = localStorage.getItem("username") || "";
  return {
    username,
    role: localStorage.getItem("role") || "",
    name: localStorage.getItem("full_name") || username,
    isAdmin: localStorage.getItem("is_admin") === "1",
    perms: readPerms(),
  };
}

// تخزين رد login أو /me (نفس الشكل)
export function storeUser(u) {
  localStorage.setItem("username", u.username);
  localStorage.setItem("role", u.role || "");
  localStorage.setItem("role_id", String(u.role_id ?? ""));
  localStorage.setItem("is_admin", u.is_admin ? "1" : "0");
  localStorage.setItem("perms", JSON.stringify(u.permissions || []));
  if (u.full_name) localStorage.setItem("full_name", u.full_name); else localStorage.removeItem("full_name");
}

export const isAdmin = () => localStorage.getItem("is_admin") === "1";

// can("a", "b") = عنده واحدة على الأقل (any-of). الأدمن عنده كل حاجة.
export function can(...keys) {
  if (isAdmin()) return true;
  const have = readPerms();
  return keys.some((k) => have.includes(k));
}

// canAll("a", "b") = عنده كلهم (all-of)
export function canAll(...keys) {
  if (isAdmin()) return true;
  const have = readPerms();
  return keys.every((k) => have.includes(k));
}

export function goLogin() {
  clearAuthHints();
  if (!location.pathname.endsWith("login") && !location.pathname.endsWith("login.html")) location.replace(loginUrl());
}

// بيحمي الصفحة: مفيش جلسة => login. الصفحة محتاجة صلاحية (perm = any-of) أو أدمن (adminOnly) وانت ملكش => الرئيسية.
export function guard({ perm = [], adminOnly = false } = {}) {
  if (!localStorage.getItem("username")) { goLogin(); return false; }
  if (adminOnly && !isAdmin()) { location.replace("index.html"); return false; }
  const keys = Array.isArray(perm) ? perm : String(perm).split(",").map((s) => s.trim()).filter(Boolean);
  if (keys.length && !can(...keys)) { location.replace("index.html"); return false; }
  return true;
}

// بيتأكد من الجلسة فعليًا من السيرفر ويحدّث الاسم/الدور/الصلاحيات (بيشتغل في الخلفية)
export async function refreshMe() {
  try {
    const me = await api.get("/api/auth/me", { ttl: 60_000 });
    if (!me) return null;
    if (me.role_active === false) { window.dispatchEvent(new CustomEvent("app-role-disabled", { detail: "الدور بتاعك موقوف، كلّم الأدمن" })); return me; }
    const before = JSON.stringify([localStorage.getItem("role_id"), localStorage.getItem("is_admin"), localStorage.getItem("perms")]);
    const had = localStorage.getItem("perms") !== null;
    storeUser(me);
    window.dispatchEvent(new CustomEvent("app-user", { detail: getUser() }));
    const after = JSON.stringify([localStorage.getItem("role_id"), localStorage.getItem("is_admin"), localStorage.getItem("perms")]);
    // لو الدور/الصلاحيات اتغيّرت من السيرفر نعيد تحميل الصفحة عشان القوايم والأزرار تتظبط
    if (had && before !== after) location.reload();
    return me;
  } catch (e) {
    return null;
  }
}

export async function logout() {
  try { await api.post("/api/auth/logout"); } catch (e) { /* الجلسة منتهية أصلاً */ }
  clearAuthHints();
  location.replace("login.html"); // بعد الخروج الصريح: صفحة الدخول من غير next
}

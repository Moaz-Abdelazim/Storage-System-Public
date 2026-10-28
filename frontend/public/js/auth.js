// =====================================================================
//  auth.js — حالة المستخدم الحالي (تلميحات للواجهة بس، السيرفر هو اللي بيفرض الصلاحيات).
// =====================================================================
import { api, clearAuthHints, loginUrl } from "./api.js";

export function getUser() {
  const username = localStorage.getItem("username") || "";
  return {
    username,
    role: localStorage.getItem("role") || "",
    name: localStorage.getItem("full_name") || username,
  };
}

export const isAdmin = () => localStorage.getItem("role") === "admin";

export function goLogin() {
  clearAuthHints();
  if (!location.pathname.endsWith("login") && !location.pathname.endsWith("login.html")) location.replace(loginUrl());
}

// بيحمي الصفحة: لو مفيش جلسة => login. لو الصفحة للأدمن بس وانت مش أدمن => الرئيسية.
export function guard({ adminOnly = false } = {}) {
  if (!localStorage.getItem("username")) { goLogin(); return false; }
  if (adminOnly && !isAdmin()) { location.replace("index.html"); return false; }
  return true;
}

// بيتأكد من الجلسة فعليًا من السيرفر ويحدّث الاسم/الدور (بيشتغل في الخلفية)
export async function refreshMe() {
  try {
    const me = await api.get("/api/auth/me", { ttl: 60_000 });
    if (!me) return null;
    const prevRole = localStorage.getItem("role");
    localStorage.setItem("username", me.username);
    localStorage.setItem("role", me.role);
    if (me.full_name) localStorage.setItem("full_name", me.full_name);
    else localStorage.removeItem("full_name");
    window.dispatchEvent(new CustomEvent("app-user", { detail: getUser() }));
    // لو الدور اتغير من السيرفر (مثلاً اتشال منه الأدمن) نعيد تحميل الصفحة عشان القوايم تتظبط
    if (prevRole && prevRole !== me.role) location.reload();
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
// // عنوان الباك اند:
// //  - HTTPS (ورا Nginx/Caddy): نفس الـ origin، والـ proxy بيوصّل /api للباك اند (الوضع الموصى بيه للـ production).
// //  - HTTP (تطوير/شبكة داخلية): نفس الـ host على بورت 8000.
// // ممكن تحدد عنوان يدوي بـ window.API_BASE_OVERRIDE قبل تحميل الصفحة (من ملف config.local.js مثلاً).
// const BACKEND_PORT = 8000;

// function detectApiBase() {
//   if (typeof window.API_BASE_OVERRIDE === "string") return window.API_BASE_OVERRIDE;
//   if (window.location.protocol === "https:") return "";
//   return `http://${window.location.hostname || "localhost"}:${BACKEND_PORT}`;
// }

export const API_BASE = "";

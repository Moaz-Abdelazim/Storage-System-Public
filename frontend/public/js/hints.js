// =====================================================================
//  hints.js — رسائل إرشاد "لأول مرة" قابلة للإخفاء. الاستخدام في HTML:
//    <div x-data="hint('اسم-فريد')" x-show="open" x-cloak> ... <button @click="dismiss()">فهمت</button></div>
//  الإخفاء بيتحفظ في localStorage لكل مستخدم على نفس المتصفح (مستخدم جديد بيشوف الرسالة).
// =====================================================================
import { getUser } from "./auth.js";

const keyOf = (name) => `hint:${getUser().username || "anon"}:${name}`;
const seen = (name) => { try { return localStorage.getItem(keyOf(name)) === "1"; } catch { return false; } };

document.addEventListener("alpine:init", () => {
  window.Alpine.data("hint", (name) => ({
    open: !seen(name),
    dismiss() { this.open = false; try { localStorage.setItem(keyOf(name), "1"); } catch { /* التخزين مش متاح: الرسالة هترجع المرة الجاية */ } },
  }));
});

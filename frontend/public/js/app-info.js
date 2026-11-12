// =====================================================================
//  app-info.js — اسم المنتج من مصدر واحد: backend/project.config.json (عن طريق GET /api/app-info).
//  - أي عنصر عليه data-app-name بيتملّى بالاسم (القائمة الجانبية، تسجيل الدخول، الإعداد الأول، ...).
//  - عنوان التبويب بيتركّب "<عنوان الصفحة> | <الاسم>" (الـ <title> في HTML بيبقى عنوان الصفحة بس).
//  - الاسم بيتخزّن في localStorage كـ cache عشان يظهر فورًا في الزيارات التالية من غير ما يستنى الـ API.
//  مفيش أي اسم منتج مكتوب في الواجهة: لو غيّرت `name` في project.config.json كل حاجة بتتحدّث لوحدها.
// =====================================================================
import { api } from "./api.js";

const KEY = "app_name";
let _name = "";
let _base = null; // عنوان الصفحة من غير اسم المنتج
let _promise = null;

function cached() {
  try { return localStorage.getItem(KEY) || ""; } catch { return ""; }
}

function composeTitle() {
  if (_base === null) _base = document.title.split(" | ")[0];
  document.title = _name && _base ? `${_base} | ${_name}` : (_base || _name);
}

export function applyAppName(name) {
  _name = name || "";
  document.querySelectorAll("[data-app-name]").forEach((el) => { el.textContent = _name; });
  document.querySelectorAll("[data-app-name-label]").forEach((el) => { el.setAttribute("aria-label", _name); });
  composeTitle();
}

/** بيحدد عنوان الصفحة (من غير الاسم) ويضيف اسم المنتج لو معروف. */
export function setPageTitle(title) {
  _base = title || "";
  composeTitle();
}

/** الاسم المعروف حاليًا (متزامن): من الـ API لو وصل، وإلا من الـ cache، وإلا نص فاضي. */
export function appName() {
  return _name || cached();
}

export function loadAppName() {
  if (!_promise) {
    applyAppName(cached());
    _promise = api.get("/api/app-info", { ttl: 300_000 })
      .then((d) => {
        const name = (d && typeof d.name === "string" ? d.name : "").trim();
        if (name) {
          try { localStorage.setItem(KEY, name); } catch { /* التخزين مش متاح: عادي */ }
          applyAppName(name);
        }
        return appName();
      })
      .catch(() => appName());
  }
  return _promise;
}

export const getAppName = loadAppName;

loadAppName();

// بيتحمّل في <head> (من غير defer) عشان الثيم يتطبّق قبل أول رسم ومايحصلش وميض.
(function () {
  try {
    var t = localStorage.getItem("theme");
    if (t === "dark" || (!t && window.matchMedia && window.matchMedia("(prefers-color-scheme: dark)").matches)) {
      document.documentElement.classList.add("dark");
    }
  } catch (e) { /* ignore */ }
  document.documentElement.classList.add("shell-pending");
})();

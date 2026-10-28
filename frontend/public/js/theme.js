// =====================================================================
//  theme.js — تبديل الوضع الليلي/النهاري بأنيميشن سلس:
//   - المتصفحات اللي بتدعم View Transitions: دايرة بتتوسّع من زرار التبديل.
//   - غير كده: fade ناعم للألوان. (ومن غير أنيميشن لو المستخدم مفعّل reduced-motion)
// =====================================================================
export function isDark() { return document.documentElement.classList.contains("dark"); }

export function applyTheme(dark, origin) {
  const root = document.documentElement;
  const commit = () => {
    root.classList.toggle("dark", dark);
    window.dispatchEvent(new CustomEvent("app-theme", { detail: { dark } }));
  };
  if (isDark() === dark) return;
  if (window.matchMedia("(prefers-reduced-motion: reduce)").matches) { commit(); return; }

  if (typeof document.startViewTransition === "function") {
    const r = origin && origin.getBoundingClientRect ? origin.getBoundingClientRect() : null;
    const x = r ? r.left + r.width / 2 : window.innerWidth / 2;
    const y = r ? r.top + r.height / 2 : 0;
    const radius = Math.hypot(Math.max(x, window.innerWidth - x), Math.max(y, window.innerHeight - y));
    const t = document.startViewTransition(commit);
    t.ready.then(() => {
      root.animate(
        { clipPath: [`circle(0px at ${x}px ${y}px)`, `circle(${radius}px at ${x}px ${y}px)`] },
        { duration: 600, easing: "cubic-bezier(.4, 0, .2, 1)", pseudoElement: "::view-transition-new(root)" },
      );
    }).catch(() => {});
    return;
  }
  root.classList.add("theme-fade");
  commit();
  setTimeout(() => root.classList.remove("theme-fade"), 450);
}

export function toggleTheme(origin) {
  const dark = !isDark();
  try { localStorage.setItem("theme", dark ? "dark" : "light"); } catch (e) { /* ignore */ }
  applyTheme(dark, origin);
}

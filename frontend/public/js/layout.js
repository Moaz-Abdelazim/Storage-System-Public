// =====================================================================
//  layout.js — الـ Shell المشترك (سايدبار + شريط علوي + Toasts).
//  كل صفحة بتحط محتواها جوه:
//     <div id="shell" data-active="dashboard" data-title="الرئيسية" [data-perm="products.view,stock_in.create"] [data-admin-only]> ...المحتوى... </div>
//  data-perm = أي صلاحية من المذكورين تفتح الصفحة (any-of) | data-admin-only = مدير النظام بس. (تلميحات؛ السيرفر هو اللي بيفرض)
//  والملف ده بيلفّ المحتوى بالـ Shell. لازم يتحمّل قبل Alpine (ترتيب الـ scripts في الصفحة).
// =====================================================================
import { guard, getUser, can, isAdmin, logout, refreshMe } from "./auth.js";
import { api } from "./api.js";
import "./ui.js"; // <ui-select> و <ui-pager> و <loc-code> متاحين في كل الصفحات
import { toggleTheme } from "./theme.js";

// ---------- قايمة التنقل ----------
// perm = أي صلاحية من القايمة تظهر العنصر (any-of) | adminOnly = مدير النظام بس. من غير أي منهم = لأي مسجّل.
const NAV = [
  { id: "dashboard", label: "الرئيسية", icon: "fa-house", href: "index.html" },
  { group: "products", label: "المنتجات", icon: "fa-box", items: [
    { id: "products", label: "كل المنتجات", href: "products.html", perm: ["products.view"] },
    { id: "categories", label: "التصنيفات", href: "categories.html", perm: ["products.view"] },
    { id: "brands", label: "الماركات", href: "brands.html", perm: ["products.view"] },
  ] },
  { group: "locations", label: "المخازن والمواقع", icon: "fa-location-dot", items: [
    { id: "locations", label: "إدارة المواقع", href: "locations.html", perm: ["locations.view"] },
    { id: "transfers", label: "النقل بين المواقع", href: "transfers.html", perm: ["transfers.view", "transfers.create"] },
    { id: "lookup", label: "البحث عن موقع", href: "location-lookup.html", perm: ["locations.view"] },
  ] },
  { group: "stock", label: "حركة المخزون", icon: "fa-right-left", items: [
    { id: "stock-in", label: "وارد (فاتورة دخول)", href: "stock-in.html", perm: ["stock_in.create"] },
    { id: "stock-out", label: "صادر (فاتورة خروج)", href: "stock-out.html", perm: ["stock_out.create"] },
    { id: "movements", label: "سجل الحركات", href: "movements.html", perm: ["invoices.view", "transfers.view"] },
  ] },
  { group: "parties", label: "الأطراف", icon: "fa-handshake", items: [
    { id: "suppliers", label: "الموردين", href: "suppliers.html", perm: ["suppliers.view"] },
    { id: "recipients", label: "جهات الاستلام", href: "recipients.html", perm: ["recipients.view"] },
  ] },
  { id: "reports", label: "التقارير", icon: "fa-chart-column", href: "reports.html", perm: ["reports.view"] },
  { group: "admin", label: "الإدارة", icon: "fa-user-gear", items: [
    { id: "users", label: "المستخدمين", href: "users.html", perm: ["users.manage"] },
    { id: "roles", label: "الأدوار والصلاحيات", href: "roles.html", adminOnly: true },
    { id: "login-logs", label: "سجل الدخول", href: "login-logs.html", perm: ["logs.view"] },
  ] },
];

const allowed = (n) => (n.adminOnly ? isAdmin() : !n.perm || can(...n.perm));

const LINK_BASE = "flex items-center gap-3 rounded-xl px-3.5 h-11 text-[15px] transition-colors";
const LINK_ON = "bg-brand-500 text-white font-medium shadow-lg shadow-brand-500/25";
const LINK_OFF = "text-slate-300 hover:bg-white/5 hover:text-white";

function navHtml(active) {
  // مجموعة من غير ولا عنصر مسموح بيها بتختفي
  const visible = NAV
    .map((n) => (n.group ? { ...n, items: n.items.filter(allowed) } : n))
    .filter((n) => (n.group ? n.items.length : allowed(n)));
  return visible.map((n) => {
    if (!n.group) {
      const on = n.id === active;
      return `<a href="${n.href}" class="${LINK_BASE} ${on ? LINK_ON : LINK_OFF}" ${on ? 'aria-current="page"' : ""}>
        <i class="fa-solid ${n.icon} w-5 text-center text-[17px]"></i><span>${n.label}</span></a>`;
    }
    const hasActive = n.items.some((i) => i.id === active);
    const items = n.items.map((i) => {
      const on = i.id === active;
      return `<a href="${i.href}" class="flex h-9 items-center gap-2 rounded-lg px-3 text-sm transition-colors ${on ? "bg-brand-500/15 font-medium text-brand-300" : "text-slate-400 hover:text-white"}" ${on ? 'aria-current="page"' : ""}>
        <span class="h-1.5 w-1.5 rounded-full ${on ? "bg-brand-400" : "bg-slate-600"}"></span>${i.label}</a>`;
    }).join("");
    return `<div x-data="{ open: ${hasActive} }">
      <button type="button" @click="open = !open" :aria-expanded="open" class="${LINK_BASE} w-full ${hasActive ? "text-white" : LINK_OFF}">
        <i class="fa-solid ${n.icon} w-5 text-center text-[17px]"></i><span class="flex-1 text-start">${n.label}</span>
        <i class="fa-solid fa-chevron-down text-[10px] text-slate-500 transition-transform" :class="open && 'rotate-180'"></i>
      </button>
      <div x-show="open" x-cloak x-transition.opacity.duration.150ms class="ms-[1.45rem] mt-1 space-y-0.5 border-s border-white/10 ps-2">${items}</div>
    </div>`;
  }).join("");
}

function shellHtml(active) {
  return `
  <div x-show="drawer" x-cloak x-transition.opacity class="fixed inset-0 z-30 bg-black/50 lg:hidden" @click="drawer = false"></div>

  <aside class="fixed inset-y-0 start-0 z-40 flex w-[280px] flex-col bg-side text-slate-300 transition-transform duration-200 lg:sticky lg:top-0 lg:h-screen lg:shrink-0 lg:translate-x-0"
         :class="drawer ? 'translate-x-0' : 'translate-x-full'" aria-label="القائمة الرئيسية">
    <a href="index.html" class="flex items-center gap-3 px-6 pb-4 pt-6" aria-label="مخزني">
      <div class="leading-tight">
        <div class="text-[32px] font-bold leading-none text-white">مخزني</div>
        <div class="mt-1.5 text-[11px] text-slate-400">إدارة المخازن والمخزون</div>
      </div>
      <svg class="ms-auto h-11 w-11 shrink-0" viewBox="0 0 64 64" fill="none" aria-hidden="true">
        <path d="M9 58V27L32 8l23 19v31" stroke="#fff" stroke-width="7" stroke-linecap="round" stroke-linejoin="round"/>
        <rect x="24" y="27" width="16" height="13" rx="2.5" fill="#10B981"/>
        <rect x="15" y="42" width="16" height="14" rx="2.5" fill="#10B981"/>
        <rect x="33" y="42" width="16" height="14" rx="2.5" fill="#10B981"/>
      </svg>
    </a>

    <nav class="flex-1 space-y-1 overflow-y-auto px-3 py-3">${navHtml(active)}</nav>

    <div class="space-y-1 border-t border-white/10 px-3 py-3">
      <a href="settings.html" class="${LINK_BASE} ${active === "settings" ? LINK_ON : LINK_OFF}">
        <i class="fa-solid fa-gear w-5 text-center text-[17px]"></i><span>الإعدادات</span></a>
      <button type="button" @click="logout()" class="${LINK_BASE} ${LINK_OFF} w-full">
        <i class="fa-solid fa-arrow-right-from-bracket w-5 text-center text-[17px]"></i><span>تسجيل الخروج</span></button>
    </div>
  </aside>

  <div class="flex min-w-0 flex-1 flex-col">
    <header class="sticky top-0 z-20 flex h-16 items-center gap-2 border-b border-line bg-card/90 px-4 backdrop-blur lg:px-8">
      <button type="button" class="btn btn-ghost btn-icon lg:hidden" @click="drawer = true" aria-label="فتح القائمة">
        <i class="fa-solid fa-bars"></i></button>

      <form class="relative min-w-0 max-w-xl flex-1" @submit.prevent="search()" role="search">
        <i class="fa-solid fa-magnifying-glass pointer-events-none absolute start-3.5 top-1/2 -translate-y-1/2 text-sm text-muted"></i>
        <input type="search" x-model="q" class="input bg-soft ps-10" placeholder="ابحث عن منتج..." aria-label="بحث عن منتج">
      </form>

      <div class="ms-auto flex items-center gap-1">
        <button type="button" class="btn btn-ghost btn-icon overflow-hidden" @click="toggleTheme($el)" aria-label="تبديل الوضع الليلي">
          <span class="grid text-base">
            <i class="fa-solid fa-moon col-start-1 row-start-1 transition-all duration-500 ease-out dark:-rotate-90 dark:scale-0 dark:opacity-0"></i>
            <i class="fa-solid fa-sun col-start-1 row-start-1 rotate-90 scale-0 opacity-0 transition-all duration-500 ease-out dark:rotate-0 dark:scale-100 dark:opacity-100"></i>
          </span>
        </button>

        <a href="products.html#stock=low" class="btn btn-ghost btn-icon relative" aria-label="تنبيهات النواقص">
          <i class="fa-regular fa-bell text-[17px]"></i>
          <span x-show="lowCount > 0" x-cloak x-text="lowCount > 99 ? '99+' : lowCount"
                class="absolute -end-0.5 -top-0.5 grid h-[18px] min-w-[18px] place-items-center rounded-full bg-red-500 px-1 text-[10px] font-bold text-white"></span>
        </a>

        <div class="relative ms-1 ps-2" @click.outside="menu = false">
          <button type="button" class="flex items-center gap-2.5 rounded-xl py-1 pe-1 ps-2 transition hover:bg-soft" @click="menu = !menu" :aria-expanded="menu" aria-haspopup="true">
            <span class="grid h-9 w-9 place-items-center rounded-full bg-brand-500 text-sm font-bold text-white" x-text="initial"></span>
            <span class="hidden text-start leading-tight sm:block">
              <span class="block max-w-[140px] truncate text-sm font-medium" x-text="user.name"></span>
              <span class="block text-xs text-muted" x-text="roleLabel"></span>
            </span>
            <i class="fa-solid fa-chevron-down hidden text-[10px] text-muted sm:block"></i>
          </button>
          <div x-show="menu" x-cloak x-transition.origin.top class="card absolute end-0 top-full z-30 mt-2 w-52 p-1.5 shadow-pop">
            <a href="settings.html" class="flex h-10 items-center gap-3 rounded-lg px-3 text-sm hover:bg-soft"><i class="fa-solid fa-gear w-4 text-muted"></i>الإعدادات</a>
            <button type="button" @click="logout()" class="flex h-10 w-full items-center gap-3 rounded-lg px-3 text-sm text-red-600 hover:bg-red-500/10 dark:text-red-400">
              <i class="fa-solid fa-arrow-right-from-bracket w-4"></i>تسجيل الخروج</button>
          </div>
        </div>
      </div>
    </header>

    <main id="page" class="mx-auto w-full max-w-[1440px] flex-1 px-4 py-6 lg:px-8"></main>
  </div>

  <div x-show="confirm.open" x-cloak x-transition.opacity class="modal z-[70]" role="alertdialog" aria-modal="true" @keydown.escape.window="answer(false)">
    <div class="modal-backdrop" @click="answer(false)"></div>
    <div class="modal-panel max-w-sm">
      <div class="px-6 pb-2 pt-6 text-center">
        <div class="mx-auto mb-4 grid h-14 w-14 place-items-center rounded-full text-2xl"
             :class="confirm.danger ? 'bg-red-500/10 text-red-500' : 'bg-brand-500/10 text-brand-500'">
          <i class="fa-solid" :class="confirm.danger ? 'fa-triangle-exclamation' : 'fa-circle-question'"></i></div>
        <h3 class="text-lg font-bold" x-text="confirm.title"></h3>
        <p class="mt-2 text-sm leading-relaxed text-muted" x-text="confirm.message"></p>
      </div>
      <div class="flex gap-2 p-5">
        <button type="button" class="btn btn-outline flex-1" @click="answer(false)">إلغاء</button>
        <button type="button" class="btn flex-1" :class="confirm.danger ? 'btn-danger' : 'btn-primary'" @click="answer(true)" x-text="confirm.confirmText"></button>
      </div>
    </div>
  </div>

  <div class="pointer-events-none fixed bottom-4 start-4 z-[60] flex w-[min(92vw,360px)] flex-col gap-2" aria-live="polite">
    <template x-for="t in toasts" :key="t.id">
      <div x-transition class="toast" :class="t.type === 'error' ? 'toast-err' : 'toast-ok'" role="status">
        <i class="fa-solid mt-0.5" :class="t.type === 'error' ? 'fa-circle-exclamation' : 'fa-circle-check'"></i>
        <span x-text="t.message" class="flex-1"></span>
      </div>
    </template>
  </div>`;
}

// ---------- بيانات Alpine للـ shell ----------
function shellData() {
  return {
    drawer: false,
    menu: false,
    q: new URLSearchParams(location.search).get("search") || "",
    user: getUser(),
    lowCount: 0,
    toasts: [],
    _tid: 0,
    confirm: { open: false, title: "", message: "", confirmText: "تأكيد", danger: false },
    _confirmResolve: null,

    init() {
      window.addEventListener("app-user", (e) => { this.user = e.detail; });
      window.addEventListener("app-role-disabled", (e) => {
        if (this._roleOff) return;
        this._roleOff = true;
        this.pushToast(e.detail || "الدور بتاعك موقوف، كلّم الأدمن", "error");
        setTimeout(() => logout(), 2500);
      });
      window.addEventListener("app-confirm", (e) => {
        const { resolve, ...opts } = e.detail;
        this._confirmResolve = resolve;
        this.confirm = { ...opts, open: true };
      });
      window.addEventListener("app-toast", (e) => this.pushToast(e.detail.message, e.detail.type));
      window.addEventListener("keydown", (e) => { if (e.key === "Escape") { this.drawer = false; this.menu = false; } });
      this.loadAlerts();
      refreshMe();
    },

    get initial() { return (this.user.name || "?").trim().charAt(0).toUpperCase(); },
    get roleLabel() { return this.user.role || (this.user.isAdmin ? "مدير النظام" : "مستخدم"); },

    async loadAlerts() {
      try {
        const s = await api.get("/api/reports/summary", { ttl: 30_000 });
        this.lowCount = s.low_stock_count || 0;
      } catch (e) { /* التنبيه مش حرج (ممكن 403 لو الدور موقوف) */ }
    },

    search() {
      const v = this.q.trim();
      location.href = `products.html${v ? `#search=${encodeURIComponent(v)}` : ""}`;
    },

    toggleTheme(origin) { toggleTheme(origin); },

    answer(ok) {
      if (!this.confirm.open) return;
      this.confirm.open = false;
      if (this._confirmResolve) this._confirmResolve(ok);
      this._confirmResolve = null;
    },

    pushToast(message, type = "success") {
      const id = ++this._tid;
      this.toasts.push({ id, message, type });
      setTimeout(() => { this.toasts = this.toasts.filter((t) => t.id !== id); }, type === "error" ? 5000 : 3200);
    },

    logout,
  };
}

// ---------- التركيب ----------
function mount() {
  const el = document.getElementById("shell");
  if (!el) return;
  const perm = (el.dataset.perm || "").split(",").map((x) => x.trim()).filter(Boolean);
  if (!guard({ adminOnly: el.hasAttribute("data-admin-only"), perm })) return;

  const title = el.dataset.title;
  if (title) document.title = `${title} | مخزني`;

  const content = document.createDocumentFragment();
  while (el.firstChild) content.appendChild(el.firstChild);

  el.className = "min-h-screen lg:flex";
  el.setAttribute("x-data", "shell");
  el.innerHTML = shellHtml(el.dataset.active || "");
  el.querySelector("#page").appendChild(content);

  document.documentElement.classList.remove("shell-pending");
}

document.addEventListener("alpine:init", () => window.Alpine.data("shell", shellData));
mount();

// =====================================================================
//  ui.js — عناصر واجهة مشتركة (Custom Elements):
//   <ui-select source="categories" placeholder="..." clearable> : قايمة منسدلة بحث (محلية أو remote)
//   <ui-pager page pages total size> : ترقيم الصفحات
//  الأحداث: ui-select بيطلق "pick" بـ detail {value, option}، و ui-pager بيطلق "go" بـ detail = رقم الصفحة.
// =====================================================================
import { api } from "./api.js";
import { loadSource, REMOTE_SOURCES } from "./data.js";
import { debounce } from "./utils.js";

const norm = (s) => String(s ?? "").toLowerCase().replace(/[\u064B-\u0652]/g, "").replace(/[إأآ]/g, "ا").replace(/ى/g, "ي").replace(/ة/g, "ه");

class UiSelect extends HTMLElement {
  connectedCallback() {
    if (this._ready) return;
    this._ready = true;
    this._options = [];
    this._value = null;
    this._selected = null;
    this._active = -1;
    this._open = false;
    this.classList.add("relative", "block");
    this.innerHTML = `
      <button type="button" class="input flex items-center justify-between gap-2 text-start disabled:opacity-60" aria-haspopup="listbox" aria-expanded="false">
        <span data-label class="min-w-0 flex-1 truncate"></span>
        <span class="flex items-center gap-2 text-muted">
          <span data-clear-wrap class="hidden"><i data-clear class="fa-solid fa-xmark rounded p-0.5 text-xs hover:text-red-500" role="button" aria-label="مسح"></i></span>
          <i class="fa-solid fa-chevron-down text-[10px]"></i>
        </span>
      </button>
      <div data-panel class="absolute inset-x-0 z-40 mt-1 hidden min-w-[230px] overflow-hidden rounded-xl border border-line bg-card shadow-pop">
        <div class="border-b border-line p-2"><input data-search type="text" class="input h-9 bg-soft text-[13px]" placeholder="ابحث..." autocomplete="off"></div>
        <ul data-list role="listbox" class="max-h-60 overflow-y-auto p-1"></ul>
      </div>`;
    this._btn = this.querySelector("button");
    this._labelEl = this.querySelector("[data-label]");
    this._clearEl = this.querySelector("[data-clear]");
    this._clearWrap = this.querySelector("[data-clear-wrap]");
    this._panel = this.querySelector("[data-panel]");
    this._search = this.querySelector("[data-search]");
    this._list = this.querySelector("[data-list]");

    this._btn.addEventListener("click", (e) => {
      if (e.target === this._clearEl) { e.stopPropagation(); this.clear(true); return; }
      this._open ? this.close() : this.open();
    });
    this._search.addEventListener("input", () => this._onSearch());
    this._search.addEventListener("keydown", (e) => this._onKey(e));
    this._btn.addEventListener("keydown", (e) => { if (["ArrowDown", "Enter", " "].includes(e.key) && !this._open) { e.preventDefault(); this.open(); } });
    this._list.addEventListener("click", (e) => {
      const li = e.target.closest("li[data-i]");
      if (li) this._choose(this._filtered[Number(li.dataset.i)]);
    });
    this._outside = (e) => { if (this._open && !this.contains(e.target)) this.close(); };
    document.addEventListener("click", this._outside);

    this._remote = REMOTE_SOURCES[this.getAttribute("source")] || null;
    this._debouncedRemote = debounce(() => this._fetchRemote(), 250);
    this._applyAttrs();
    this._renderLabel();
    this.reload();
  }

  disconnectedCallback() { document.removeEventListener("click", this._outside); }

  static get observedAttributes() { return ["disabled", "placeholder"]; }
  attributeChangedCallback() { if (this._ready) this._applyAttrs(); }

  _applyAttrs() {
    this._btn.disabled = this.hasAttribute("disabled");
    this._search.placeholder = this._remote ? "اكتب للبحث..." : "ابحث...";
    this._renderLabel();
  }

  get value() { return this._value; }
  set value(v) {
    if (v === undefined || v === "") v = null;
    if (v === this._value) return;
    this._value = v;
    this._selected = v == null ? null : (this._options || []).find((o) => String(o.value) === String(v)) || (this._selected && String(this._selected.value) === String(v) ? this._selected : null);
    this._renderLabel();
  }
  get option() { return this._selected; }

  // بيحدد الاختيار بكائن option كامل (مفيد للـ remote أو لما الـ options لسه ماتحمّلتش)
  setSelected(option) {
    this._selected = option || null;
    this._value = option ? option.value : null;
    this._renderLabel();
  }

  setOptions(options) {
    this._options = options || [];
    if (this._value != null) {
      const found = this._options.find((o) => String(o.value) === String(this._value));
      if (found) this._selected = found;
    }
    this._renderLabel();
    if (this._open) this._renderList();
  }

  async reload() {
    const src = this.getAttribute("source");
    if (!src || this._remote) return;
    try { this.setOptions(await loadSource(src)); } catch (e) { this._error = true; this._renderLabel(); }
  }

  clear(emit = false) {
    this._value = null; this._selected = null;
    this._renderLabel();
    if (emit) this._emit();
  }

  _renderLabel() {
    if (!this._labelEl) return;
    const sel = this._selected;
    const ph = this.getAttribute("placeholder") || "اختر...";
    this._labelEl.textContent = sel ? sel.label : this._error ? "تعذّر تحميل القائمة" : ph;
    this._labelEl.classList.toggle("text-muted", !sel);
    this._labelEl.classList.toggle("opacity-70", !sel);
    this._clearWrap.classList.toggle("hidden", !(sel && this.hasAttribute("clearable")) || this.hasAttribute("disabled"));
  }

  open() {
    if (this.hasAttribute("disabled")) return;
    this._open = true;
    this._btn.setAttribute("aria-expanded", "true");
    this._panel.classList.remove("hidden");
    // لو مفيش مساحة تحت نفتح لفوق
    const r = this.getBoundingClientRect();
    const flip = window.innerHeight - r.bottom < 300 && r.top > window.innerHeight - r.bottom;
    this._panel.classList.toggle("bottom-full", flip);
    this._panel.classList.toggle("mb-1", flip);
    this._panel.classList.toggle("mt-1", !flip);
    this._search.value = "";
    if (this._remote) this._fetchRemote(); else this._renderList();
    setTimeout(() => this._search.focus(), 0);
  }

  close() {
    this._open = false;
    this._btn.setAttribute("aria-expanded", "false");
    this._panel.classList.add("hidden");
  }

  _onSearch() {
    if (this._remote) { this._debouncedRemote(); return; }
    this._renderList();
  }

  async _fetchRemote() {
    this._list.innerHTML = `<li class="px-3 py-6 text-center text-sm text-muted"><i class="fa-solid fa-spinner animate-spin"></i></li>`;
    try {
      this._options = await this._remote(this._search.value.trim());
      this._renderList();
    } catch (e) {
      this._list.innerHTML = `<li class="px-3 py-6 text-center text-sm text-red-500">${e.message || "تعذّر البحث"}</li>`;
    }
  }

  _renderList() {
    const q = norm(this._search.value);
    this._filtered = this._remote || !q
      ? this._options
      : this._options.filter((o) => norm(`${o.label} ${o.sub || ""}`).includes(q));
    this._active = Math.max(0, this._filtered.findIndex((o) => this._value != null && String(o.value) === String(this._value)));
    if (!this._filtered.length) {
      this._list.innerHTML = `<li class="px-3 py-6 text-center text-sm text-muted">لا توجد نتائج</li>`;
      return;
    }
    const esc = (s) => String(s ?? "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
    this._list.innerHTML = this._filtered.slice(0, 200).map((o, i) => `
      <li data-i="${i}" role="option" class="cursor-pointer rounded-lg px-3 py-2 text-sm ${i === this._active ? "bg-brand-500/10" : "hover:bg-soft"} ${String(o.value) === String(this._value) ? "font-semibold text-brand-600 dark:text-brand-400" : ""}">
        <div class="truncate ${/^[A-Za-z0-9\-:_ ]+$/.test(o.label) ? "text-right" : ""}" ${/^[A-Za-z0-9\-:_ ]+$/.test(o.label) ? 'dir="ltr"' : ""}>${esc(o.label)}</div>
        ${o.sub ? `<div class="truncate text-xs text-muted">${esc(o.sub)}</div>` : ""}
      </li>`).join("") + (this._remote && this._options.truncated ? `<li class="px-3 py-2 text-center text-xs text-muted">في نتايج أكتر — اكتب للبحث عن المزيد</li>` : "");
  }

  _onKey(e) {
    const n = this._filtered ? this._filtered.length : 0;
    if (e.key === "ArrowDown") { e.preventDefault(); this._active = Math.min(n - 1, this._active + 1); this._renderList2(); }
    else if (e.key === "ArrowUp") { e.preventDefault(); this._active = Math.max(0, this._active - 1); this._renderList2(); }
    else if (e.key === "Enter") { e.preventDefault(); if (n) this._choose(this._filtered[this._active]); }
    else if (e.key === "Escape") { e.stopPropagation(); this.close(); this._btn.focus(); }
  }

  _renderList2() {
    this._list.querySelectorAll("li[data-i]").forEach((li) => {
      const on = Number(li.dataset.i) === this._active;
      li.classList.toggle("bg-brand-500/10", on);
      if (on) li.scrollIntoView({ block: "nearest" });
    });
  }

  _choose(opt) {
    if (!opt) return;
    this.setSelected(opt);
    this.close();
    this._emit();
  }

  _emit() {
    this.dispatchEvent(new CustomEvent("pick", { detail: { value: this._value, option: this._selected }, bubbles: true }));
  }
}

class UiPager extends HTMLElement {
  static get observedAttributes() { return ["page", "pages", "total", "size"]; }
  connectedCallback() { this.render(); }
  attributeChangedCallback() { this.render(); }

  render() {
    const page = Number(this.getAttribute("page")) || 1;
    const pages = Number(this.getAttribute("pages")) || 1;
    const total = Number(this.getAttribute("total")) || 0;
    const size = Number(this.getAttribute("size")) || 10;
    if (!total) { this.innerHTML = ""; return; }
    const from = (page - 1) * size + 1;
    const to = Math.min(total, page * size);
    const nums = [];
    for (let i = 1; i <= pages; i++) {
      if (i === 1 || i === pages || Math.abs(i - page) <= 1) nums.push(i);
      else if (nums[nums.length - 1] !== "…") nums.push("…");
    }
    const btn = "inline-flex h-8 min-w-8 items-center justify-center rounded-lg border px-2 text-[13px] font-medium transition";
    this.className = "flex flex-wrap items-center justify-between gap-3 px-1 py-3 text-sm";
    this.innerHTML = `
      <span class="text-muted">عرض ${from.toLocaleString("en-US")}–${to.toLocaleString("en-US")} من ${total.toLocaleString("en-US")}</span>
      <nav class="flex items-center gap-1" aria-label="الصفحات">
        <button type="button" data-go="${page - 1}" ${page <= 1 ? "disabled" : ""} class="${btn} border-line bg-card hover:bg-soft disabled:pointer-events-none disabled:opacity-40" aria-label="السابق"><i class="fa-solid fa-chevron-right text-[10px]"></i></button>
        ${nums.map((n) => n === "…" ? `<span class="px-1 text-muted">…</span>` : `<button type="button" data-go="${n}" class="${btn} ${n === page ? "border-brand-500 bg-brand-500 text-white" : "border-line bg-card hover:bg-soft"}">${n}</button>`).join("")}
        <button type="button" data-go="${page + 1}" ${page >= pages ? "disabled" : ""} class="${btn} border-line bg-card hover:bg-soft disabled:pointer-events-none disabled:opacity-40" aria-label="التالي"><i class="fa-solid fa-chevron-left text-[10px]"></i></button>
      </nav>`;
    this.querySelectorAll("[data-go]").forEach((b) => b.addEventListener("click", () => {
      this.dispatchEvent(new CustomEvent("go", { detail: Number(b.dataset.go), bubbles: true }));
    }));
  }
}


// =====================================================================
//  <loc-code code="W1-F3-R5"> — كود مكان بيظهر له pop-up بالعنوان التفصيلي لما الماوس يقف عليه
//  (أو بالضغط على الموبايل): المخزن ← كل مستوى باسمه ونوعه. الوصف بيتجاب من السيرفر بس لما تحتاجه (وبيتخزّن مؤقتًا).
// =====================================================================
const escHtml = (s) => String(s ?? "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
let popEl = null;
let popOwner = null;
let showTimer = null;

function ensurePop() {
  if (popEl) return popEl;
  popEl = document.createElement("div");
  popEl.className = "loc-pop hidden pointer-events-none";
  popEl.setAttribute("role", "tooltip");
  document.body.appendChild(popEl);
  const hide = () => hidePop();
  window.addEventListener("scroll", hide, true);
  window.addEventListener("resize", hide);
  document.addEventListener("click", (e) => { if (popOwner && !popOwner.contains(e.target)) hidePop(); });
  document.addEventListener("keydown", (e) => { if (e.key === "Escape") hidePop(); });
  return popEl;
}

function placePop(host) {
  const r = host.getBoundingClientRect();
  const pw = popEl.offsetWidth, ph = popEl.offsetHeight;
  let left = r.left + r.width / 2 - pw / 2;
  left = Math.max(8, Math.min(left, window.innerWidth - pw - 8));
  let top = r.bottom + 8;
  if (top + ph > window.innerHeight - 8 && r.top - ph - 8 > 8) top = r.top - ph - 8;
  popEl.style.left = `${left}px`;
  popEl.style.top = `${top}px`;
}

function pathHtml(d) {
  const rows = d.path.map((p, i) => {
    const last = i === d.path.length - 1;
    const icon = p.kind === "warehouse" ? "fa-warehouse" : last && d.is_leaf ? "fa-box-archive" : "fa-layer-group";
    return `<li class="flex items-start gap-2.5">
      <span class="mt-0.5 grid h-6 w-6 shrink-0 place-items-center rounded-lg ${last ? "bg-brand-500 text-white" : "bg-soft text-muted"} text-[11px]"><i class="fa-solid ${icon}"></i></span>
      <span class="min-w-0 flex-1"><span class="block text-[11px] text-muted">${escHtml(p.type || "موقع")}</span>
      <span class="block truncate font-medium ${last ? "text-brand-600 dark:text-brand-400" : ""}">${escHtml(p.name)}</span></span>
      <span class="code-chip shrink-0 text-[11px]">${escHtml(p.full_code)}</span></li>`;
  }).join("");
  return `<ul class="space-y-2.5">${rows}</ul>${d.is_default_receiving ? '<div class="mt-3 rounded-lg bg-brand-500/10 px-2.5 py-1.5 text-xs text-brand-700 dark:text-brand-300"><i class="fa-solid fa-inbox"></i> مكان الاستلام الافتراضي للمخزن</div>' : ""}`;
}

async function showPop(host, code) {
  ensurePop();
  popOwner = host;
  popEl.innerHTML = `<div class="flex items-center gap-2 text-muted"><i class="fa-solid fa-spinner animate-spin"></i><span>جاري التحميل...</span></div>`;
  popEl.classList.remove("hidden");
  placePop(host);
  try {
    const d = await api.get(`/api/locations/nav/describe?code=${encodeURIComponent(code)}`, { ttl: 60_000 });
    if (popOwner !== host) return;
    popEl.innerHTML = pathHtml(d);
  } catch (e) {
    if (popOwner !== host) return;
    popEl.innerHTML = `<div class="text-muted">${escHtml(e.message || "تعذّر تحميل العنوان")}</div>`;
  }
  placePop(host);
}

function hidePop() {
  clearTimeout(showTimer);
  popOwner = null;
  if (popEl) popEl.classList.add("hidden");
}

class LocCode extends HTMLElement {
  static get observedAttributes() { return ["code"]; }
  connectedCallback() {
    if (this._ready) return;
    this._ready = true;
    this.classList.add("inline-flex");
    this.render();
    this.addEventListener("mouseenter", () => this.schedule());
    this.addEventListener("mouseleave", () => hidePop());
    this.addEventListener("focusin", () => this.schedule(0));
    this.addEventListener("focusout", () => hidePop());
    this.addEventListener("click", (e) => { e.stopPropagation(); popOwner === this ? hidePop() : this.schedule(0); });
  }
  attributeChangedCallback() { if (this._ready) this.render(); }
  schedule(delay = 180) {
    clearTimeout(showTimer);
    const code = this.getAttribute("code");
    if (!code) return;
    showTimer = setTimeout(() => showPop(this, code), delay);
  }
  render() {
    const code = this.getAttribute("code") || "";
    this.innerHTML = `<span class="code-chip cursor-help transition hover:border-brand-500 hover:text-brand-600" tabindex="0" role="button" aria-label="عنوان المكان ${escHtml(code)}">${escHtml(code)}</span>`;
  }
}
customElements.define("loc-code", LocCode);

customElements.define("ui-select", UiSelect);
customElements.define("ui-pager", UiPager);

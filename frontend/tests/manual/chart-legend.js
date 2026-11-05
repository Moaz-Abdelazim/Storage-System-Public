// يبني نفس هيكل ليجند "توزيع الكميات على المخازن" بأسماء قصيرة وطويلة جدًا (عربي/إنجليزي/من غير مسافات) في عروض مختلفة.
const NAMES = [
  "المخزن الرئيسي",
  "مخزن الفرع الرئيسي لمنطقة وسط البلد والدور الثالث بالعقار رقم 15 شارع طلعت حرب",
  "WAREHOUSE-NORTH-DISTRIBUTION-CENTER-BUILDING-A-GATE-4",
  "مخزنالطويلجداًبدونمسافاتمخزنالطويلجداًبدونمسافاتمخزنالطويلجداًبدونمسافات",
  "ص",
];
const COLORS = ["#10b981", "#f97316", "#3b82f6", "#a855f7", "#ef4444"];
const esc = (s) => String(s).replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
const row = (n, i) => `<li class="flex min-w-0 items-start gap-2">
  <span class="mt-1.5 h-2.5 w-2.5 shrink-0 rounded-full" data-bg="${COLORS[i]}"></span>
  <button type="button" class="min-w-0 flex-1 text-start truncate" title="${esc(n)}" aria-expanded="false">${esc(n)}</button>
  <span class="shrink-0 font-medium tabular-nums text-muted">${(20 - i * 3)}%</span></li>`;
const card = (w) => `<h3>عرض ${w}px</h3><div class="frame" style="width:${w}px"><section class="card card-pad">
  <h2 class="mb-4 text-base font-bold">توزيع الكميات على المخازن</h2>
  <div class="flex flex-wrap items-center justify-center gap-x-5 gap-y-4">
    <div class="h-40 w-40 shrink-0 rounded-full border-8 border-brand-500"></div>
    <ul class="min-w-0 max-w-full flex-1 basis-44 space-y-2.5 text-sm">${NAMES.map(row).join("")}</ul>
  </div></section></div>`;
document.getElementById("root").innerHTML = [280, 360, 768, 1280].map(card).join("");
document.querySelectorAll("[data-bg]").forEach((e) => (e.style.background = e.dataset.bg));
document.querySelectorAll(".frame").forEach((f) => (f.style.width = f.getAttribute("style").match(/width:(\d+)px/)[1] + "px"));
// الضغط على الاسم = إظهاره كامل (نفس سلوك الداشبورد على اللمس)
document.addEventListener("click", (e) => {
  const b = e.target.closest("button[aria-expanded]"); if (!b) return;
  const open = b.getAttribute("aria-expanded") !== "true";
  b.setAttribute("aria-expanded", String(open));
  b.classList.toggle("truncate", !open);
  b.classList.toggle("whitespace-normal", open); b.classList.toggle("break-words", open);
  b.style.overflowWrap = open ? "anywhere" : "";
});

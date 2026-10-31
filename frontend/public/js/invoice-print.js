// =====================================================================
//  invoice-print.js — طباعة فاتورة وارد/صادر على A4 (أبيض وأسود).
//  - بيجيب كل بيانات الفاتورة من endpoint واحد (GET /api/stock/{kind}/{id}/print).
//  - التقسيم لصفحات بيتم هنا بقياس ارتفاع كل سطر فعليًا (مش بالاعتماد على CSS بس) عشان "صفحة X من Y"
//    تشتغل في كل المتصفحات، وهيدر الجدول يتكرر، والسطر ما يتقطعش، والإجماليات والتوقيعات تفضل مع بعض في آخر صفحة.
// =====================================================================
import { api, ApiError } from "./api.js";
import { guard } from "./auth.js";
import { API_BASE } from "./config.js";
import { getQuery } from "./utils.js";
import { PRINT_CONFIG } from "./print-config.js";

const kind = getQuery("kind") === "out" ? "out" : "in";
const id = Number(getQuery("id")) || 0;
const T = {
  in: { title: "فاتورة وارد", kindLabel: "وارد", billed: "المورّد", user: "استلمها" },
  out: { title: "فاتورة صادر", kindLabel: "صادر", billed: "فاتورة إلى", user: "سلّمها" },
}[kind];
document.body.dataset.kind = kind; // لون الفاتورة: وارد أخضر / صادر برتقالي
const QR_KEY = "print_show_qr";
const EPS_PX = 6; // هامش أمان صغير عشان فروق التقريب بين القياس والرسم

const $ = (s) => document.querySelector(s);
const esc = (v) => String(v ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const fmtQty = (n) => Number(n).toLocaleString("en-US", { maximumFractionDigits: 3 });
const fmtMoney = (n) => n == null ? "-" : Number(n).toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
const cur = (n) => `<span class="num">${fmtMoney(n)}</span> ج.م`;
// "2026-10-12T14:30:05" -> "12/10/2026 14:30" (توقيت القاهرة زي ما متسجّل - من غير أي تحويل منطقة زمنية)
const fmtDate = (iso) => {
  const m = /^(\d{4})-(\d{2})-(\d{2})[T ](\d{2}):(\d{2})/.exec(iso || "");
  return m ? `${m[3]}/${m[2]}/${m[1]} ${m[4]}:${m[5]}` : "-";
};

let data = null;
let showQr = (() => {
  try { const v = localStorage.getItem(QR_KEY); if (v !== null) return v === "1"; } catch (e) { /* ignore */ }
  return PRINT_CONFIG.showQr;
})();
const qrAvailable = () => typeof window.qrcode === "function";

// ---------- QR ----------
function qrSvg(url) {
  try {
    const qr = window.qrcode(0, "M");
    qr.addData(url);
    qr.make();
    return qr.createSvgTag(4, 0).replace(/\swidth="\d+"/, "").replace(/\sheight="\d+"/, "");
  } catch (e) { return ""; }
}
function invoiceUrl() {
  const base = (data.public_base_url || window.location.origin).replace(/\/$/, "");
  return `${base}/${data.qr_path}`;
}

// ---------- أجزاء الصفحة (HTML) ----------
const COLS = [11, 0, 16, 29, 16, 25, 27]; // mm (0 = الباقي لاسم الصنف)
const colgroup = () => `<colgroup>${COLS.map((w) => `<col${w ? ` style="width:${w}mm"` : ""}>`).join("")}</colgroup>`;
const thead = () => `<tr><th>#</th><th>الصنف</th><th>الوحدة</th><th>المخزن</th><th>الكمية</th><th>سعر الوحدة (ج.م)</th><th>الإجمالي (ج.م)</th></tr>`;

function rowHtml(it) {
  const price = it.unit_price == null ? "-" : fmtMoney(it.unit_price);
  const total = it.line_total == null ? "-" : fmtMoney(it.line_total);
  return `<tr><td class="c">${it.n}</td>
    <td><div class="pn">${esc(it.product_name)}</div>${it.sku ? `<span class="sku" dir="ltr">SKU: ${esc(it.sku)}</span>` : ""}</td>
    <td class="c">${esc(it.unit || "-")}</td><td class="c">${esc(it.warehouse_name || "-")}</td>
    <td class="n">${fmtQty(it.quantity)}</td><td class="n">${price}</td><td class="n tot">${total}</td></tr>`;
}

function brandHtml() {
  const c = data.company;
  const phones = [c.phone1, c.phone2].filter(Boolean).map((p) => `<span class="num">${esc(p)}</span>`).join("");
  const mark = c.has_logo
    ? `<img class="logo" alt="" src="${API_BASE}/api/company/logo?v=${encodeURIComponent(c.logo_version || "")}">`
    : (c.name ? `<div class="mono">${esc([...c.name.trim()][0] || "")}</div>` : "");
  return `<div class="brand">${mark}<div><div class="co-name">${esc(c.name || "")}</div>${phones ? `<div class="co-ph">${phones}</div>` : ""}</div></div>`;
}

function headerFull() {
  const inv = data.invoice;
  const billed = `<div class="billed"><div class="cap">${T.billed}</div><div class="p-name">${esc(inv.party_name || "غير محدد")}</div>
      ${inv.party_phone ? `<div class="p-line"><span class="num">${esc(inv.party_phone)}</span></div>` : ""}
      ${inv.party_address ? `<div class="p-line">${esc(inv.party_address)}</div>` : ""}</div>`;
  return `<div class="top">${brandHtml()}
      <div class="title-box"><div class="title">فاتورة</div></div></div>
    <div class="info">${billed}
      <div class="meta"><div><span>رقم الفاتورة</span><b class="num">${inv.id}</b></div>
        <div><span>التاريخ</span><b class="num">${fmtDate(inv.date)}</b></div>
        <div><span>${T.user}</span><b>${esc(inv.user_name || "-")}</b></div></div></div>
    ${inv.notes ? `<div class="notes"><b>ملاحظات:</b> ${esc(inv.notes)}</div>` : ""}`;
}

function headerCont() {
  return `<div class="hdr-cont"><span>${T.title} رقم <b class="num">${data.invoice.id}</b></span><span>${esc(data.company.name || "")}</span></div>`;
}

function adjLabel(base, a) {
  return a.type === "percent" ? `${base} (<span class="num">${fmtQty(a.value)}%</span>)` : base;
}

function footerHtml() {
  const t = data.totals;
  const hasAdj = !!(t.discount || t.tax);
  const rows = [];
  if (hasAdj) rows.push(`<tr><td>الإجمالي الفرعي</td><td>${cur(t.subtotal)}</td></tr>`);
  if (t.discount) rows.push(`<tr><td>${adjLabel("الخصم", t.discount)}</td><td>${cur(t.discount.amount)}</td></tr>`);
  if (t.tax) rows.push(`<tr><td>${adjLabel("الضريبة", t.tax)}</td><td>${cur(t.tax.amount)}</td></tr>`);
  const grand = `<div class="grand"><span>${hasAdj ? "الإجمالي النهائي" : "إجمالي الفاتورة"}</span><b><span class="num">${fmtMoney(t.grand_total)}</span><small>ج.م</small></b></div>`;
  const qr = showQr && qrAvailable() ? `<div class="qr">${qrSvg(invoiceUrl())}<div>امسح لفتح الفاتورة في النظام</div></div>` : "";
  return `<div class="foot">
    <div class="foot-top">
      <div class="sum-note">عدد الأصناف: <b class="num">${fmtQty(t.items_count)}</b><br>إجمالي الكمية: <b class="num">${fmtQty(t.total_quantity)}</b></div>
      <div class="totals-wrap">${rows.length ? `<table class="totals">${rows.join("")}</table>` : ""}${grand}</div>
    </div>
    <div class="sign-row">
      <div class="sign"><div class="line">توقيع المستلم</div></div>
      <div class="sign"><div class="line">توقيع المسلّم</div></div>
      ${qr}
    </div></div>`;
}

// ---------- التقسيم لصفحات ----------
function measure() {
  const m = $("#measure");
  m.innerHTML = `<div id="m-hf">${headerFull()}</div><div id="m-hc">${headerCont()}</div>
    <table class="items" id="m-tbl">${colgroup()}<thead>${thead()}</thead><tbody>${data.items.map(rowHtml).join("")}</tbody></table>
    <div id="m-ft">${footerHtml()}</div><div class="probe" id="m-probe"></div>`;
  const h = (s) => m.querySelector(s).getBoundingClientRect().height;
  const out = {
    page: h("#m-probe") - EPS_PX,
    first: h("#m-hf"), cont: h("#m-hc"),
    thead: m.querySelector("#m-tbl thead").getBoundingClientRect().height,
    foot: h("#m-ft"),
    rows: [...m.querySelectorAll("#m-tbl tbody tr")].map((tr) => tr.getBoundingClientRect().height),
  };
  m.innerHTML = ""; // ما نسيبش عنصر القياس في الصفحة (كان ممكن يطلّع صفحة فاضية زيادة في الطباعة)
  return out;
}

function paginate(ms) {
  const n = ms.rows.length;
  const room = (pageIdx) => ms.page - (pageIdx === 0 ? ms.first : ms.cont) - ms.thead;
  const pages = [];
  let i = 0;
  while (i < n || pages.length === 0) {
    const cap = room(pages.length);
    const take = [];
    let used = 0;
    while (i < n && (used + ms.rows[i] <= cap || take.length === 0)) { used += ms.rows[i]; take.push(i++); }
    pages.push(take);
  }
  // الإجماليات وخانات التوقيع لازم تتحط مع بعض في آخر صفحة. لو ما دخلوش: بننقل آخر سطرين (أو أقل) لصفحة جديدة معاهم
  const usedOn = (p) => p.reduce((s, r) => s + ms.rows[r], 0);
  let last = pages[pages.length - 1];
  if (usedOn(last) + ms.foot > room(pages.length - 1)) {
    const keep = pages.length === 1 ? Math.max(1, last.length - 2) : Math.max(0, last.length - 2);
    const carry = last.splice(keep);
    if (!last.length && pages.length > 1) pages.pop();
    pages.push(carry);
  }
  return pages;
}

function render() {
  const ms = measure();
  const pages = paginate(ms);
  const total = pages.length;
  $("#sheets").innerHTML = pages.map((rows, p) => `
    <section class="sheet"><div class="sheet-inner">
      ${p === 0 ? headerFull() : headerCont()}
      <table class="items">${colgroup()}<thead>${thead()}</thead><tbody>${rows.map((r) => rowHtml(data.items[r])).join("")}</tbody></table>
      ${p === total - 1 ? footerHtml() : ""}
    </div>
    <div class="pgno"><span>${T.title} <span class="num">#${data.invoice.id}</span></span><span>صفحة <span class="num">${p + 1}</span> من <span class="num">${total}</span></span></div></section>`).join("");
  $("#pagesInfo").textContent = `${data.items.length} صنف · ${total} صفحة`;
  fitToScreen();
  document.body.dataset.ready = "1"; // علامة للاختبارات الآلية
}

// على الموبايل الصفحة (210mm) أعرض من الشاشة: بنصغّرها للمعاينة بس (الطباعة نفسها مش بتتأثر)
function fitToScreen() {
  const sheetPx = 210 * 3.7795;
  const z = Math.min(1, (window.innerWidth - 16) / sheetPx);
  $("#sheets").style.zoom = z < 1 ? String(z) : "";
}

// ---------- التشغيل ----------
function showState(msg, isError = false) {
  const s = $("#state");
  s.hidden = false; s.textContent = msg;
  if (isError) s.style.borderColor = "#000";
}

async function waitLogo() {
  if (!data.company.has_logo) return;
  await new Promise((res) => {
    const img = new Image();
    img.onload = img.onerror = () => res();
    img.src = `${API_BASE}/api/company/logo?v=${encodeURIComponent(data.company.logo_version || "")}`;
    setTimeout(res, 4000);
  });
}

async function main() {
  if (!guard({ perm: ["invoices.print"] })) return;
  $("#backLink").href = id ? `invoice.html#kind=${kind}&id=${id}` : "movements.html";
  if (!id) return showState("الرابط مش فيه رقم فاتورة.", true);

  const wrap = $("#qrWrap"), toggle = $("#qrToggle");
  if (PRINT_CONFIG.allowQrToggle && qrAvailable()) {
    wrap.hidden = false; toggle.checked = showQr;
    toggle.addEventListener("change", () => {
      showQr = toggle.checked;
      try { localStorage.setItem(QR_KEY, showQr ? "1" : "0"); } catch (e) { /* ignore */ }
      if (data) render();
    });
  }

  try {
    data = await api.get(`/api/stock/${kind}/${id}/print`);
  } catch (e) {
    if (e instanceof ApiError && e.status === 401) return; // api.js بيحوّل لصفحة الدخول
    return showState(e.message || "تعذّر تحميل الفاتورة", true);
  }
  document.title = `${T.title} ${data.invoice.id}`;
  await Promise.all([document.fonts ? document.fonts.ready : null, waitLogo()]);
  $("#state").hidden = true;
  $("#sheets").hidden = false;
  render();
  const btn = $("#printBtn");
  btn.disabled = false;
  btn.addEventListener("click", () => window.print());
  window.addEventListener("resize", fitToScreen);
}

main();

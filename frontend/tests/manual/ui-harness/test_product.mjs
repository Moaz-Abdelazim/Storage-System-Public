import { mount } from "./harness.mjs";
import assert from "node:assert/strict";

const product = {
  id: 1, name: "منتج تجريبي", sku: "SKU-1", barcode: "6221", unit: "قطعة", price: 25, min_quantity: 5, quantity: 50, reserved: 5, available: 45, unassigned_quantity: 0,
  category_name: "تصنيف", brand_name: "ماركة", created_at: "2026-01-01T10:00:00", updated_at: "2026-02-01T10:00:00", is_active: true,
  has_image: true, image_url: "/api/products/1/image?size=main&v=abc", thumb_url: "/api/products/1/image?size=thumb&v=abc",
  locations: [
    { location_id: 11, full_code: "W1-F1-R2", warehouse_id: 1, warehouse_name: "مخزن 1", warehouse_code: "W1", quantity: 25, reserved: 5, available: 20 },
    { location_id: 12, full_code: "W1-F2-R1", warehouse_id: 1, warehouse_name: "مخزن 1", warehouse_code: "W1", quantity: 10, reserved: 0, available: 10 },
    { location_id: 21, full_code: "W2-F1-R3", warehouse_id: 2, warehouse_name: "مخزن 2", warehouse_code: "W2", quantity: 15, reserved: 0, available: 15 },
  ],
};
const history = {
  items: [
    { key: "1-9", kind: "out", is_reversal: false, voided: false, date: "2026-02-01T10:00:00", quantity: 3, delta: -3, balance_before: 50, balance_after: 47, location: { code: "W1-F1-R2" }, from: null, to: null, document_id: 7, party_name: "عميل", user_name: "admin", notes: "ملاحظة", status: "completed" },
    { key: "2-4", kind: "transfer", is_reversal: false, voided: false, date: "2026-01-20T10:00:00", quantity: 4, delta: 0, balance_before: 50, balance_after: 50, location: null, from: { code: "W1-F1-R2" }, to: { code: "W1-F2-R1" }, document_id: null, user_name: "admin", status: "completed" },
    { key: "4-3", kind: "in", is_reversal: true, voided: true, date: "2026-01-10T10:00:00", quantity: 5, delta: -5, balance_before: 55, balance_after: 50, location: { code: "W2-F1-R3" }, document_id: 3, void_reason: "خطأ", status: "voided" },
  ],
  total: 3, page: 1, total_pages: 1, page_size: 15,
  summary: { in_quantity: 0, out_quantity: 3, transfers_quantity: 4, adjustments_net: 0, net_change: -3 },
  current_quantity: 50, opening_balance: 12, balances_available: true,
};
const routes = [[/GET \/api\/products\/1$/, product], [/GET \/api\/products\/1\/history/, history], [/GET \/api\/locations\/warehouses/, [{ id: 1, code: "W1", name: "مخزن 1" }]]];
const { doc, calls, w, wait } = await mount("product.html", "product.js", routes);
const text = () => doc.body.textContent.replace(/\s+/g, " ");

// بيانات المنتج
assert.match(text(), /منتج تجريبي/); assert.match(text(), /SKU-1/); assert.match(text(), /تصنيف/); assert.match(text(), /ماركة/);
assert.match(text(), /تاريخ الإنشاء/);
// الصورة الأساسية
const img = doc.querySelector('img[alt="منتج تجريبي"]');
assert.ok(img, "main image rendered"); assert.equal(img.getAttribute("src"), "/api/products/1/image?size=main&v=abc");
// توزيع المخزون: 3 صفوف + إجمالي، واسم المخزن بيظهر مرة واحدة لكل مجموعة
const distRows = [...doc.querySelectorAll("section")].find((s) => /توزيع المخزون/.test(s.textContent)).querySelectorAll("tbody tr");
assert.equal(distRows.length, 4 /* 3 مواقع + الإجمالي (صف الكمية بدون مكان مخفي) */ + 1);
const firstCells = [...distRows].slice(0, 3).map((r) => r.children[0].textContent.trim());
assert.deepEqual([...distRows].slice(0, 3).map((r) => r.querySelector("loc-code").getAttribute("code")), ["W1-F1-R2", "W1-F2-R1", "W2-F1-R3"]);
assert.deepEqual(firstCells, ["مخزن 1 (W1)", "", "مخزن 2 (W2)"]);
assert.match([...distRows].at(-1).textContent, /الإجمالي/); assert.match([...distRows].at(-1).textContent, /50/);
assert.equal(doc.querySelector('[class*="text-red-600"][x-show="distributionMismatch"]').style.display, "none"); // مفيش تحذير عدم تطابق
// سجل الحركة
const hrows = [...doc.querySelectorAll("section")].find((s) => /سجل الحركة/.test(s.querySelector("h2")?.textContent || "")).querySelectorAll("tbody tr");
assert.equal(hrows.length, 3);
assert.match(hrows[0].textContent, /صادر/); assert.match(hrows[0].textContent, /−3/); assert.match(hrows[0].textContent, /50/); assert.match(hrows[0].textContent, /47/); assert.match(hrows[0].textContent, /#7/);
assert.match(hrows[1].textContent, /نقل/);
const codes = (row) => [...row.querySelectorAll("loc-code")].filter((e) => e.closest("[style*='display: none']") === null).map((e) => e.getAttribute("code")).filter(Boolean);
assert.deepEqual(codes(hrows[1]), ["W1-F1-R2", "W1-F2-R1"]);   // من ← إلى
assert.deepEqual(codes(hrows[0]), ["W1-F1-R2"]); assert.deepEqual(codes(hrows[2]), ["W2-F1-R3"]);
assert.match(hrows[2].textContent, /إلغاء وارد/); assert.match(hrows[2].textContent, /سبب الإلغاء: خطأ/);
assert.match(text(), /رصيد قبل أول حركة مسجّلة/);
// أول طلب history بالافتراضيات
const hcall = calls.find((c) => /history/.test(c.path)); assert.match(hcall.path, /order=desc/); assert.match(hcall.path, /page=1/); assert.match(hcall.path, /page_size=15/);
// الفلاتر بتتبعت مع بعض
const sel = doc.querySelector('select[aria-label="نوع الحركة"]'); sel.value = "out"; sel.dispatchEvent(new w.Event("change", { bubbles: true }));
await wait();
const last = calls.filter((c) => /history/.test(c.path)).at(-1).path;
assert.match(last, /kind=out/); assert.match(last, /page=1/);
const cb = doc.querySelector('input[type="checkbox"]'); cb.checked = true; cb.dispatchEvent(new w.Event("change", { bubbles: true }));
await wait();
assert.match(calls.filter((c) => /history/.test(c.path)).at(-1).path, /include_voided=1.*|kind=out/);
assert.match(calls.filter((c) => /history/.test(c.path)).at(-1).path, /include_voided=1/);
console.log("product page OK; history calls:", calls.filter((c) => /history/.test(c.path)).length);

// صلاحيات: مستخدم بدون أي صلاحية تاريخ + بدون products.edit
const m2 = await mount("product.html", "product.js", routes, { admin: false, perms: ["products.view"] });
const t2 = m2.doc.body.textContent.replace(/\s+/g, " ");
assert.ok(!m2.calls.some((c) => /history/.test(c.path)), "no history request without permission");
assert.equal([...m2.doc.querySelectorAll("section")].find((s) => /سجل الحركة/.test(s.textContent))?.style.display, "none");
assert.equal(m2.doc.querySelector('input[type="file"]').closest("div").style.display, "none"); // أزرار الصورة مخفية
console.log("permissions OK");
process.exit(0);

import { mount } from "./harness.mjs";
import assert from "node:assert/strict";

let batch = {
  token: "t1", total: 4, counts: { matched: 1, unmatched: 1, ambiguous: 1, duplicate: 0, invalid: 1 },
  items: [
    { id: 1, filename: "A1.png", status: "matched", error: null, how: "sku", product: { id: 5, name: "منتج أ", sku: "A1", barcode: null, has_image: false }, candidates: [], thumb_url: "/api/product-images/batches/t1/items/1/thumb" },
    { id: 2, filename: "random.png", status: "unmatched", error: null, product: null, candidates: [], thumb_url: "/api/product-images/batches/t1/items/2/thumb" },
    { id: 3, filename: "X1.png", status: "ambiguous", error: null, product: null, candidates: [{ id: 7, name: "مرشح 1", sku: "X1", barcode: null }, { id: 8, name: "مرشح 2", sku: null, barcode: "x1" }], thumb_url: "/api/product-images/batches/t1/items/3/thumb" },
    { id: 4, filename: "bad.png", status: "invalid", error: "الصورة تالفة أو مش صورة صالحة", product: null, candidates: [], thumb_url: null },
  ],
};
const routes = [
  [/POST \/api\/product-images\/batches$/, () => batch],
  [/PUT \/api\/product-images\/batches\/t1\/items\/2/, (p, o) => {
    const pid = JSON.parse(o.body).product_id;
    batch = { ...batch, counts: { ...batch.counts, matched: 2, unmatched: 0 }, items: batch.items.map((i) => i.id === 2 ? { ...i, status: "matched", product: { id: pid, name: "اخترته", sku: "Z", barcode: null, has_image: true } } : i) };
    return batch;
  }],
  [/GET \/api\/products\/lookup/, [{ id: 9, name: "نتيجة بحث", sku: "S9", barcode: null }]],
  [/POST \/api\/product-images\/batches\/t1\/apply/, { applied: 2, skipped_existing: 0, failed: [], unmatched_discarded: 1, invalid: 1 }],
];
const { doc, calls, w, wait } = await mount("product-images.html", "product-images.js", routes);
w.confirm = () => true;
const vis = (el) => el && el.closest("[style*='display: none']") === null;

// رفع
const fi = doc.querySelector('input[type="file"][accept*=".zip"]');
Object.defineProperty(fi, "files", { value: [new w.File([new Uint8Array(10)], "images.zip")], configurable: true });
fi.dispatchEvent(new w.Event("change", { bubbles: true }));
await wait(500);
const post = calls.find((c) => c.method === "POST");
assert.ok(post && post.body instanceof w.FormData && post.body.getAll("files").length === 1, "FormData with files sent");
// المراجعة: بتفتح على "محتاجة اختيار" (2 صور)، والتابات بأرقامها
let rows = [...doc.querySelectorAll("tbody tr")].filter(vis);
assert.deepEqual(rows.map((r) => r.children[1].textContent.trim()), ["random.png", "X1.png"]);
assert.match(doc.body.textContent, /تطبيق\s*1\s*صورة/);
// المبهم: أزرار المرشحين
assert.match(rows[1].textContent, /مرشح 1/); assert.match(rows[1].textContent, /مرشح 2/);
// الصور المصغّرة
assert.equal(rows[0].querySelector("img").getAttribute("src"), "/api/product-images/batches/t1/items/2/thumb");
// اختيار يدوي من نافذة البحث
[...rows[0].querySelectorAll("button")].find((b) => /اختيار منتج/.test(b.textContent)).click();
await wait();
const q = doc.querySelector('input[aria-label="بحث عن منتج"]'); q.value = "نتيجة"; q.dispatchEvent(new w.Event("input", { bubbles: true }));
await wait(600);
assert.ok(calls.some((c) => /products\/lookup\?q=/.test(c.path)));
[...doc.querySelectorAll(".modal button")].find((b) => /نتيجة بحث/.test(b.textContent)).click();
await wait();
const put = calls.find((c) => c.method === "PUT"); assert.equal(JSON.parse(put.body).product_id, 9);
assert.match(doc.body.textContent, /تطبيق\s*2\s*صورة/);
// فلتر التابات: الصور اللي فيها مشاكل
[...doc.querySelectorAll(".seg-btn")].find((b) => /فيها مشاكل/.test(b.textContent)).click();
await wait();
rows = [...doc.querySelectorAll("tbody tr")].filter(vis);
assert.equal(rows.length, 1); assert.match(rows[0].textContent, /تالفة/); assert.equal([...rows[0].querySelectorAll("button")].filter(vis).length, 0);
// تطبيق مع استبدال
doc.querySelector('input[type="checkbox"]').click();
[...doc.querySelectorAll("button")].find((b) => /^\s*تطبيق/.test(b.textContent)).click();
await wait(500);
const ap = calls.find((c) => /apply/.test(c.path)); assert.deepEqual(JSON.parse(ap.body), { replace_existing: true });
assert.match(doc.body.textContent, /تم تطبيق الدفعة/);
console.log("batch page OK");

// ---- جدول المنتجات ----
const products = { items: [
  { id: 1, name: "بصورة", sku: "S1", barcode: null, unit: "قطعة", price: 5, quantity: 10, reserved: 0, available: 10, min_quantity: 1, unassigned_quantity: 0, is_active: true, thumb_url: "/api/products/1/image?size=thumb&v=k", locations: [{ location_id: 1, full_code: "W1-A", quantity: 10, reserved: 0, available: 10 }] },
  { id: 2, name: "بدون", sku: "S2", barcode: null, unit: "قطعة", price: 5, quantity: 0, reserved: 0, available: 0, min_quantity: 1, unassigned_quantity: 0, is_active: true, thumb_url: null, locations: [] },
], total: 2, page: 1, total_pages: 1 };
const m = await mount("products.html", "products.js", [[/GET \/api\/products\?/, products], [/GET \/api\/locations/, []]]);
const prow = [...m.doc.querySelectorAll("tbody tr")].filter((r) => r.querySelector("a"));
assert.equal(prow.length, 2);
assert.equal(prow[0].querySelector("img").getAttribute("src"), "/api/products/1/image?size=thumb&v=k");
assert.equal(prow[1].querySelector("img"), null);                                         // منتج من غير صورة: أيقونة
assert.equal(prow[0].querySelector("a").getAttribute("href"), "product.html#id=1");
console.log("products page OK");
process.exit(0);

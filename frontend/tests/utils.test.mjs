// node --test frontend/tests  — اختبارات الدوال الخالصة في utils.js (من غير متصفح)
import test from "node:test";
import assert from "node:assert/strict";
import { safeNext, newIdempotencyKey, docStatus, num, money } from "../public/js/utils.js";

test("safeNext accepts simple internal pages", () => {
  for (const ok of ["index.html", "stock-out.html", "invoice.html#kind=out&id=5", "movements.html?kind=out", "products.html?q=a%20b#x=1"]) {
    assert.equal(safeNext(ok), ok);
  }
});

test("safeNext rejects open-redirect and script payloads", () => {
  const bad = [
    "//evil.com", "///evil.com/x.html", "https://evil.com/a.html", "http://evil.com", "javascript:alert(1)", "JaVaScRiPt:alert(1).html",
    "/index.html", "\\evil.com", "..\\x.html", "../index.html", "a/b.html", "index.html/../../x", "index.html\\evil", "evil.com", "data:text/html,<script>",
    "index.html#<script>", "index.html?a=<b>", " //evil.com", "x.html\nSet-Cookie:a=b", "x.html%0d%0a", "", null, undefined, 5, {}, "a".repeat(400) + ".html",
  ];
  for (const b of bad) assert.equal(safeNext(b), "index.html", String(b));
  assert.equal(safeNext("//evil.com", "login.html"), "login.html");
});

test("idempotency keys are valid unique uuids", () => {
  const a = newIdempotencyKey(), b = newIdempotencyKey();
  assert.match(a, /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/);
  assert.notEqual(a, b);
  const saved = Object.getOwnPropertyDescriptor(globalThis, "crypto");
  Object.defineProperty(globalThis, "crypto", { value: undefined, configurable: true });
  try { assert.match(newIdempotencyKey(), /^[0-9a-f-]{36}$/); } finally { if (saved) Object.defineProperty(globalThis, "crypto", saved); }
});

test("document status labels", () => {
  assert.deepEqual(["quote", "reserved", "completed", "voided"].map((s) => docStatus(s).label), ["عرض سعر", "محجوزة", "تمت", "ملغاة"]);
  assert.equal(docStatus("whatever").label, "تمت");
});

test("number helpers", () => {
  assert.equal(num(1234.5678), "1,234.568");
  assert.equal(money(null), "—");
});

import { ellipsize, wrapLines } from "../public/js/utils.js";

test("ellipsize keeps short names and cuts long ones by real characters", () => {
  assert.equal(ellipsize("مخزن", 10), "مخزن");
  const long = "مخزن الفرع الرئيسي لمنطقة وسط البلد والدور الثالث بالعقار رقم 15";
  const out = ellipsize(long, 16);
  assert.ok([...out].length <= 16 && out.endsWith("…"));
  assert.equal(ellipsize("😀".repeat(20), 5), "😀😀😀😀…");  // مفيش قطع في نص الإيموجي
  assert.equal(ellipsize(null), "");
  assert.equal(ellipsize("abc", 1), "…");
});

test("wrapLines wraps on words and splits over-long words", () => {
  const lines = wrapLines("مخزن الفرع الرئيسي لمنطقة وسط البلد والدور الثالث", 16);
  assert.ok(lines.length > 1 && lines.every((l) => [...l].length <= 16));
  assert.equal(lines.join(" "), "مخزن الفرع الرئيسي لمنطقة وسط البلد والدور الثالث");
  const word = wrapLines("ا".repeat(70), 28);
  assert.deepEqual(word.map((l) => [...l].length), [28, 28, 14]);
  assert.deepEqual(wrapLines("", 10), [""]);
  assert.deepEqual(wrapLines("قصير", 28), ["قصير"]);
});

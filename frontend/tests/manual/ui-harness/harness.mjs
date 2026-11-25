// اختبار واجهة بدون متصفح: بيشغّل الصفحة في jsdom مع Alpine الحقيقي وردود API مزيّفة.
// التشغيل (من أي فولدر فيه jsdom و esbuild منصّبين):  npm i jsdom esbuild && node test_product.mjs && node test_batch.mjs
import { JSDOM } from "jsdom";
import { buildSync } from "esbuild";
import fs from "node:fs";
const FE = new URL("../../../public", import.meta.url).pathname;

export async function mount(page, jsFile, routes, { perms = null, admin = true } = {}) {
  const html = fs.readFileSync(`${FE}/${page}`, "utf-8").replace(/<script[\s\S]*?<\/script>/g, "").replace(/<link[^>]*>/g, "");
  const dom = new JSDOM(html, { url: `http://localhost/${page}#id=1`, pretendToBeVisual: true, runScripts: "outside-only" });
  const w = dom.window;
  w.localStorage.setItem("is_admin", admin ? "1" : "0");
  w.localStorage.setItem("perms", JSON.stringify(perms || []));
  const calls = [];
  w.fetch = async (url, opts = {}) => {
    const path = String(url);
    calls.push({ path, method: opts.method || "GET", body: opts.body });
    for (const [pat, handler] of routes) {
      if (pat.test(`${opts.method || "GET"} ${path}`)) {
        const out = typeof handler === "function" ? handler(path, opts) : handler;
        return { ok: true, status: 200, headers: { get: (k) => (/content-type/i.test(k) ? "application/json" : null) }, json: async () => out };
      }
    }
    return { ok: false, status: 404, headers: { get: () => "application/json" }, json: async () => ({ detail: "not mocked: " + path }) };
  };
  w.createImageBitmap = undefined;
  w.addEventListener("app-confirm", (e) => e.detail.resolve(true));   // نافذة التأكيد بتاعة الـ shell: بنوافق تلقائيًا
  const bundle = buildSync({ entryPoints: [`${FE}/js/${jsFile}`], bundle: true, write: false, format: "iife", platform: "browser" }).outputFiles[0].text;
  w.eval(bundle);
  w.eval(fs.readFileSync(`${FE}/vendor/alpine.min.js`, "utf-8"));
  await new Promise((r) => setTimeout(r, 400));
  return { w, doc: w.document, calls, wait: (ms = 250) => new Promise((r) => setTimeout(r, ms)) };
}

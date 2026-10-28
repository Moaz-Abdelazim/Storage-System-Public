// بينسخ المكتبات من node_modules لمجلد public/ عشان الموقع يشتغل 100% من غير أي CDN.
import { cpSync, mkdirSync, copyFileSync, existsSync } from "node:fs";
import { join } from "node:path";

const nm = (p) => join("node_modules", p);
const out = (p) => join("public", p);
const mk = (p) => mkdirSync(p, { recursive: true });

mk(out("vendor")); mk(out("fonts")); mk(out("vendor/fontawesome/css")); mk(out("vendor/fontawesome/webfonts"));

copyFileSync(nm("alpinejs/dist/cdn.min.js"), out("vendor/alpine.min.js"));
copyFileSync(nm("chart.js/dist/chart.umd.js"), out("vendor/chart.umd.js"));

// Font Awesome (solid + regular بس)
copyFileSync(nm("@fortawesome/fontawesome-free/css/all.min.css"), out("vendor/fontawesome/css/all.min.css"));
for (const f of ["fa-solid-900.woff2", "fa-regular-400.woff2"]) {
  copyFileSync(nm(`@fortawesome/fontawesome-free/webfonts/${f}`), out(`vendor/fontawesome/webfonts/${f}`));
}

// خط Tajawal (عربي + لاتيني)
for (const w of [400, 500, 700]) {
  for (const s of ["arabic", "latin"]) {
    const f = `tajawal-${s}-${w}-normal.woff2`;
    const src = nm(`@fontsource/tajawal/files/${f}`);
    if (existsSync(src)) copyFileSync(src, out(`fonts/${f}`));
  }
}
console.log("vendor files copied ✔");

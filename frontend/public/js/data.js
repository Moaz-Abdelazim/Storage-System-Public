// =====================================================================
//  data.js — مصادر القوايم المنسدلة (<ui-select source="...">).
//  كل المصادر الكبيرة (منتجات، موردين، مواقع...) بتبحث على السيرفر بحد أقصى للنتايج (مفيش تحميل للكل).
//  بترجّع options بالشكل: { value, label, sub?, data? }
// =====================================================================
import { api, qs } from "./api.js";
import { num } from "./utils.js";

const LIMIT = 20;
const TTL = 30_000;
const cache = new Map();

export function invalidateData() { cache.clear(); api.invalidate(); }

function cached(key, loader) {
  const hit = cache.get(key);
  if (hit && Date.now() - hit.t < TTL) return hit.p;
  const p = loader().catch((e) => { cache.delete(key); throw e; });
  cache.set(key, { t: Date.now(), p });
  return p;
}

// علامة "في نتايج أكتر" عشان القايمة تعرض "اكتب للبحث عن المزيد"
const withMore = (opts, more) => { opts.truncated = !!more; return opts; };

// ---------- مصادر بحث (remote) ----------
export async function searchProducts(q) {
  const rows = await api.get(`/api/products/lookup${qs({ q, limit: LIMIT })}`);
  return withMore(rows.map((p) => ({
    value: p.id, label: p.name, sub: `${p.barcode || p.sku ? (p.barcode || p.sku) + " · " : ""}${num(p.quantity)} ${p.unit || ""}`, data: p,
  })), rows.length >= LIMIT);
}

const searchParty = (path) => async (q) => {
  const rows = await api.get(`${path}/lookup${qs({ q, limit: LIMIT })}`);
  return withMore(rows.map((s) => ({ value: s.id, label: s.name, sub: s.phone || "" })), rows.length >= LIMIT);
};

const searchNamed = (path) => async (q) => {
  const r = await api.get(`${path}/stats${qs({ search: q, page: 1, page_size: LIMIT })}`);
  return withMore(r.items.map((c) => ({ value: c.id, label: c.name })), r.total > r.items.length);
};

const locOption = (n, valuePrefix) => ({
  value: valuePrefix ? `${n.kind === "warehouse" ? "w" : "l"}:${n.id}` : n.id,
  label: n.full_code,
  sub: `${(n.path || []).join(" › ")}${n.quantity ? ` · ${num(n.quantity)} قطعة` : ""}`,
  data: n,
});

async function searchLocations(q, params, valuePrefix) {
  const r = await api.get(`/api/locations/nav/search${qs({ q, limit: 30, ...params })}`);
  return withMore(r.items.map((n) => locOption(n, valuePrefix)), r.truncated);
}

export const REMOTE_SOURCES = {
  products: searchProducts,
  suppliers: searchParty("/api/suppliers"),
  recipients: searchParty("/api/recipients"),
  categories: searchNamed("/api/categories"),
  brands: searchNamed("/api/brands"),
  "leaf-locations": (q) => searchLocations(q, { leaf_only: 1 }, false),
  "all-nodes": (q) => searchLocations(q, { include_warehouses: 1 }, true),
  receiving: (q) => searchLocations(q, { mode: "receiving" }, true),
};

// ---------- مصادر صغيرة (بتتحمّل مرة وتتفلتر محليًا) ----------
const LOCAL_SOURCES = {
  usernames: async () => (await cached("usernames", () => api.get("/api/auth/usernames"))).map((u) => ({ value: u, label: u })),
  warehouses: async () => (await cached("warehouses", () => api.get("/api/locations/warehouses"))).map((w) => ({ value: w.id, label: `${w.code} — ${w.name}`, data: w })),
};

export function loadSource(name) {
  const fn = LOCAL_SOURCES[name];
  if (!fn) return Promise.reject(new Error(`مصدر غير معروف: ${name}`));
  return fn();
}

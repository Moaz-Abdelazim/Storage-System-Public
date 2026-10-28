import { registerParties } from "./parties.js";
registerParties("recipients", { base: "/api/recipients", productsPath: "/api/reports/recipient-products", invoicesPath: "/api/reports/recipient-product-invoices", noun: "الجهة", deleteHint: "لو مرتبطة بفواتير صادر سابقة مش هتتحذف." });

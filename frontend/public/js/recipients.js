import { registerParties } from "./parties.js";
registerParties("recipients", { base: "/api/recipients", productsPath: "/api/reports/recipient-products", invoicesPath: "/api/reports/recipient-product-invoices", managePerm: "recipients.manage", deletePerm: "recipients.delete", noun: "الجهة", deleteHint: "لو مرتبطة بفواتير صادر سابقة مش هتتحذف." });

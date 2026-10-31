import { registerParties } from "./parties.js";
registerParties("suppliers", { base: "/api/suppliers", productsPath: "/api/reports/supplier-products", invoicesPath: "/api/reports/supplier-product-invoices", noun: "المورد", managePerm: "suppliers.manage", deletePerm: "suppliers.delete", deleteHint: "لو مرتبط بفواتير وارد سابقة مش هيتحذف." });

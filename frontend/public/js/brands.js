import { registerCrud } from "./simple-crud.js";
registerCrud("brands", { base: "/api/brands", noun: "ماركة", managePerm: "catalog.manage", deletePerm: "catalog.delete", filterParam: "brand_id" });

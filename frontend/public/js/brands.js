import { registerCrud } from "./simple-crud.js";
registerCrud("brands", { base: "/api/brands", noun: "ماركة", filterParam: "brand_id" });

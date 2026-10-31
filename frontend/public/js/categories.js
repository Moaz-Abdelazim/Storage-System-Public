import { registerCrud } from "./simple-crud.js";
registerCrud("categories", { base: "/api/categories", noun: "تصنيف", managePerm: "catalog.manage", deletePerm: "catalog.delete", filterParam: "category_id" });

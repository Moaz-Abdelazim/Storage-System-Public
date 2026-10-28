import { registerCrud } from "./simple-crud.js";
registerCrud("categories", { base: "/api/categories", noun: "تصنيف", filterParam: "category_id" });

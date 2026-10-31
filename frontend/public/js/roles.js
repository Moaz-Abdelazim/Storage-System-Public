// roles.js — إدارة الأدوار والصلاحيات (مدير النظام فقط). السيرفر هو اللي بيفرض كل القواعد (الواجهة تلميحات).
import { api } from "./api.js";
import { toast, errMsg, confirmAction } from "./utils.js";

document.addEventListener("alpine:init", () => {
  window.Alpine.data("roles", () => ({
    rows: [], groups: [], meta: {}, loading: true, error: "",
    form: { open: false, id: null, name: "", description: "", perms: new Set(), saving: false, error: "" },

    async init() {
      try {
        const cat = await api.get("/api/roles/permissions");
        this.groups = cat.groups;
        this.meta = Object.fromEntries(cat.groups.flatMap((g) => g.permissions.map((p) => [p.key, p])));
      } catch (e) { this.error = errMsg(e); }
      this.load();
    },

    labelOf(key) { return this.meta[key] ? this.meta[key].label : key; },

    async load() {
      this.loading = true; this.error = "";
      try { this.rows = await api.get("/api/roles"); } catch (e) { this.error = errMsg(e); this.rows = []; }
      this.loading = false;
    },

    openForm(r) {
      this.form = { open: true, id: r ? r.id : null, name: r ? r.name : "", description: r ? r.description || "" : "", perms: new Set(r ? r.permissions : []), saving: false, error: "" };
    },

    // اعتماديات: تحديد صلاحية بيضيف اللي بتتطلبه، وإلغاءها بيلغي اللي بتعتمد عليها
    _closure(key, set) { set.add(key); for (const r of (this.meta[key]?.requires || [])) if (!set.has(r)) this._closure(r, set); },
    _dependents(key) { return Object.keys(this.meta).filter((k) => k !== key && (this.meta[k].requires || []).includes(key)); },
    _drop(key, set) { set.delete(key); for (const d of this._dependents(key)) if (set.has(d)) this._drop(d, set); },
    toggle(key, on) {
      const next = new Set(this.form.perms);
      if (on) this._closure(key, next); else this._drop(key, next);
      this.form.perms = next;
    },
    groupAll(g) { return g.permissions.every((p) => this.form.perms.has(p.key)); },
    toggleGroup(g) {
      const next = new Set(this.form.perms);
      if (this.groupAll(g)) g.permissions.forEach((p) => this._drop(p.key, next)); else g.permissions.forEach((p) => this._closure(p.key, next));
      this.form.perms = next;
    },

    async save() {
      const f = this.form;
      if (f.name.length < 2) { f.error = "اسم الدور لازم يكون حرفين على الأقل"; return; }
      f.saving = true; f.error = "";
      try {
        const body = { name: f.name, description: f.description || null, permissions: [...f.perms] };
        if (f.id) await api.put(`/api/roles/${f.id}`, body); else await api.post("/api/roles", body);
        toast(f.id ? "تم تعديل الدور" : "تمت إضافة الدور");
        f.open = false; this.load();
      } catch (e) { f.error = errMsg(e); }
      f.saving = false;
    },

    async toggleActive(r) {
      const stop = r.is_active;
      const ok = await confirmAction({
        title: stop ? "إيقاف الدور؟" : "تفعيل الدور؟", danger: stop, confirmText: stop ? "إيقاف" : "تفعيل",
        message: stop ? `مستخدمو "${r.name}" (${r.users_count}) مش هيقدروا يدخلوا، وجلساتهم المفتوحة هتتقفل فورًا.` : `مستخدمو "${r.name}" هيقدروا يدخلوا تاني.`,
      });
      if (!ok) return;
      try { await api.patch(`/api/roles/${r.id}/active`, { is_active: !stop }); toast(stop ? "تم الإيقاف" : "تم التفعيل"); this.load(); }
      catch (e) { toast(errMsg(e), "error"); }
    },

    async remove(r) {
      const ok = await confirmAction({ title: "حذف الدور؟", danger: true, confirmText: "حذف", message: `"${r.name}" هيتحذف نهائيًا.` });
      if (!ok) return;
      try { await api.del(`/api/roles/${r.id}`); toast("تم الحذف"); this.load(); }
      catch (e) { toast(errMsg(e), "error"); }
    },
  }));
});

/** The flow files: switch (asking about unsaved changes), open a pasted run link, create from
 * blank, a template or a copy, delete and move. */
import { useEffect } from "react";
import { api, errorText } from "../api";
import { locLabel } from "../LibraryManager";
import { parseRunLink } from "../runLink";
import { FLOW_NAME, locationField } from "./shared";
import type { EditorCtx } from "./shared";

export function useFlowFiles(c: Pick<EditorCtx, "catalog" | "current" | "dialog" | "dirty" | "doc" | "flash" | "flows" | "refreshFlows" | "setCurrent" | "setDoc" | "setRight" | "setRunReq" | "setView" | "t" | "templates">) {
  const { catalog, current, dialog, dirty, doc, flash, flows, refreshFlows, setCurrent, setDoc, setRight, setRunReq, setView, t, templates } = c;
  // ---- flow management
  const confirmDiscard = async () => !dirty || dialog.confirm({
    title: t("app.discardTitle"), message: t("app.discard"), ok: t("app.discardOk"), danger: true });
  const switchFlow = async (name: string) => { if (await confirmDiscard()) setCurrent(name); };
  // a run link pasted in this tab: open that flow and run
  useEffect(() => {
    const go = async () => {
      const link = parseRunLink(location.hash);
      if (!link.flow || !link.run || !flows.some((f) => f.name === link.flow)) return;
      if (link.flow !== current && !(await confirmDiscard())) return;
      setCurrent(link.flow); setView("flow"); setRight("run");
      setRunReq({ mode: "full", open: link.run, flow: link.flow, step: link.step, nonce: Date.now() });
    };
    window.addEventListener("hashchange", go);
    return () => window.removeEventListener("hashchange", go);
  });
  /** A flow name not used yet: base, base-2, base-3… */
  const freeName = (base: string) => {
    let n = base, i = 2;
    while (flows.some((f) => f.name === n)) n = `${base}-${i++}`;
    return n;
  };
  const createFlow = async (copyFrom?: string, copyTitle?: string) => {
    if (!(await confirmDiscard())) return;
    const r = await dialog.form({
      title: copyFrom ? t("app.duplicateTitle") : t("app.newFlowTitle"),
      message: copyFrom ? t("app.newFlowCopy", { name: copyFrom }) : undefined,
      ok: copyFrom ? t("app.create") : t("app.next"),
      fields: [
        ...(copyFrom ? [] : [{ key: "from", label: t("app.startFrom"), value: "",
          options: [{ value: "", label: t("app.blankFlow") },
            ...templates.map((x) => ({ value: x.id, label: t("app.templateOption", { title: x.title }) }))] }]),
        { key: "name", label: t("app.flowName"), hint: t("app.flowNameHint"), placeholder: "my-flow",
          ...(copyFrom ? { value: freeName(`${copyFrom}-copy`) } : {}),
          validate: (v) => !FLOW_NAME.test(v.trim()) ? t("app.flowNameHint")
            : flows.some((f) => f.name === v.trim()) ? t("app.flowExists") : null },
        { key: "title", label: t("app.flowTitle"),
          value: copyFrom ? t("app.copySuffix", { title: copyTitle ?? (copyFrom === current ? doc?.spec.title : undefined) ?? copyFrom }) : "" },
        ...locationField(catalog?.locations, t),
      ],
    });
    if (!r) return;
    const { name } = r;
    const tmpl = templates.find((x) => x.id === r.from);
    let params: Record<string, string> | undefined;
    if (tmpl && Object.keys(tmpl.params).length) {
      const p = await dialog.form({
        title: t("app.templateParams", { title: tmpl.title }), message: tmpl.description, ok: t("app.create"),
        fields: Object.entries(tmpl.params).map(([key, prm]) => {
          const conns = Object.entries(catalog?.connections ?? {}).filter(([, c]) =>
            (!prm.connector_types.length || prm.connector_types.includes(c.type))
            && (!prm.connector_access || prm.connector_access === "read" || c.access === "write"));
          const useSelect = prm.kind === "connector" && conns.length > 0;
          return {
            key, label: prm.required ? prm.label : `${prm.label} (${t("app.optional")})`,
            value: prm.default ?? (useSelect ? conns[0][0] : ""),
            hint: prm.kind === "connector" && !conns.length ? `${prm.hint} ${t("app.noMatchingConnector")}` : prm.hint,
            placeholder: prm.kind === "path" ? "D:\\repos\\app" : undefined,
            options: useSelect ? conns.map(([n, c]) => ({ value: n, label: `${n} · ${c.type}` })) : undefined,
            validate: (v: string) => (prm.required && !v.trim() ? t("app.required") : null),
          };
        }),
      });
      if (!p) return;
      params = p;
    }
    try {
      await api.create(name, r.title || name, copyFrom ? { copy_from: copyFrom } : tmpl ? { template: tmpl.id, params } : undefined,
        r.location || "local");
      await refreshFlows(); setCurrent(name); flash(t("app.created", { name }));
    }
    catch (e) { flash(errorText(e, t), true); }
  };
  const deleteFlow = async () => {
    if (!current || !(await dialog.confirm({ title: t("app.deleteTitle"), message: t("app.confirmDelete", { name: current }),
      ok: t("app.delete"), danger: true }))) return;
    try {
      const r = await api.remove(current);
      flash(t("app.trashed", { path: r.trashed }));
      setCurrent(null); setDoc(null);
      await refreshFlows();
    } catch (e) { flash(String(e), true); }
  };

  const moveFlow = async () => {
    const f = flows.find((x) => x.name === current);
    if (!f || !current) return;
    if (dirty && !(await confirmDiscard())) return;
    const r = await dialog.form({ title: t("loc.moveFlowTitle", { name: current }), message: t("loc.moveFlowMessage"),
      ok: t("loc.move"), fields: locationField(catalog?.locations, t, f.location ?? "local") });
    if (!r || !r.location || r.location === f.location) return;
    try {
      await api.moveFlow(current, r.location);
      await refreshFlows();
      flash(t("loc.moved", { name: current, place: locLabel(t, r.location) }));
    } catch (e) { flash(errorText(e, t), true); }
  };
  return { confirmDiscard, switchFlow, createFlow, deleteFlow, moveFlow };
}

/** The agent library in a flow: insert a copy (with its origin), update from the library,
 * detach, save to the library, the state of a copy. */
import { api } from "../api";
import { clone, inputKind, nodeNames, SLUG, uniqueKey } from "../model";
import type { AgentRaw, Doc, LibraryAgent } from "../types";
import { locationField } from "./shared";
import type { useNodeActions } from "./nodeActions";
import type { EditorCtx } from "./shared";

export function useLibraryActions(c: Pick<EditorCtx, "catalog" | "dialog" | "doc" | "flash" | "library" | "rf" | "setLibrary" | "t" | "update" | "val"> & Pick<ReturnType<typeof useNodeActions>, "showProps">) {
  const { catalog, dialog, doc, flash, library, rf, setLibrary, showProps, t, update, val } = c;
  // ---- agent library: copies with an origin (the flow never changes when the library does)
  const promptOf = (d: Doc, a: AgentRaw) => (a.prompt_file ? d.prompts[a.prompt_file] ?? "" : a.prompt ?? "");
  /** The library agent as a flow agent: its prompt in prompts/<id>.md (or `path`), with an origin. */
  const fromLibrary = (d: Doc, l: LibraryAgent, id: string, path?: string | null): AgentRaw => {
    const a = clone(l.agent);
    const file = path ?? uniqueKey(Object.keys(d.prompts), `prompts/${id}.md`);
    d.prompts[file] = l.agent.prompt ?? "";
    delete a.prompt;
    return { ...a, prompt_file: file, origin: { agent: l.id, hash: l.hash } };
  };
  const insertLibrary = (l: LibraryAgent, at?: { x: number; y: number }) => {
    if (!doc) return;
    const id = uniqueKey([...nodeNames(doc.spec), ...Object.keys(doc.spec.mcp_servers ?? {})], l.id);
    const p = at ?? rf.screenToFlowPosition({ x: window.innerWidth / 2 - 120, y: window.innerHeight / 2 - 50 });
    update((d) => {
      d.spec.agents[id] = fromLibrary(d, l, id);
      d.spec.layout = { ...d.spec.layout, [id]: { x: Math.round(p.x), y: Math.round(p.y) } };
    });
    showProps({ kind: "node", id });
    flash(t("lib.inserted", { name: l.title }));
  };
  const updateFromLibrary = async (id: string) => {
    const a = doc?.spec.agents[id], l = library.find((x) => x.id === a?.origin?.agent);
    if (!doc || !a || !l) return;
    if (val?.origins?.[id]?.changed !== false && !(await dialog.confirm({
      title: t("lib.updateTitle", { name: id }), message: t("lib.updateConfirm", { lib: l.title }),
      ok: t("lib.updateOk"), danger: true }))) return;
    update((d) => {
      const old = d.spec.agents[id];
      const n = fromLibrary(d, l, id, old.prompt_file);
      // what belongs to this flow stays: where each input comes from, join, loops and the icon
      for (const [k, i] of Object.entries(n.inputs ?? {})) if (old.inputs?.[k]?.from) i.from = old.inputs[k].from;
      if (old.join !== undefined) n.join = old.join;
      if (old.max_runs !== undefined) n.max_runs = old.max_runs;
      if (old.icon) n.icon = old.icon;
      d.spec.agents[id] = n;
    });
    flash(t("lib.updated", { name: id }));
  };
  const detachFromLibrary = (id: string) => {
    update((d) => { delete d.spec.agents[id].origin; });
    flash(t("lib.detached", { name: id }));
  };
  const saveToLibrary = async (id: string) => {
    const a = doc?.spec.agents[id];
    if (!doc || !a) return;
    const mine = library.find((l) => l.id === a.origin?.agent && l.source !== "core");  // local or extension
    const r = await dialog.form({
      title: t("lib.saveTitle", { name: id }), message: t("lib.saveMessage"), ok: t("lib.saveOk"),
      fields: [
        { key: "id", label: t("lib.saveId"), value: mine?.id ?? id, hint: t("lib.saveIdHint"),
          validate: (v) => !SLUG.test(v.trim()) ? t("insp.nameHint")
            : library.some((l) => l.id === v.trim() && l.source === "core") ? t("lib.idTaken") : null },
        { key: "title", label: t("lib.saveTitleField"), value: mine?.title ?? id },
        ...locationField(catalog?.locations, t, mine?.source && mine.source !== "core" ? mine.source : "local"),
      ],
    });
    if (!r) return;
    const where = r.location || "local";
    const exists = library.some((l) => l.id === r.id && l.source === where);
    if (exists && !(await dialog.confirm({ title: t("lib.overwriteTitle", { name: r.id }),
      message: t("lib.overwrite"), ok: t("lib.overwriteOk"), danger: true }))) return;
    try {
      // without `from`, an input has no source to take its type from: keep the resolved one
      const agent = clone(a);
      for (const i of Object.values(agent.inputs ?? {})) i.type = inputKind(doc.spec, i);
      const res = await api.saveToLibrary({ id: r.id, title: r.title, agent, prompt: promptOf(doc, a), overwrite: exists,
        location: where });
      update((d) => {
        const x = d.spec.agents[id];
        x.origin = { agent: res.id, hash: res.hash };
        for (const [k, i] of Object.entries(x.inputs ?? {})) i.type = agent.inputs![k].type;  // same as saved
      });
      setLibrary(await api.library());
      flash([t("lib.saved", { name: res.id }), ...res.notes].join(" · "));
    } catch (e) { flash(String((e as Error).message ?? e), true); }
  };
  const libState = (id: string) => {
    const o = val?.origins?.[id];
    if (!o) return "…";
    if (o.missing) return t("lib.state.missing");
    if (o.changed && o.update) return t("lib.state.both");
    return t(o.changed ? "lib.state.changed" : o.update ? "lib.state.update" : "lib.state.same");
  };
  return { promptOf, fromLibrary, insertLibrary, updateFromLibrary, detachFromLibrary, saveToLibrary, libState };
}

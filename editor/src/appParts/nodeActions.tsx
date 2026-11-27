/** Operations on a node (context menu and shortcuts): properties, run requests, copy, paste,
 * duplicate, model, rename, ports. */
import { type MenuItem } from "../ContextMenu";
import { clone, nodeNames, renameNode, SLUG, uniqueKey } from "../model";
import type { Selection } from "../types";
import { type Clip } from "./shared";
import type { EditorCtx } from "./shared";

export function useNodeActions(c: Pick<EditorCtx, "catalog" | "clip" | "dialog" | "doc" | "flash" | "rf" | "setRight" | "setRunReq" | "setSel" | "t" | "update">) {
  const { catalog, clip, dialog, doc, flash, rf, setRight, setRunReq, setSel, t, update } = c;
  // ---- node operations (context menu and shortcuts)
  const showProps = (s: Selection) => { setSel(s); setRight("props"); };
  const requestRun = (mode: "agent" | "until" | "from", target: string) => {
    setRunReq({ mode, target, nonce: Date.now() });
    setRight("run");
  };
  const copyNode = (id: string, quiet = false): Clip | null => {
    if (!doc) return null;
    const s = doc.spec, a = s.agents[id], g = s.gates?.[id];
    if (!a && !g) return null;
    const c: Clip = { base: id, agent: a && clone(a), gate: g && clone(g),
      prompt: a?.prompt_file ? doc.prompts[a.prompt_file] : undefined };
    clip.current = c;
    if (!quiet) flash(t("menu.copied", { name: id }));
    return c;
  };
  const pasteNode = (c: Clip | null = clip.current, at?: { x: number; y: number }) => {
    if (!doc || !c) return;
    const used = [...nodeNames(doc.spec), ...Object.keys(doc.spec.mcp_servers ?? {})];
    const id = uniqueKey(used, c.base);
    const p = at ?? rf.screenToFlowPosition({ x: window.innerWidth / 2 - 120, y: window.innerHeight / 2 - 50 });
    const droppedWrites = !!c.agent?.writes?.length;
    update((d) => {
      if (c.agent) {
        const a = clone(c.agent);
        delete a.writes;  // an artifact has one writer: the copy does not write the same ones
        if (a.prompt_file) {
          const path = uniqueKey(Object.keys(d.prompts), `prompts/${id}.md`);
          a.prompt_file = path;
          d.prompts[path] = c.prompt ?? "";
        }
        d.spec.agents[id] = a;
      } else if (c.gate) d.spec.gates = { ...d.spec.gates, [id]: clone(c.gate) };
      d.spec.layout = { ...d.spec.layout, [id]: { x: Math.round(p.x), y: Math.round(p.y) } };
    });
    setSel({ kind: "node", id });
    if (droppedWrites) flash(t("menu.writesNotCopied"));
  };
  const duplicateNode = (id: string) => {
    const at = doc?.spec.layout?.[id];
    pasteNode(copyNode(id, true), at && { x: at.x + 40, y: at.y + 40 });
  };
  /** The model of an agent in its context menu: the flow's (none set), a short name, or any ID. */
  const modelGrid = (id: string, current?: string | null): MenuItem => {
    const names = catalog?.models ?? ["inherit", "sonnet", "opus", "haiku"];
    const own = current && current !== "inherit" ? current : null;
    const other = own && !names.includes(own) ? own : null;
    const pick = (m: string | null) => update((d) => { d.spec.agents[id].model = m ?? undefined; });
    return { kind: "grid", wide: true, label: t("menu.model"), cells: [
      ...names.map((m) => m === "inherit"
        ? { key: m, title: t("menu.modelInheritTitle"), on: !own, content: t("menu.modelInherit"), onSelect: () => pick(null) }
        : { key: m, title: m, on: own === m, content: m, onSelect: () => pick(m) }),
      { key: "other", title: other ? `${other} · ${t("menu.modelOther")}` : t("menu.modelOther"), on: !!other, content: other ?? "…", onSelect: () => modelDialog(id) },
    ] };
  };
  const modelDialog = async (id: string) => {
    const a = doc?.spec.agents[id];
    if (!a) return;
    const r = await dialog.form({
      title: t("menu.modelTitle", { name: id }), ok: t("app.save"),
      fields: [{ key: "model", label: t("insp.model"), value: a.model ?? "", hint: t("menu.modelHint"),
        validate: (v) => /\s/.test(v.trim()) ? t("menu.modelHint") : null }],
    });
    if (!r) return;
    update((d) => { d.spec.agents[id].model = r.model.trim() || undefined; });
  };
  const renameDialog = async (id: string) => {
    if (!doc) return;
    const r = await dialog.form({
      title: t("menu.renameTitle", { name: id }), ok: t("menu.rename"),
      fields: [{ key: "name", label: t("insp.name"), value: id, hint: t("insp.nameHint"),
        validate: (v) => !SLUG.test(v.trim()) ? t("insp.nameHint")
          : v.trim() !== id && nodeNames(doc.spec).includes(v.trim()) ? t("insp.nameTaken") : null }],
    });
    if (!r || r.name === id) return;
    update((d) => renameNode(d.spec, id, r.name));
    setSel({ kind: "node", id: r.name });
  };
  const addPort = (id: string, side: "in" | "out") => {
    update((d) => {
      const a = d.spec.agents[id];
      if (side === "in") a.inputs = { ...a.inputs, [uniqueKey(Object.keys(a.inputs ?? {}), "input")]: {} };
      else a.outputs = { ...a.outputs, [uniqueKey(Object.keys(a.outputs ?? {}), "output")]: { type: "text" } };
    });
    showProps({ kind: "node", id });
  };
  return { showProps, requestRun, copyNode, pasteNode, duplicateNode, modelGrid, modelDialog, renameDialog, addPort };
}

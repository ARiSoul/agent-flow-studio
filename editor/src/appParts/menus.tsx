/** The context menus of the canvas (node, link, empty canvas) and the keyboard menu key. */
import { type KeyboardEvent as ReactKeyboardEvent } from "react";
import { type MenuItem } from "../ContextMenu";
import { Icon, ICON_IDS } from "../icons";
import { autoLayout, savedSize, setSavedSize } from "../model";
import type { Selection } from "../types";
import { PALETTE, dataEdgeId, parseDataEdge } from "./shared";
import type { useLibraryActions } from "./libraryActions";
import type { useNodeActions } from "./nodeActions";
import type { EditorCtx } from "./shared";

export function useCanvasMenus(c: Pick<EditorCtx, "addNode" | "clip" | "copyAgent" | "doc" | "kbMenu" | "library" | "nodeStyle" | "remove" | "rf" | "sel" | "setMenu" | "setSel" | "t" | "update" | "val"> & Pick<ReturnType<typeof useLibraryActions>, "detachFromLibrary" | "libState" | "saveToLibrary" | "updateFromLibrary"> & Pick<ReturnType<typeof useNodeActions>, "addPort" | "copyNode" | "duplicateNode" | "modelGrid" | "pasteNode" | "renameDialog" | "requestRun" | "showProps">) {
  const { addNode, addPort, clip, copyAgent, copyNode, detachFromLibrary, doc, duplicateNode, kbMenu, libState, library, modelGrid, nodeStyle, pasteNode, remove, renameDialog, requestRun, rf, saveToLibrary, sel, setMenu, setSel, showProps, t, update, updateFromLibrary, val } = c;
  // ---- context menu
  const menuAt = (e: { clientX: number; clientY: number }, el?: Element | null) => {
    if (e.clientX || e.clientY) return { x: e.clientX, y: e.clientY };
    const r = (el ?? document.querySelector(".canvas"))?.getBoundingClientRect();
    return r ? { x: r.left + 12, y: r.top + Math.min(r.height, 40) } : { x: 100, y: 100 };
  };
  const nodeMenu = (id: string, x: number, y: number) => {
    if (!doc) return;
    const s = doc.spec, a = s.agents[id], g = s.gates?.[id];
    const isStart = s.start === id;
    const items: MenuItem[] = [
      { label: t("menu.properties"), onSelect: () => showProps({ kind: "node", id }) },
    ];
    if (a) items.push(
      { label: t("insp.testAgent"), title: t("insp.testAgentTitle"), onSelect: () => requestRun("agent", id) },
      { label: t("insp.runUntil"), title: t("insp.runUntilTitle"), onSelect: () => requestRun("until", id) },
      { label: t("insp.runFrom"), title: t("insp.runFromTitle"), onSelect: () => requestRun("from", id) },
      { kind: "sep" },
      { label: t("menu.rename"), hint: "F2", onSelect: () => renameDialog(id) },
      { label: t("menu.setStart"), disabled: isStart, onSelect: () => update((d) => { d.spec.start = id; }) },
      { kind: "grid", label: t("insp.icon"), cells: ICON_IDS.map((k) => ({
        key: k, title: k, on: (a.icon || "robot") === k, content: <Icon id={k} size={16} />,
        onSelect: () => update((d) => { d.spec.agents[id].icon = k === "robot" ? undefined : k; }),
      })) },
      ...(a.action || a.subflow ? [] : [modelGrid(id, a.model)]),
      { label: t("menu.addInput"), onSelect: () => addPort(id, "in") },
      { label: t("menu.addOutput"), onSelect: () => addPort(id, "out") },
      { kind: "sep" },
      ...(a.origin ? [
        { kind: "label" as const, label: `${t("lib.from", { lib: val?.origins?.[id]?.title ?? a.origin.agent })} · ${libState(id)}` },
        { label: t("lib.update"), disabled: !library.some((l) => l.id === a.origin!.agent),
          onSelect: () => updateFromLibrary(id) },
        { label: t("lib.detach"), onSelect: () => detachFromLibrary(id) },
      ] : []),
      { label: t("lib.save"), onSelect: () => saveToLibrary(id) },
      { label: t("copy.button"), title: t("copy.agentTitle"), onSelect: () => copyAgent(id) },
      { label: t("copy.moveButton"), title: t("copy.moveAgentTitle"), disabled: isStart,
        onSelect: () => copyAgent(id, "move") },
    );
    if (g) items.push(
      { kind: "sep" },
      { label: t("menu.rename"), hint: "F2", onSelect: () => renameDialog(id) },
      ...(g.kind !== "decision" ? [{ label: t(g.kind === "human" ? "menu.toStop" : "menu.toHuman"),
        onSelect: () => update((d) => { d.spec.gates![id].kind = g.kind === "human" ? "stop" : "human"; }) }] : []),
    );
    if (savedSize(doc.spec, id, nodeStyle)) items.push({ label: t("menu.autoSize"),
      onSelect: () => update((d) => setSavedSize(d.spec, id, nodeStyle, null)) });
    items.push(
      { kind: "sep" },
      { label: t("menu.copy"), hint: "Ctrl+C", onSelect: () => copyNode(id) },
      { label: t("menu.duplicate"), hint: "Ctrl+D", onSelect: () => duplicateNode(id) },
      { label: t("menu.delete"), hint: "Del", danger: true, disabled: isStart,
        title: isStart ? t("insp.changeStartFirst") : undefined, onSelect: () => remove({ kind: "node", id }) },
    );
    setSel({ kind: "node", id });
    setMenu({ x, y, title: id, items });
  };
  const edgeMenu = (edgeId: string, x: number, y: number) => {
    if (!doc) return;
    if (edgeId.startsWith("d:")) {
      const l = parseDataEdge(edgeId), s: Selection = { kind: "data", ...l };
      setSel(s);
      setMenu({ x, y, title: `${doc.spec.agents[l.agent]?.inputs?.[l.input]?.from ?? "—"} → ${l.agent}.${l.input}`, items: [
        { label: t("menu.properties"), onSelect: () => showProps(s) },
        { kind: "sep" },
        { label: t("menu.deleteLink"), hint: "Del", danger: true, onSelect: () => remove(s) },
      ] });
      return;
    }
    const index = Number(edgeId.slice(1)), e = doc.spec.edges?.[index];
    if (!e) return;
    const s: Selection = { kind: "edge", index };
    setSel(s);
    setMenu({ x, y, title: `${e.from} → ${e.to}`, items: [
      { label: t("menu.editCondition"), onSelect: () => showProps(s) },
      { label: t("menu.clearCondition"), disabled: !e.if?.trim() && !e.when?.trim(),
        onSelect: () => update((d) => { const x = d.spec.edges![index]; delete x.if; x.when = ""; }) },
      { kind: "sep" },
      { label: t("menu.deleteLink"), hint: "Del", danger: true, onSelect: () => remove(s) },
    ] });
  };
  const paneMenu = (x: number, y: number) => {
    const at = rf.screenToFlowPosition({ x, y });
    setMenu({ x, y, items: [
      ...PALETTE.map((p) => ({ label: t(`menu.add.${p.kind}`), onSelect: () => addNode(p.kind, at) })),
      { label: clip.current ? t("menu.pasteNamed", { name: clip.current.base }) : t("menu.paste"), hint: "Ctrl+V",
        disabled: !clip.current, onSelect: () => pasteNode(clip.current, at) },
      { kind: "sep" },
      { label: t("app.arrange"), onSelect: arrange },
      { label: t("menu.fitView"), onSelect: () => rf.fitView({ padding: 0.15, duration: 200 }) },
      { label: t("menu.flowProperties"), onSelect: () => showProps(null) },
    ] });
  };
  const arrange = () => { update((d) => autoLayout(d.spec, undefined, nodeStyle)); setTimeout(() => rf.fitView({ padding: 0.15, minZoom: 0.8, maxZoom: 1 }), 50); };
  const fromKeyboard = () => Date.now() - kbMenu.current < 600;
  // Shift+F10 or the context-menu key: the menu of the focused node/link, else of the selection, else the canvas
  const onCanvasKey = (e: ReactKeyboardEvent) => {
    if (!(e.key === "ContextMenu" || (e.key === "F10" && e.shiftKey)) || !doc) return;
    if ((e.target as HTMLElement).closest("input,textarea,select")) return;
    e.preventDefault();
    kbMenu.current = Date.now();
    const el = e.target as HTMLElement;
    const nodeEl = el.closest<HTMLElement>(".react-flow__node");
    const edgeEl = el.closest<HTMLElement>(".react-flow__edge");
    const place = (x?: Element | null) => menuAt({ clientX: 0, clientY: 0 }, x);
    const sNode = sel?.kind === "node" ? sel.id : null;
    const nodeId = nodeEl?.dataset.id ?? sNode;
    if (nodeId) {
      const p = place(nodeEl ?? document.querySelector(`.react-flow__node[data-id="${CSS.escape(nodeId)}"]`));
      return nodeMenu(nodeId, p.x, p.y);
    }
    const edgeId = edgeEl?.dataset.id ?? (edgeEl?.dataset.testid?.replace(/^rf__edge-/, ""))
      ?? (sel?.kind === "edge" ? `e${sel.index}` : sel?.kind === "data" ? dataEdgeId(sel.agent, sel.input) : null);
    if (edgeId) { const p = place(edgeEl); return edgeMenu(edgeId, p.x, p.y); }
    const r = document.querySelector(".canvas")?.getBoundingClientRect();
    paneMenu(r ? r.left + r.width / 2 : 200, r ? r.top + r.height / 2 : 200);
  };
  return { menuAt, nodeMenu, edgeMenu, paneMenu, arrange, fromKeyboard, onCanvasKey };
}

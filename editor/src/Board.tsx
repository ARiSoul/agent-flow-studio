// The workspace board: one card per flow (inputs, outputs, last run), with the links between
// flows (where an input usually comes from, subflows, hand-offs recorded by runs). Double click
// opens a flow; the side panel holds the workspace's roots, variables and environments.
import { useCallback, useEffect, useMemo, useState, type CSSProperties } from "react";
import {
  Background, Controls, Handle, MarkerType, Position, ReactFlow, ReactFlowProvider, useReactFlow,
  type Edge, type Node, type NodeProps,
} from "@xyflow/react";
import { api } from "./api";
import { ContextMenu, type MenuState } from "./ContextMenu";
import { useI18n } from "./i18n";
import { useTheme } from "./theme";
import type { BoardFlow, WorkspaceNow } from "./types";
import { ContextPanel } from "./ContextPanel";
import { useDialog } from "./Dialog";
import { Splitter } from "./Splitter";
import { useWorkspaceActions, WorkspaceSettings, wsErrorText } from "./Workspaces";
import { Chat } from "./Chat";

type CardData = { flow: BoardFlow; label: (k: string) => string };

function FlowCard({ data }: NodeProps<Node<CardData>>) {
  const f = data.flow;
  const ins = Object.entries(f.inputs);
  return (
    <div className={`board-card${f.valid ? "" : " invalid"}`}>
      <Handle type="target" position={Position.Left} />
      <div className="node-head">
        <span className="node-kind">{f.execution === "graph" ? data.label("board.graph") : data.label("board.orchestrator")}</span>
        {f.last_run && <span className={`badge run-${f.last_run.status}`} title={f.last_run.input}>{data.label(`run.status.${f.last_run.status}`)}</span>}
        {!f.valid && <span className="badge warn">⚠</span>}
      </div>
      <div className="node-name">{f.title}</div>
      <div className="muted small mono">{f.name}</div>
      {ins.length > 0 && <ul className="board-io">
        {ins.map(([n, x]) => <li key={n} title={x.source ? `← ${x.source.flow} · ${x.source.output}` : undefined}>
          <span className="in">▸</span> {x.label} <span className="muted">{x.type}</span>{x.source ? " ⇠" : ""}</li>)}
      </ul>}
      {f.outputs.length > 0 && <ul className="board-io">
        {f.outputs.map((o) => <li key={`${o.agent}.${o.name}`}><span className="out">◂</span> {o.agent}.{o.name} <span className="muted">{o.type}</span></li>)}
      </ul>}
      <Handle type="source" position={Position.Right} />
    </div>
  );
}
const nodeTypes = { flowcard: FlowCard };

/** Producers to the left of the flows they feed (a simple layered layout). */
function layout(flows: BoardFlow[], links: [string, string][]): Record<string, { x: number; y: number }> {
  const level: Record<string, number> = Object.fromEntries(flows.map((f) => [f.name, 0]));
  for (let i = 0; i < flows.length; i++) {
    let moved = false;
    for (const [a, b] of links) if (a in level && b in level && level[b] < level[a] + 1 && level[a] < flows.length) {
      level[b] = level[a] + 1; moved = true;
    }
    if (!moved) break;
  }
  const rows: Record<number, number> = {};
  const out: Record<string, { x: number; y: number }> = {};
  for (const f of flows) {
    const l = level[f.name];
    const r = (rows[l] = (rows[l] ?? -1) + 1);
    out[f.name] = { x: l * 340, y: r * 260 };
  }
  return out;
}

type BoardActions = { onOpen: (flow: string) => void; onRun: (flow: string) => void; onNewFlow: () => void;
  onFlowsChanged: () => void; onContext: (mode: "explore" | "update") => void; flowsKey: string };

function BoardCanvas({ ws, flash, onOpen, onRun, onNewFlow, onFlowsChanged, onContext, flowsKey }: {
  ws: WorkspaceNow; flash: (text: string, bad?: boolean) => void } & BoardActions) {
  const { t, tOr } = useI18n();
  const rf = useReactFlow();
  const dialog = useDialog();
  const { base } = useTheme();
  const [flows, setFlows] = useState<BoardFlow[] | null>(null);
  const [menu, setMenu] = useState<MenuState | null>(null);
  const posKey = `afs.board.${ws.name}`;
  const [moved, setMoved] = useState<Record<string, { x: number; y: number }>>(() => {
    try { return JSON.parse(localStorage.getItem(posKey) ?? "{}"); } catch { return {}; }
  });
  const acts = useWorkspaceActions(ws, flash);
  const load = useCallback(() => api.board().then((b) => setFlows(b.flows)).catch((e) => flash(String(e), true)), [flash]);
  useEffect(() => { load(); }, [load, flowsKey]);  // flowsKey: a flow was created or deleted

  const { nodes, edges } = useMemo(() => {
    if (!flows) return { nodes: [] as Node[], edges: [] as Edge[] };
    const names = new Set(flows.map((f) => f.name));
    const edges: Edge[] = [];
    const seen = new Set<string>();
    const add = (from: string, to: string, kind: string, label: string) => {
      const id = `${kind}:${from}:${to}:${label}`;
      if (!names.has(from) || !names.has(to) || seen.has(id)) return;
      seen.add(id);
      edges.push({ id, source: from, target: to, label, className: `board-${kind}`, type: "smoothstep",
        markerEnd: { type: MarkerType.ArrowClosed }, animated: kind === "handoff" });
    };
    for (const f of flows) {
      for (const [n, x] of Object.entries(f.inputs)) if (x.source) add(x.source.flow, f.name, "hint", `${x.source.output} → ${n}`);
      for (const h of f.handoffs) if (!f.inputs[h.input]?.source || f.inputs[h.input].source!.flow !== h.flow)
        add(h.flow, f.name, "handoff", `${h.output} → ${h.input}`);
      for (const s of f.subflows) add(s, f.name, "sub", t("board.subflow"));
    }
    const auto = layout(flows, edges.map((e) => [e.source, e.target]));
    const label = (k: string) => tOr(k, k.split(".").pop() ?? k);
    const nodes: Node[] = flows.map((f) => ({ id: f.name, type: "flowcard", position: moved[f.name] ?? auto[f.name],
      data: { flow: f, label } }));
    return { nodes, edges };
  }, [flows, moved, t, tOr]);

  const changed = () => { load(); onFlowsChanged(); };
  const remove = async (name: string) => {
    if (!(await dialog.confirm({ title: t("app.deleteTitle"), message: t("app.confirmDelete", { name }),
      ok: t("app.delete"), danger: true }))) return;
    try { const r = await api.remove(name); flash(t("app.trashed", { path: r.trashed })); changed(); }
    catch (e) { flash(String((e as Error).message ?? e), true); }
  };
  const resetLayout = () => {
    setMoved({});
    try { localStorage.removeItem(posKey); } catch { /* storage not available */ }
    setTimeout(() => rf.fitView({ padding: 0.15, duration: 200 }), 60);
  };
  const cardMenu = (name: string, x: number, y: number) => setMenu({ x, y, title: name, items: [
    { label: t("board.open"), onSelect: () => onOpen(name) },
    { label: t("board.run"), title: t("board.runTitle"), onSelect: () => onRun(name) },
    { kind: "sep" },
    { label: t("board.duplicateHere"), onSelect: () => acts.copyTo({ kind: "flow", name }, name).then((r) => r && changed()) },
    { label: t("copy.button"), onSelect: () => acts.copyTo({ kind: "flow", name }, name).then((r) => r && changed()) },
    { label: t("copy.moveButton"), onSelect: () => acts.copyTo({ kind: "flow", name }, name, "move").then((r) => r && changed()) },
    { kind: "sep" },
    { label: t("app.delete"), danger: true, onSelect: () => remove(name) },
  ] });
  const paneMenu = (x: number, y: number) => setMenu({ x, y, title: ws.name === "studio" ? t("ws.studio") : ws.title ?? ws.name, items: [
    { label: t("board.newFlow"), onSelect: onNewFlow },
    { kind: "sep" },
    ...(ws.name !== "studio" ? [
      { label: t("ctx.updateAll"), title: t("ctx.costHint"), onSelect: () => onContext("update") },
      { label: t("ctx.exploreAll"), title: t("ctx.costHint"), onSelect: () => onContext("explore") },
      { kind: "sep" as const },
    ] : []),
    { label: t("board.resetLayout"), onSelect: resetLayout },
    { label: t("menu.fitView"), onSelect: () => rf.fitView({ padding: 0.15, duration: 200 }) },
    { kind: "sep" },
    { label: t("ws.new"), onSelect: () => acts.create() },
    { label: t("ws.duplicate"), disabled: ws.name === "studio", onSelect: () => acts.duplicate(ws.name) },
  ] });

  if (!flows) return <p className="muted pad">{t("app.loading")}</p>;
  return (
    <>
      {flows.length === 0 && <p className="muted pad board-empty">{t("board.empty")}</p>}
      <ReactFlow nodes={nodes} edges={edges} nodeTypes={nodeTypes} colorMode={base} fitView minZoom={0.2} maxZoom={1.5}
        nodesConnectable={false} proOptions={{ hideAttribution: true }}
        onNodeDoubleClick={(_, n) => onOpen(n.id)}
        onNodeContextMenu={(e, n) => { e.preventDefault(); cardMenu(n.id, e.clientX, e.clientY); }}
        onPaneContextMenu={(e) => { e.preventDefault(); paneMenu(e.clientX, e.clientY); }}
        onNodeDragStop={(_, n) => setMoved((m) => {
          const next = { ...m, [n.id]: { x: Math.round(n.position.x), y: Math.round(n.position.y) } };
          try { localStorage.setItem(posKey, JSON.stringify(next)); } catch { /* storage not available */ }
          return next;
        })}>
        <Background gap={20} />
        <Controls />
      </ReactFlow>
      <div className="board-legend small">
        <span className="lg hint">{t("board.legendHint")}</span>
        <span className="lg handoff">{t("board.legendHandoff")}</span>
        <span className="lg sub">{t("board.legendSub")}</span>
      </div>
      {menu && <ContextMenu menu={menu} onClose={() => setMenu(null)} />}
    </>
  );
}

/** The board view: the canvas of flows and the workspace settings beside it (resizable). */
export function Board({ ws, flash, onSaved, side, onSide, onConnectors, onCatalog, ...acts }: { ws: WorkspaceNow;
  flash: (text: string, bad?: boolean) => void; onSaved: () => void;
  onConnectors: () => void; onCatalog: () => void;
  side: number; onSide: (px: number | null) => void } & Omit<BoardActions, "onContext">) {
  const { t, lang } = useI18n();
  const [ctxKey, setCtxKey] = useState(0);  // the context block reloads after the settings change
  const [setKey, setSetKey] = useState(0);  // the settings reload after the assistant changed them
  const [tab, setTab] = useState<"settings" | "assistant">("settings");
  const [, setBusy] = useState(false);
  const onContext = async (mode: "explore" | "update") => {
    try {
      await api.updateContext(mode, null);
      setCtxKey((k) => k + 1);  // the context block finds the run and shows its progress
    } catch (e) { flash(wsErrorText(e, lang), true); }
  };
  return (
    <div className="board" style={{ "--insp-w": `${side}px` } as CSSProperties}>
      <div className="board-canvas">
        <ReactFlowProvider><BoardCanvas ws={ws} flash={flash} onContext={onContext} {...acts} /></ReactFlowProvider>
      </div>
      <aside className="board-side">
        <Splitter edge="left" size={side} onSize={onSide} label={t("app.resizePanel")} />
        <h2>{ws.name === "studio" ? t("ws.studio") : ws.title ?? ws.name}</h2>
        {ws.name === "studio"
          ? <p className="muted small">{t("board.studioHint")}</p>
          : <>
            <div className="tabs board-tabs">
              <button className={tab === "settings" ? "on" : ""} onClick={() => setTab("settings")}>{t("wsa.tabSettings")}</button>
              <button className={tab === "assistant" ? "on" : ""} onClick={() => setTab("assistant")}>{t("tabs.assistant")}</button>
            </div>
            <div className="board-settings" hidden={tab !== "settings"}>
              <ContextPanel key={ctxKey} flash={flash} />
              <WorkspaceSettings key={setKey} name={ws.name} flash={flash} onSaved={() => { onSaved(); setCtxKey((k) => k + 1); }} />
            </div>
            <div className="board-chat" hidden={tab !== "assistant"}>
              <Chat key={ws.name} flow="@workspace" lang={lang} onBusy={setBusy} onLibrary={() => undefined}
                workspace={{
                  onApplied: (p) => {
                    if (p.kind === "workspace") { onSaved(); setSetKey((k) => k + 1); setCtxKey((k) => k + 1); }
                    else if (p.kind === "flow") acts.onFlowsChanged();
                    else onCatalog();
                  },
                  onOpenFlow: acts.onOpen, onConnectors,
                }} />
            </div>
          </>}
      </aside>
    </div>
  );
}

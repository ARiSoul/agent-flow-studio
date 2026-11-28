// The workspace board: one card per flow (inputs, outputs, last run, Open and Run, ⋯), with the
// links between flows (where an input usually comes from, flows called as a subflow, hand-offs
// recorded by runs). Linked flows are laid out as chains; the others sit in a "Not linked" row.
// Enter or double click opens a flow; Shift+F10 or ⋯ opens a card's menu. The side panel leads
// with what needs the person (failed, waiting, broken) and the recent runs; the context, the
// workspace settings and the workspace assistant are in the other tabs.
import { useCallback, useEffect, useMemo, useRef, useState, type CSSProperties, type KeyboardEvent as ReactKeyboardEvent } from "react";
import dagre from "@dagrejs/dagre";
import {
  Background, Controls, Handle, Position, ReactFlow, ReactFlowProvider, useReactFlow,
  type BuiltInEdge, type Node, type NodeProps,
} from "@xyflow/react";
import { api } from "./api";
import { ContextMenu, type MenuState } from "./ContextMenu";
import { useI18n } from "./i18n";
import { localizeServer } from "./i18n/server";
import { useTheme } from "./theme";
import type { BoardFlow, IOKind, WorkspaceDetail, WorkspaceNow } from "./types";
import { ContextPanel } from "./ContextPanel";
import { useDialog } from "./Dialog";
import { Splitter } from "./Splitter";
import { useWorkspaceActions, WorkspaceSettings, wsErrorText } from "./Workspaces";
import { Chat } from "./Chat";
import { RUN_GLYPH, typeTag } from "./nodes";
import { NODE_W } from "./model";
import { useProd } from "./envKind";

/** Port rows shown per column; the rest is "+n more". */
const MAX_ROWS = 4;
/** A saved run status as a run-dot state (the same shapes as the editor's nodes). */
const RUN_STATE: Record<string, string> = { done: "done", error: "error", lost: "error", stopped: "stopped",
  paused: "stopped", running: "active", gate: "gate", decided: "gate" };
const GLYPH: Record<string, string> = { ...RUN_GLYPH, active: "●" };
const FAILED = new Set(["error", "lost"]);

/** How long ago a run started, in the UI language ("3 minutes ago", "há 2 horas"). */
export function ago(seconds: number, lang: string): string {
  const s = Math.max(0, Math.round(seconds));
  const [n, unit]: [number, Intl.RelativeTimeFormatUnit] = s < 60 ? [s, "second"] : s < 3600 ? [Math.floor(s / 60), "minute"]
    : s < 86400 ? [Math.floor(s / 3600), "hour"] : [Math.floor(s / 86400), "day"];
  return new Intl.RelativeTimeFormat(lang, { numeric: "auto" }).format(-n, unit);
}
const since = (r: { started: number }, lang: string) => ago(Date.now() / 1000 - r.started, lang);

/** The height a card takes (the layout keeps cards apart with it). */
export function cardHeight(f: BoardFlow): number {
  const rows = (n: number) => Math.min(n, MAX_ROWS) + (n > MAX_ROWS ? 1 : 0);
  const ports = f.valid ? Math.max(rows(Object.keys(f.inputs).length), rows(f.outputs.length)) : 0;
  return 18 + 24 + 18 + (f.description ? 32 : 0) + (f.valid ? 0 : 34) + (ports ? 12 + ports * 18 : 0) + 66;
}

/** What a flow's last run asks of the person: a failure, a wait, or nothing. */
export function runTone(f: BoardFlow): "bad" | "warn" | "ok" | "none" | "other" {
  const r = f.last_run;
  if (!r) return "none";
  if (r.waiting) return "warn";
  if (FAILED.has(r.status)) return "bad";
  return r.status === "done" ? "ok" : "other";
}

type Handlers = { open: (n: string) => void; run: (n: string) => void; openRun: (n: string, input: string) => void;
  fix: (n: string) => void; menu: (n: string, x: number, y: number) => void };
type CardData = { flow: BoardFlow; on: Handlers; prod: boolean };

/** The last run in words, with its tone ("✕ Failed 3 minutes ago"). */
function RunRow({ f, on }: { f: BoardFlow; on: Handlers }) {
  const { t, tOr, lang } = useI18n();
  const r = f.last_run;
  const tone = runTone(f);
  const text = !r ? t("board.neverRun")
    : tone === "warn" ? t("board.waitingAgo", { t: since(r, lang) })
    : tone === "bad" ? t("board.failedAgo", { t: since(r, lang) })
    : tone === "ok" ? t("board.doneAgo", { t: since(r, lang) })
    : t("board.otherState", { glyph: GLYPH[RUN_STATE[r.status] ?? "stopped"] ?? "", status: tOr(`run.status.${r.status}`, r.status), t: since(r, lang) });
  return (
    <div className={`board-run-info ${tone}`} title={r ? r.input : undefined}>
      <span className="board-run-text">{text}{r?.input && !r.input.startsWith(`${f.name}-`)
        ? <span className="muted"> · {r.input.slice(0, 40)}</span> : null}</span>
      {tone === "bad" && r && <button className="link small nodrag" aria-label={t("board.viewRunOf", { title: f.title })}
        onClick={() => on.openRun(f.name, r.input)}>{t("board.viewRun")}</button>}
    </div>
  );
}

function FlowCard({ data }: NodeProps<Node<CardData>>) {
  const { t, lang } = useI18n();
  const { flow: f, on, prod } = data;
  const ins = Object.entries(f.inputs);
  const r = f.last_run;
  const state = r ? (r.waiting ? "gate" : RUN_STATE[r.status] ?? "stopped") : null;
  const more = (n: number) => n > MAX_ROWS
    ? <div className="port board-more">{t("board.more", { n: String(n - MAX_ROWS) })}</div> : null;
  const tone = runTone(f);
  return (
    <div className={`board-card${f.valid ? "" : " invalid"}${tone === "warn" ? " waits" : ""}`}>
      <Handle type="target" position={Position.Left} id="c" />
      <Handle type="source" position={Position.Right} id="c" />
      <Handle type="target" position={Position.Left} id="sub" className="h-head" />
      <Handle type="source" position={Position.Right} id="sub" className="h-head" />
      <div className="node-head">
        <span className="board-icon" aria-hidden="true">{f.execution === "orchestrator" ? "◎" : "⎇"}</span>
        <span className="node-name" title={f.title}>{f.title}</span>
        {state && <span className={`run-dot ${state}`} aria-hidden="true">{GLYPH[state] ?? ""}</span>}
        <button className="icon small board-dots nodrag" aria-haspopup="menu" aria-label={t("board.moreFor", { title: f.title })}
          onClick={(e) => { const b = (e.currentTarget as HTMLElement).getBoundingClientRect(); on.menu(f.name, b.left, b.bottom + 2); }}>⋯</button>
      </div>
      <div className="board-id">
        <span className="mono">{f.name}</span>
        {f.execution === "orchestrator" && <span className="port-tag">{t("board.orchestratorTag")}</span>}
      </div>
      {f.description && <div className="node-desc" title={f.description}>{f.description}</div>}
      {!f.valid && <div className="board-invalid" title={f.error?.msg}>
        ⚠ {t("board.invalid", { reason: f.error ? localizeServer(f.error, f.error.msg, lang) : "?" })}</div>}
      {f.valid && (ins.length > 0 || f.outputs.length > 0) && (
        <div className="ports board-ports">
          <div className="ports-col in">
            {ins.slice(0, MAX_ROWS).map(([n, x]) => (
              <div key={n} className="port" title={`${n} · ${x.type}${x.source ? `\n${t("board.sourceOf", { flow: x.source.flow, output: x.source.output })}` : ""}`}>
                <Handle type="target" position={Position.Left} id={`in:${n}`} className="h-row" />
                <span className={`port-tag t-${x.type}`}>{typeTag(x.type as IOKind)}</span>
                <span className="port-name">{x.label}</span>
              </div>))}
            {more(ins.length)}
          </div>
          <div className="ports-col out">
            {f.outputs.slice(0, MAX_ROWS).map((o) => (
              <div key={`${o.agent}.${o.name}`} className="port" title={`${o.agent}.${o.name} · ${o.type}`}>
                <span className="port-name">{o.agent}.{o.name}</span>
                <span className={`port-tag t-${o.type}`}>{typeTag(o.type as IOKind)}</span>
                <Handle type="source" position={Position.Right} id={`out:${o.agent}.${o.name}`} className="h-row" />
              </div>))}
            {more(f.outputs.length)}
          </div>
        </div>)}
      <div className="board-run">
        <RunRow f={f} on={on} />
        <div className="board-acts">
          {f.valid
            ? <button className="small nodrag" aria-label={t("board.openFlow", { title: f.title })}
              onClick={() => on.open(f.name)}>{t("board.open")}</button>
            : <button className="small nodrag" aria-label={t("board.fixFlow", { title: f.title })}
              onClick={() => on.fix(f.name)}>{t("board.fixInEditor")}</button>}
          {f.valid && (tone === "warn" && r
            ? <button className="small nodrag board-needs-btn" aria-label={t("board.needsYouFlow", { title: f.title })}
              title={t("board.needsYouTitle")} onClick={() => on.openRun(f.name, r.input)}>? {t("board.answer")}</button>
            : <button className={`small nodrag btn-run${prod ? " prod" : ""}`} title={t("board.runTitle")}
              aria-label={t(prod ? "board.runFlowProd" : "board.runFlow", { title: f.title })}
              onClick={() => on.run(f.name)}>▶ {t(prod ? "board.runProd" : "board.runShort")}</button>)}
        </div>
      </div>
    </div>
  );
}

/** The title of the row of flows that link to no other flow. */
function RowLabel({ data }: NodeProps<Node<{ text: string }>>) {
  return <div className="board-row-label">{data.text}</div>;
}
const nodeTypes = { flowcard: FlowCard, rowlabel: RowLabel };

type Pos = { x: number; y: number };
/** Linked flows as chains, producers on the left (dagre with the cards' real sizes); the flows with
 * no link in a grid row below them, sorted by title. Returns the positions and the row's top. */
function layout(flows: BoardFlow[], links: [string, string][]): { pos: Record<string, Pos>; row: Pos | null } {
  const linked = new Set(links.flatMap(([a, b]) => (a !== b ? [a, b] : [])));
  const chain = flows.filter((f) => linked.has(f.name));
  const alone = flows.filter((f) => !linked.has(f.name)).sort((a, b) => a.title.localeCompare(b.title));
  const pos: Record<string, Pos> = {};
  let bottom = 0, width = 3 * NODE_W + 64;
  if (chain.length) {
    const g = new dagre.graphlib.Graph();
    g.setGraph({ rankdir: "LR", nodesep: 32, ranksep: 120 });  // room for the link labels between columns
    g.setDefaultEdgeLabel(() => ({}));
    for (const f of chain) g.setNode(f.name, { width: NODE_W, height: cardHeight(f) });
    for (const [a, b] of links) if (a !== b) g.setEdge(a, b);
    dagre.layout(g);
    let minX = Infinity, maxX = 0;
    for (const f of chain) {
      const p = g.node(f.name);
      pos[f.name] = { x: Math.round(p.x - NODE_W / 2), y: Math.round(p.y - cardHeight(f) / 2) };
      bottom = Math.max(bottom, pos[f.name].y + cardHeight(f));
      minX = Math.min(minX, pos[f.name].x); maxX = Math.max(maxX, pos[f.name].x + NODE_W);
    }
    for (const n of Object.keys(pos)) pos[n].x -= minX;
    // the cards of one row of a chain share their top (dagre centres them on their handles)
    const rows: BoardFlow[][] = [];
    for (const f of [...chain].sort((x, y) => pos[x.name].y + cardHeight(x) / 2 - pos[y.name].y - cardHeight(y) / 2)) {
      const mid = (c: BoardFlow) => pos[c.name].y + cardHeight(c) / 2;
      const row = rows.find((r) => Math.abs(mid(r[0]) - mid(f)) < 60);
      if (row) row.push(f); else rows.push([f]);
    }
    for (const r of rows) {
      const top = Math.min(...r.map((f) => pos[f.name].y));
      for (const f of r) pos[f.name].y = top;
    }
    width = Math.max(width, maxX - minX);
  }
  if (!alone.length) return { pos, row: null };
  const cols = Math.max(1, Math.floor((width + 32) / (NODE_W + 32)));
  const top = chain.length ? bottom + 48 + 22 : 22;
  let y = top, rowH = 0;
  alone.forEach((f, i) => {
    if (i && i % cols === 0) { y += rowH + 32; rowH = 0; }
    pos[f.name] = { x: (i % cols) * (NODE_W + 32), y };
    rowH = Math.max(rowH, cardHeight(f));
  });
  return { pos, row: chain.length ? { x: 0, y: top - 22 } : null };
}

/** The automatic positions, with the cards that would cover a card the person moved put below
 * every moved card (in their layout order). */
function clearOf(auto: Record<string, Pos>, moved: Record<string, Pos>, flows: BoardFlow[]): Record<string, Pos> {
  const fixed = flows.filter((f) => moved[f.name]).map((f) => ({ ...moved[f.name], h: cardHeight(f) }));
  if (!fixed.length) return auto;
  const hits = (p: Pos, h: number) => fixed.some((m) =>
    p.x < m.x + NODE_W + 16 && m.x < p.x + NODE_W + 16 && p.y < m.y + m.h + 16 && m.y < p.y + h + 16);
  const out = { ...auto };
  const stays = flows.filter((f) => !moved[f.name] && !hits(auto[f.name], cardHeight(f)));
  let y = Math.max(...fixed.map((m) => m.y + m.h), ...stays.map((f) => auto[f.name].y + cardHeight(f))) + 48;
  for (const f of flows) {
    if (moved[f.name] || !hits(auto[f.name], cardHeight(f))) continue;
    out[f.name] = { x: auto[f.name].x, y };
    y += cardHeight(f) + 32;
  }
  return out;
}

/** The arrow heads of the three link kinds, colored by the theme's tokens. */
function Arrows() {
  const kinds: [string, string][] = [["hint", "var(--muted)"], ["sub", "var(--accent)"], ["handoff", "var(--ok)"]];
  return (
    <svg className="board-defs" aria-hidden="true" width="0" height="0">
      <defs>{kinds.map(([k, c]) => (
        <marker key={k} id={`board-arrow-${k}`} viewBox="0 0 10 10" refX="9" refY="5" markerWidth="12" markerHeight="12" markerUnits="userSpaceOnUse" orient="auto-start-reverse">
          <path d="M0,0 L10,5 L0,10 z" style={{ fill: c }} />
        </marker>))}</defs>
    </svg>
  );
}

type BoardActions = { onOpen: (flow: string) => void; onRun: (flow: string) => void; onNewFlow: () => void;
  onOpenRun: (flow: string, input: string) => void; onFix: (flow: string) => void;
  onDuplicate: (flow: string, title: string) => void;
  onFlowsChanged: () => void; flowsKey: string };

function BoardCanvas({ ws, flash, flows, reload, onContext, ...a }: { ws: WorkspaceNow;
  flash: (text: string, bad?: boolean) => void; flows: BoardFlow[]; reload: () => void;
  onContext: (mode: "explore" | "update") => void } & BoardActions) {
  const { t, tOr } = useI18n();
  const rf = useReactFlow();
  const dialog = useDialog();
  const prod = useProd();
  const { base } = useTheme();
  const [menu, setMenu] = useState<MenuState | null>(null);
  const wrap = useRef<HTMLDivElement>(null);
  const posKey = `afs.board.${ws.name}`;
  const [moved, setMoved] = useState<Record<string, Pos>>(() => {
    try { return JSON.parse(localStorage.getItem(posKey) ?? "{}"); } catch { return {}; }
  });
  const acts = useWorkspaceActions(ws, flash);
  // the cards call the latest handlers without being rebuilt when the parent draws again
  const latest = useRef(a);
  latest.current = a;
  const menuRef = useRef<(n: string, x: number, y: number) => void>(() => undefined);
  const on = useMemo<Handlers>(() => ({
    open: (n) => latest.current.onOpen(n), run: (n) => latest.current.onRun(n),
    openRun: (n, i) => latest.current.onOpenRun(n, i), fix: (n) => latest.current.onFix(n),
    menu: (n, x, y) => menuRef.current(n, x, y),
  }), []);

  const { nodes, edges, kinds } = useMemo(() => {
    const names = new Set(flows.map((f) => f.name));
    const byName = Object.fromEntries(flows.map((f) => [f.name, f]));
    const byKind: Record<string, BuiltInEdge[]> = { hint: [], sub: [], handoff: [] };
    const seen = new Set<string>();
    /** The row's handle when that row shows on the card, else the card's own. */
    const outHandle = (flow: string, ref: string) => {
      const f = byName[flow];
      const i = f?.outputs.findIndex((o) => `${o.agent}.${o.name}` === ref) ?? -1;
      return f?.valid && i >= 0 && i < MAX_ROWS ? `out:${ref}` : "c";
    };
    const inHandle = (flow: string, input: string) => {
      const f = byName[flow];
      const i = f ? Object.keys(f.inputs).indexOf(input) : -1;
      return f?.valid && i >= 0 && i < MAX_ROWS ? `in:${input}` : "c";
    };
    const add = (from: string, to: string, kind: string, label: string, sh: string, th: string) => {
      const id = `${kind}:${from}:${to}:${label}`;
      if (!names.has(from) || !names.has(to) || seen.has(id)) return;
      seen.add(id);
      byKind[kind].push({ id, source: from, target: to, sourceHandle: sh, targetHandle: th, label,
        className: `board-${kind}`, type: "smoothstep", markerEnd: `board-arrow-${kind}`, pathOptions: { offset: 16 },
        labelBgPadding: [6, 2], labelBgBorderRadius: 8 } satisfies BuiltInEdge);
    };
    for (const f of flows) {
      for (const [n, x] of Object.entries(f.inputs)) if (x.source)
        add(x.source.flow, f.name, "hint", `${x.source.output} → ${n}`, outHandle(x.source.flow, x.source.output), inHandle(f.name, n));
      for (const h of f.handoffs) if (!f.inputs[h.input]?.source || f.inputs[h.input].source!.flow !== h.flow)
        add(h.flow, f.name, "handoff", `${h.output} → ${h.input}`, outHandle(h.flow, h.output), inHandle(f.name, h.input));
      for (const s of f.subflows) add(f.name, s, "sub", t("board.calls"), "sub", "sub");  // the caller → the flow it calls
    }
    const edges = [...byKind.hint, ...byKind.sub, ...byKind.handoff];  // hints first: the others draw above
    const { pos, row } = layout(flows, edges.map((e) => [e.source, e.target]));
    const auto = clearOf(pos, moved, flows);
    const nodes: Node[] = flows.map((f) => ({ id: f.name, type: "flowcard", position: moved[f.name] ?? auto[f.name],
      ariaLabel: t("board.cardLabel", { title: f.title,
        status: f.last_run ? (f.last_run.waiting ? t("board.needsYou") : tOr(`run.status.${f.last_run.status}`, f.last_run.status))
          : t("board.neverRun") }),
      data: { flow: f, on, prod } satisfies CardData }));
    if (row) nodes.push({ id: "@unlinked", type: "rowlabel", position: row, selectable: false, draggable: false,
      focusable: false, data: { text: t("board.unlinked") } });
    return { nodes, edges, kinds: new Set(edges.map((e) => String(e.className).slice(6))) };
  }, [flows, moved, t, tOr, on, prod]);

  // the first view: the whole board when it fits at 85 % or more; else the start at the top left
  const placed = useRef(false);
  useEffect(() => {
    if (placed.current || !wrap.current) return;
    const cards = nodes.filter((n) => n.type === "flowcard");
    if (!cards.length) return;
    placed.current = true;
    const W = wrap.current.clientWidth, H = wrap.current.clientHeight;
    let minX = Infinity, minY = Infinity, maxX = -Infinity, maxY = -Infinity;
    for (const n of nodes) {
      const h = n.type === "flowcard" ? cardHeight((n.data as CardData).flow) : 20;
      minX = Math.min(minX, n.position.x); minY = Math.min(minY, n.position.y);
      maxX = Math.max(maxX, n.position.x + NODE_W); maxY = Math.max(maxY, n.position.y + h);
    }
    const bw = maxX - minX, bh = maxY - minY;
    const fitW = Math.min(1, (W - 80) / bw), fit = Math.min(fitW, (H - 96) / bh);
    // the whole board at 85 % or more; a slightly wide one whole across down to 65 % (as the editor); else the
    // start at the top left at 85 % (the person pans)
    const zoom = fit >= 0.85 ? fit : fitW >= 0.65 && fitW < 0.85 ? fitW : 0.85;
    const across = zoom === fit || zoom === fitW;
    const x = across ? (W - bw * zoom) / 2 - minX * zoom : 24 - minX * zoom;
    const y = zoom === fit ? Math.max(48, (H - bh * zoom) / 2) - minY * zoom : 48 - minY * zoom;
    requestAnimationFrame(() => rf.setViewport({ x, y, zoom }));
  }, [nodes, rf]);

  const changed = () => { reload(); a.onFlowsChanged(); };
  const remove = async (name: string) => {
    if (!(await dialog.confirm({ title: t("app.deleteTitle"), message: t("app.confirmDelete", { name }),
      ok: t("app.delete"), danger: true }))) return;
    try { const r = await api.remove(name); flash(t("app.trashed", { path: r.trashed })); changed(); }
    catch (e) { flash(String((e as Error).message ?? e), true); }
  };
  const fitAll = () => rf.fitView({ padding: 0.15, maxZoom: 1, duration: 200 });
  const resetLayout = () => {
    setMoved({});
    try { localStorage.removeItem(posKey); } catch { /* storage not available */ }
    setTimeout(fitAll, 60);
  };
  const context = async (mode: "explore" | "update") => {
    if (await dialog.confirm({ title: t(mode === "update" ? "ctx.updateAll" : "ctx.exploreAll"),
      message: t("ctx.costHint"), ok: t(mode === "update" ? "ctx.updateAll" : "ctx.exploreAll") })) onContext(mode);
  };
  const cardMenu = (name: string, x: number, y: number) => {
    const f = flows.find((x) => x.name === name);
    setMenu({ x, y, title: f?.title ?? name, items: [
      f?.valid === false
        ? { label: t("board.fixInEditor"), onSelect: () => a.onFix(name) }
        : { label: t("board.open"), onSelect: () => a.onOpen(name) },
      { label: t(prod ? "board.runProdMenu" : "board.run"), title: t("board.runTitle"), disabled: !f?.valid,
        note: f?.valid ? undefined : t("board.runBlocked"), onSelect: () => a.onRun(name) },
      { kind: "sep" },
      { label: t("board.duplicateHere"), onSelect: () => a.onDuplicate(name, f?.title ?? name) },
      { label: t("copy.button"), onSelect: () => acts.copyTo({ kind: "flow", name }, name).then((r) => r && changed()) },
      { label: t("copy.moveButton"), onSelect: () => acts.copyTo({ kind: "flow", name }, name, "move").then((r) => r && changed()) },
      { kind: "sep" },
      { label: t("board.deleteFlow"), danger: true, onSelect: () => remove(name) },
    ] });
  };
  menuRef.current = cardMenu;
  const paneMenu = (x: number, y: number) => setMenu({ x, y, title: ws.name === "studio" ? t("ws.studio") : ws.title ?? ws.name, items: [
    { label: t("board.newFlow"), onSelect: a.onNewFlow },
    { kind: "sep" },
    ...(ws.name !== "studio" ? [
      { label: `${t("ctx.updateAll")}…`, title: t("ctx.costHint"), onSelect: () => context("update") },
      { label: `${t("ctx.exploreAll")}…`, title: t("ctx.costHint"), onSelect: () => context("explore") },
      { kind: "sep" as const },
    ] : []),
    { label: t("board.resetLayout"), onSelect: resetLayout },
    { label: t("menu.fitView"), onSelect: fitAll },
    { kind: "sep" },
    { label: t("ws.new"), onSelect: () => acts.create() },
    { label: t("ws.duplicate"), disabled: ws.name === "studio", onSelect: () => acts.duplicate(ws.name) },
  ] });
  // Enter opens the focused card; Shift+F10 or the context-menu key opens its menu (or the board's)
  const onKey = (e: ReactKeyboardEvent) => {
    const el = e.target as HTMLElement;
    const card = el.classList.contains("react-flow__node") ? el : null;
    if (e.key === "Enter" && card?.dataset.id) {
      e.preventDefault();
      const f = flows.find((x) => x.name === card.dataset.id);
      if (f) (f.valid ? a.onOpen : a.onFix)(f.name);
      return;
    }
    if (!(e.key === "ContextMenu" || (e.key === "F10" && e.shiftKey))) return;
    if (el.closest("input,textarea,select")) return;
    e.preventDefault();
    const host = el.closest<HTMLElement>(".react-flow__node");
    if (host?.dataset.id && host.dataset.id !== "@unlinked") {
      const r = host.getBoundingClientRect();
      return cardMenu(host.dataset.id, r.left + 12, r.top + 28);
    }
    const r = (e.currentTarget as HTMLElement).getBoundingClientRect();
    paneMenu(r.left + 12, r.top + 40);
  };

  return (
    <div className="board-flow" ref={wrap} onKeyDown={onKey}>
      <Arrows />
      {flows.length === 0 && <p className="muted pad board-empty">{t("board.empty")}</p>}
      <ReactFlow nodes={nodes} edges={edges} nodeTypes={nodeTypes} colorMode={base}
        minZoom={0.2} maxZoom={1.5} nodesConnectable={false} proOptions={{ hideAttribution: true }}
        onNodeDoubleClick={(e, n) => { if (n.type === "flowcard" && !(e.target as HTMLElement).closest("button")) on.open(n.id); }}
        onNodeContextMenu={(e, n) => { e.preventDefault(); if (n.type === "flowcard") cardMenu(n.id, e.clientX, e.clientY); }}
        onPaneContextMenu={(e) => { e.preventDefault(); paneMenu(e.clientX, e.clientY); }}
        onNodeDragStop={(_, n) => setMoved((m) => {
          const next = { ...m, [n.id]: { x: Math.round(n.position.x), y: Math.round(n.position.y) } };
          try { localStorage.setItem(posKey, JSON.stringify(next)); } catch { /* storage not available */ }
          return next;
        })}>
        <Background gap={20} />
        <Controls />
      </ReactFlow>
      {kinds.size > 0 && <div className="board-legend" role="note" aria-label={t("board.legend")}>
        {kinds.has("hint") && <span className="lg hint">{t("board.legendHint")}</span>}
        {kinds.has("handoff") && <span className="lg handoff">{t("board.legendHandoff")}</span>}
        {kinds.has("sub") && <span className="lg sub">{t("board.legendSub")}</span>}
      </div>}
      {menu && <ContextMenu menu={menu} onClose={() => setMenu(null)} />}
    </div>
  );
}

/** What needs the person (failed, waiting, broken) and the recent runs: the side panel's first tab. */
function Overview({ ws, flows, reload, ...a }: { ws: WorkspaceNow; flows: BoardFlow[] | null; reload: number }
  & Pick<BoardActions, "onOpenRun" | "onFix">) {
  const { t, lang } = useI18n();
  const rf = useReactFlow();
  const [d, setD] = useState<WorkspaceDetail | null>(null);
  useEffect(() => {
    if (ws.name !== "studio") api.workspace(ws.name).then(setD).catch(() => undefined);
  }, [ws.name, reload]);
  const envs = Object.entries(d?.spec?.environments ?? {});
  const rank = (f: BoardFlow) => (!f.valid ? 2 : runTone(f) === "warn" ? 0 : runTone(f) === "bad" ? 1 : 9);
  const attention = (flows ?? []).filter((f) => rank(f) < 9).sort((x, y) => rank(x) - rank(y)
    || (y.last_run?.started ?? 0) - (x.last_run?.started ?? 0));
  const listed = new Set(attention.map((f) => f.name));
  const ran = (flows ?? []).filter((f) => f.last_run);
  const recent = ran.filter((f) => !listed.has(f.name)).sort((x, y) => y.last_run!.started - x.last_run!.started).slice(0, 5);
  /** Centres the card of a flow and gives it the focus. */
  const show = (name: string) => {
    const n = rf.getNode(name);
    if (!n) return;
    rf.setCenter(n.position.x + NODE_W / 2, n.position.y + (n.measured?.height ?? 200) / 2, { zoom: 1, duration: 300 });
    setTimeout(() => document.querySelector<HTMLElement>(`.react-flow__node[data-id="${CSS.escape(name)}"]`)?.focus(), 320);
  };
  return (
    <div className="board-overview">
      {d?.spec?.description && <p className="small">{d.spec.description}</p>}
      {envs.length > 0 && <div className="board-envs">
        <span className="field-label">{t("ws.environments")}</span>
        {envs.map(([e, x]) => (
          <span key={e} className={`badge env-badge env-${x.kind}${e === ws.environment ? " now" : ""}`}
            title={e === ws.environment ? t("board.envNow") : t(`ws.kind.${x.kind}`)}>
            {e}{e === ws.environment ? " ●" : ""}</span>))}
      </div>}
      <h3>{t("board.attention")}</h3>
      {flows && attention.length === 0 && <p className="muted small">{t("board.attentionNone")}</p>}
      <ul className="board-list" aria-label={t("board.attention")}>
        {attention.map((f) => {
          const tone = !f.valid ? "broken" : runTone(f);
          const r = f.last_run;
          return (
            <li key={f.name} className={`board-item ${tone}`}>
              <button className="board-item-main" onClick={() => show(f.name)} title={t("board.showCard")}>
                <span className="board-item-glyph" aria-hidden="true">{tone === "broken" ? "⚠" : tone === "warn" ? "?" : "✕"}</span>
                <span className="board-item-text">
                  <span className="board-item-title">{f.title}</span>
                  <span className="board-item-state">{tone === "broken" ? t("board.brokenShort")
                    : t(tone === "warn" ? "board.waitingShort" : "board.failedShort", { t: since(r!, lang) })}</span>
                </span>
              </button>
              {tone === "broken"
                ? <button className="small" aria-label={t("board.fixFlow", { title: f.title })} onClick={() => a.onFix(f.name)}>{t("board.fixInEditor")}</button>
                : tone === "warn"
                  ? <button className="small board-needs-btn" aria-label={t("board.needsYouFlow", { title: f.title })} onClick={() => a.onOpenRun(f.name, r!.input)}>{t("board.answer")}</button>
                  : <button className="small" aria-label={t("board.viewRunOf", { title: f.title })} onClick={() => a.onOpenRun(f.name, r!.input)}>{t("board.viewRun")}</button>}
            </li>);
        })}
      </ul>
      {(recent.length > 0 || ran.length === 0) && <h3>{t(attention.length ? "board.recentOther" : "board.recent")}</h3>}
      {flows && ran.length === 0 && <p className="muted small">{t("board.recentNone")}</p>}
      <ul className="board-list" aria-label={t(attention.length ? "board.recentOther" : "board.recent")}>
        {recent.map((f) => {
          const r = f.last_run!;
          const state = r.waiting ? "gate" : RUN_STATE[r.status] ?? "stopped";
          return (
            <li key={f.name} className="board-item">
              <button className="board-item-main" aria-label={t("board.openRunOf", { title: f.title })} title={r.input}
                onClick={() => a.onOpenRun(f.name, r.input)}>
                <span className={`run-dot ${state}`} aria-hidden="true">{GLYPH[state] ?? ""}</span>
                <span className="board-item-title">{f.title}</span>
                <span className="board-item-state">{since(r, lang)}</span>
              </button>
            </li>);
        })}
      </ul>
    </div>
  );
}

/** The board view: the canvas of flows and the workspace panel beside it (resizable). */
export function Board(props: Parameters<typeof BoardView>[0]) {
  return <ReactFlowProvider><BoardView {...props} /></ReactFlowProvider>;
}

function BoardView({ ws, flash, onSaved, side, onSide, onConnectors, onCatalog, ...acts }: { ws: WorkspaceNow;
  flash: (text: string, bad?: boolean) => void; onSaved: () => void;
  onConnectors: () => void; onCatalog: () => void;
  side: number; onSide: (px: number | null) => void } & BoardActions) {
  const { t, lang } = useI18n();
  const [ctxKey, setCtxKey] = useState(0);  // the context reloads after the settings change
  const [setKey, setSetKey] = useState(0);  // the settings reload after the assistant changed them
  const [tab, setTab] = useState<"overview" | "settings" | "assistant">("overview");
  const [flows, setFlows] = useState<BoardFlow[] | null>(null);
  const [, setBusy] = useState(false);
  const known = useRef(acts);
  known.current = acts;
  const load = useCallback(() => {
    api.board().then((b) => {
      setFlows(b.flows);
      // a flow added or removed outside the editor (a file, the assistant): its flow list follows
      const names = new Set(known.current.flowsKey.split(",").filter(Boolean));
      if (b.flows.length !== names.size || b.flows.some((f) => !names.has(f.name))) known.current.onFlowsChanged();
    }).catch((e) => flash(String(e), true));
  }, [flash]);
  useEffect(() => { load(); }, [load, acts.flowsKey]);  // flowsKey: a flow was created or deleted
  const onContext = async (mode: "explore" | "update") => {
    try {
      await api.updateContext(mode, null);
      setTab("settings");
      setCtxKey((k) => k + 1);  // the context block finds the run and shows its progress
    } catch (e) { flash(wsErrorText(e, lang), true); }
  };
  const studio = ws.name === "studio";
  const tabs = [["overview", t("board.overview")], ["settings", t("board.tabSettings")], ["assistant", t("tabs.assistant")]] as const;
  const overview = <Overview ws={ws} flows={flows} reload={ctxKey} onOpenRun={acts.onOpenRun} onFix={acts.onFix} />;
  return (
    <div className="board" style={{ "--insp-w": `${side}px` } as CSSProperties}>
      <div className="board-canvas">
        {flows ? <BoardCanvas ws={ws} flash={flash} flows={flows} reload={load} onContext={onContext} {...acts} />
          : <p className="muted pad">{t("app.loading")}</p>}
      </div>
      <aside className="board-side" aria-label={t("board.sideLabel")}>
        <Splitter edge="left" size={side} onSize={onSide} label={t("app.resizePanel")} />
        <h2>{studio ? t("ws.studio") : ws.title ?? ws.name}</h2>
        {studio
          ? <div className="board-settings">{overview}<p className="muted small">{t("board.studioHint")}</p></div>
          : <>
            <div className="tabs board-tabs" role="group" aria-label={t("board.sideViews")}>
              {tabs.map(([k, label]) => (
                <button key={k} aria-pressed={tab === k} className={tab === k ? "on" : ""}
                  onClick={() => setTab(k)}>{label}</button>))}
            </div>
            <div className="board-settings" hidden={tab !== "overview"}>{overview}</div>
            <div className="board-settings" hidden={tab !== "settings"}>
              <WorkspaceSettings key={setKey} name={ws.name} flash={flash} onSaved={() => { onSaved(); setCtxKey((k) => k + 1); }} />
              <ContextPanel key={ctxKey} flash={flash} />
            </div>
            <div className="board-chat" hidden={tab !== "assistant"}>
              <Chat key={ws.name} flow="@workspace" lang={lang} onBusy={setBusy} onLibrary={() => undefined}
                workspace={{
                  onApplied: (p) => {
                    if (p.kind === "workspace") { onSaved(); setSetKey((k) => k + 1); setCtxKey((k) => k + 1); }
                    else if (p.kind === "flow") { load(); acts.onFlowsChanged(); }
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

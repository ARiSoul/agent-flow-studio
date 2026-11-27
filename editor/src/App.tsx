import { localizeServer } from "./i18n/server";
import {
  useCallback, useEffect, useMemo, useRef, useState, type CSSProperties, type DragEvent,
  type KeyboardEvent as ReactKeyboardEvent,
} from "react";
import { clampSize, DEFAULT_SIZES, loadSizes, saveSizes, Splitter, type PanelSizes } from "./Splitter";
import {
  Background, Controls, MiniMap, ReactFlow, ReactFlowProvider, useReactFlow, MarkerType,
  type Connection, type Edge, type EdgeChange, type IsValidConnection, type Node, type NodeChange,
} from "@xyflow/react";
import { api, currentWorkspaceName, errorText } from "./api";
import { Chat } from "./Chat";
import { Connections } from "./Connections";
import { Providers } from "./Providers";
import { ContextMenu, type MenuItem, type MenuState } from "./ContextMenu";
import { useDialog } from "./Dialog";
import { AgentIcon, Icon, ICON_IDS } from "./icons";
import { LibraryManager, locLabel } from "./LibraryManager";
import { parseRunLink } from "./runLink";
import { CONTEXT_NODE, edgeKey, RunPanel, type RunRequest, type Trace } from "./RunPanel";
import { Inspector } from "./Inspector";
import { Board } from "./Board";
import { useWorkspaceActions, WorkspaceBar } from "./Workspaces";
import { LANGS, useI18n, type Lang } from "./i18n";
import { THEMES, useTheme, type Theme } from "./theme";
import {
  autoLayout, clone, ensureLayout, NODE_STYLES, type NodeStyle, inputKind, newAgent, newGate, nodeNames, nodeSize, removeNode, savedSize, setSavedSize,
  renameNode, SLUG, sourceIO, tidy, uniqueKey, uniqueName,
} from "./model";
import { nodeTypes, type Port } from "./nodes";
import type {
  AgentRaw, Catalog, Doc, FlowSummary, FlowTemplate, GateRaw, LibraryAgent, Selection, Validation,
} from "./types";

/** A copied node (in memory, for paste and duplicate). */
type Clip = { base: string; agent?: AgentRaw; gate?: GateRaw; prompt?: string };

const PALETTE = [
  { kind: "agent", label: "palette.agent", hint: "palette.agentHint", base: "agent" },
  { kind: "human", label: "palette.human", hint: "palette.humanHint", base: "approval" },
  { kind: "stop", label: "palette.stop", hint: "palette.stopHint", base: "stop" },
  { kind: "decision", label: "palette.decision", hint: "palette.decisionHint", base: "decision" },
  { kind: "action", label: "palette.action", hint: "palette.actionHint", base: "action" },
  { kind: "subflow", label: "palette.subflow", hint: "palette.subflowHint", base: "subflow" },
] as const;
type PaletteKind = (typeof PALETTE)[number]["kind"];
const FLOW_NAME = /^[a-z0-9][a-z0-9_-]{0,39}$/;  // same rule as the server
const DATA_LINKS_KEY = "afs.dataLinks";
// the open studio workspace (cookie afs_ws): the last open flow is kept per workspace
const WS_NOW = currentWorkspaceName();
const LAST_FLOW_KEY = WS_NOW === "studio" ? "afs.flow" : `afs.flow.${WS_NOW}`;
const VIEW_KEY = "afs.view";
const HIDDEN_KEY = "afs.hiddenPanels";
type Panel = "pal" | "insp" | "bottom";

/** Id of a data link on the canvas: d:<agent>:<input> (agent and input names are slugs, no ":"). */
const dataEdgeId = (agent: string, input: string) => `d:${agent}:${input}`;
const parseDataEdge = (id: string) => { const [, agent, input] = id.split(":"); return { agent, input }; };

/** Where a flow lives: on this machine only, in an extension, or public (git would publish it). */
function LocationBadge({ loc, isPublic }: { loc?: string; isPublic?: boolean }) {
  const { t } = useI18n();
  if (isPublic) return <span className="badge warn loc" title={t("loc.publicTitle")}>{t("loc.public")}</span>;
  if (loc === "workspace") return <span className="badge loc" title={t("loc.workspaceTitle")}>{t("loc.workspace")}</span>;
  if (loc && loc !== "local") return <span className="badge loc" title={t("loc.extensionTitle", { name: loc })}>{t("loc.extension", { name: loc })}</span>;
  return <span className="badge loc" title={t("loc.localTitle")}>{t("loc.local")}</span>;
}

/** The "Where" field of the create / save-to-library forms (only when there are extensions). */
function locationField(locations: string[] | undefined, t: ReturnType<typeof useI18n>["t"], value = "local") {
  const locs = locations ?? ["local"];
  if (locs.length < 2) return [];
  return [{ key: "location", label: t("loc.where"), value, hint: t("loc.whereHint"),
    options: locs.map((l) => ({ value: l, label: locLabel(t, l) })) }];
}

function Editor() {
  const rf = useReactFlow();
  const { t, tn, lang, setLang } = useI18n();
  const { theme, setTheme, base } = useTheme();
  // node style: a preference of this browser, not part of the flow
  const [nodeStyle, setNodeStyleState] = useState<NodeStyle>(() => {
    const v = localStorage.getItem("afs.nodeStyle");
    return NODE_STYLES.includes(v as NodeStyle) ? (v as NodeStyle) : "card";
  });
  const setNodeStyle = (s: NodeStyle) => {
    setNodeStyleState(s);
    try { localStorage.setItem("afs.nodeStyle", s); } catch { /* storage not available */ }
    setTimeout(() => rf.fitView({ padding: 0.15 }), 80);
  };
  const dialog = useDialog();
  const [catalog, setCatalog] = useState<Catalog | null>(null);
  const [flows, setFlows] = useState<FlowSummary[]>([]);
  const [templates, setTemplates] = useState<FlowTemplate[]>([]);
  const [library, setLibrary] = useState<LibraryAgent[]>([]);
  const [current, setCurrent] = useState<string | null>(null);
  const [doc, setDoc] = useState<Doc | null>(null);
  const [saved, setSaved] = useState<string>("");
  const [sel, setSel] = useState<Selection>(null);
  const [val, setVal] = useState<Validation | null>(null);
  const [tab, setTab] = useState<"problems" | "flow">("problems");
  const [msg, setMsg] = useState<{ text: string; bad?: boolean } | null>(null);
  const [right, setRightTab] = useState<"props" | "chat" | "run">("props");
  // the flow editor, or the board of the workspace's flows (a preference of this browser)
  const [view, setViewState] = useState<"flow" | "board">(() => (localStorage.getItem(VIEW_KEY) === "board" ? "board" : "flow"));
  const setView = (v: "flow" | "board") => {
    setViewState(v);
    try { localStorage.setItem(VIEW_KEY, v); } catch { /* storage not available */ }
    if (v === "flow") setTimeout(() => rf.fitView({ padding: 0.15 }), 80);
  };
  // panels hidden by the user (this browser): palette, right panel, bottom panel
  const [hidden, setHidden] = useState<Record<Panel, boolean>>(() => {
    try { return { pal: false, insp: false, bottom: false, ...JSON.parse(localStorage.getItem(HIDDEN_KEY) ?? "{}") }; }
    catch { return { pal: false, insp: false, bottom: false }; }
  });
  const togglePanel = (p: Panel, show?: boolean) => setHidden((h) => {
    const n = { ...h, [p]: show === undefined ? !h[p] : !show };
    if (n[p] === h[p]) return h;
    try { localStorage.setItem(HIDDEN_KEY, JSON.stringify(n)); } catch { /* storage not available */ }
    setTimeout(() => rf.fitView({ padding: 0.15, duration: 150 }), 60);
    return n;
  });
  /** Opens a tab of the right panel (and shows the panel when it is hidden). */
  const setRight = (tab: "props" | "chat" | "run") => { setRightTab(tab); togglePanel("insp", true); };
  const [trace, setTrace] = useState<Trace>({ nodes: {}, edges: {} });
  const [assistBusy, setAssistBusy] = useState(false);
  const [showConns, setShowConns] = useState(false);
  const [showProviders, setShowProviders] = useState<{ focus?: string } | null>(null);
  const [showLibrary, setShowLibrary] = useState(false);
  const [runReq, setRunReq] = useState<RunRequest | null>(null);
  const [runNeeds, setRunNeeds] = useState(0);  // gates and approvals that wait in the Run panel
  const [dims, setDims] = useState<Record<string, { width: number; height: number }>>({});
  const [showData, setShowData] = useState<boolean>(() => {
    try { return localStorage.getItem(DATA_LINKS_KEY) !== "0"; } catch { return true; }
  });
  const toggleData = () => setShowData((v) => {
    try { localStorage.setItem(DATA_LINKS_KEY, v ? "0" : "1"); } catch { /* storage not available */ }
    return !v;
  });
  const past = useRef<Doc[]>([]);
  const future = useRef<Doc[]>([]);
  // panel sizes changed with the splitters (this browser); the others follow the CSS defaults
  const [sizes, setSizes] = useState<Partial<PanelSizes>>(loadSizes);
  const resize = (k: keyof PanelSizes) => (px: number | null) => setSizes((s) => {
    const n = { ...s };
    if (px === null) delete n[k]; else n[k] = clampSize(k, px);
    saveSizes(n);
    return n;
  });
  const [menu, setMenu] = useState<MenuState | null>(null);
  const closeMenu = useCallback(() => setMenu(null), []);
  const clip = useRef<Clip | null>(null);
  const kbMenu = useRef(0);  // time of the last keyboard-opened menu (ignore the browser's own event)

  const dirty = doc !== null && JSON.stringify(doc) !== saved;
  const flash = useCallback((text: string, bad = false) => {
    setMsg({ text, bad }); setTimeout(() => setMsg(null), bad ? 8000 : 5000);
  }, []);
  const wsActs = useWorkspaceActions(catalog?.workspace, flash);
  /** Another environment: the variables and approvals change, the flows stay. */
  const refreshEnv = () => {
    api.catalog().then(setCatalog).catch(() => undefined);
    if (doc && current) api.validate(current, tidy(doc)).then(setVal).catch(() => undefined);
  };
  /** "Copy to workspace…" works on the saved files: unsaved changes would not go with it. */
  const copyFlow = (how: "copy" | "move" = "copy") => {
    if (!current) return;
    if (dirty) return flash(t("copy.saveFirst"), true);
    wsActs.copyTo({ kind: "flow", name: current }, current, how).then(async (r) => {
      if (!r) return;
      await refreshFlows();  // first: the flow that left must not be opened again
      if (how === "move") { setCurrent(null); setDoc(null); }
    });
  };
  const flowCopyMenu = (x: number, y: number) => setMenu({ x, y, title: current ?? "", items: [
    { label: t("copy.button"), title: t("copy.flowTitle"), onSelect: () => copyFlow("copy") },
    { label: t("copy.moveButton"), title: t("copy.moveFlowTitle"), onSelect: () => copyFlow("move") },
  ] });
  const copyAgent = (id: string, how: "copy" | "move" = "copy") => {
    if (!current) return;
    if (dirty) return flash(t("copy.saveFirst"), true);
    wsActs.copyTo({ kind: "flow_agent", name: current, agent: id }, id, how).then(async (r) => {
      if (!r) return;
      api.library().then(setLibrary);
      if (how === "move") {  // the agent left this flow: load the flow as it is now
        const f = await api.load(current);
        const d = ensureLayout({ spec: f.spec, prompts: f.prompts }, nodeStyle);
        setDoc(d); setSaved(JSON.stringify(d)); setVal(f);
      }
    });
  };

  // ---- load
  const refreshFlows = useCallback(() => api.flows().then(setFlows), []);
  useEffect(() => {
    api.catalog().then(setCatalog).catch((e) => flash(String(e), true));
    refreshFlows().then(() => undefined);
    api.templates().then(setTemplates).catch(() => undefined);
    api.library().then(setLibrary).catch((e) => flash(String(e), true));
  }, [refreshFlows]);
  // the last open flow (a preference of this browser), else the first one
  useEffect(() => {
    if (current || !flows.length) return;
    const link = parseRunLink(location.hash);  // a link to a run (#flow=…&run=…&step=…) wins
    if (link.flow && flows.some((f) => f.name === link.flow)) {
      setCurrent(link.flow);
      if (link.run) { setRunReq({ mode: "full", open: link.run, flow: link.flow, step: link.step, nonce: Date.now() }); setRight("run"); }
      return;
    }
    let last: string | null = null;
    try { last = localStorage.getItem(LAST_FLOW_KEY); } catch { /* storage not available */ }
    setCurrent(flows.some((f) => f.name === last) ? last : flows[0].name);
  }, [flows, current]);  // eslint-disable-line react-hooks/exhaustive-deps -- setters only
  useEffect(() => {
    if (!current) return;
    try { localStorage.setItem(LAST_FLOW_KEY, current); } catch { /* storage not available */ }
    api.load(current).then((r) => {
      const d = ensureLayout({ spec: r.spec, prompts: r.prompts }, nodeStyle);
      setDoc(d);
      setSaved(JSON.stringify(d));
      setVal(r);
      setSel(null);
      past.current = [];
      future.current = [];
      setTimeout(() => rf.fitView({ padding: 0.15 }), 50);
    }).catch((e) => flash(String(e), true));
  }, [current, rf]);

  // ---- validate (debounced) on every change
  useEffect(() => {
    if (!doc || !current) return;
    const t = setTimeout(() => api.validate(current, tidy(doc)).then(setVal).catch(() => undefined), 400);
    return () => clearTimeout(t);
  }, [doc, current]);

  // ---- changes with history
  const update = useCallback((fn: (d: Doc) => void, record = true) => {
    setDoc((prev) => {
      if (!prev) return prev;
      if (record) { past.current.push(prev); future.current = []; if (past.current.length > 200) past.current.shift(); }
      const next = clone(prev);
      fn(next);
      return next;
    });
  }, []);
  const undo = () => { const p = past.current.pop(); if (p && doc) { future.current.push(doc); setDoc(p); } };
  const redo = () => { const f = future.current.pop(); if (f && doc) { past.current.push(doc); setDoc(f); } };

  const remove = useCallback((s: Selection) => {
    if (!s || !doc) return;
    if (s.kind === "node") {
      if (s.id === doc.spec.start) return flash(t("app.cantDeleteStart"), true);
      update((d) => removeNode(d.spec, s.id));
    } else if (s.kind === "data") update((d) => { const i = d.spec.agents[s.agent]?.inputs?.[s.input]; if (i) delete i.from; });
    else update((d) => { d.spec.edges = (d.spec.edges ?? []).filter((_, i) => i !== s.index); });
    setSel(null);
  }, [doc, update, t]);

  const save = useCallback(async () => {
    if (!doc || !current) return;
    try {
      let r = await api.save(current, tidy(doc));
      if (r.private_terms?.length) {
        // a public flow (git publishes it) never names private words: keep it on this machine instead?
        if (!(await dialog.confirm({ title: t("loc.privateTitle"), ok: t("loc.privateOk"),
          message: t("loc.privateMessage", { terms: r.private_terms.join(", ") }) }))) return;
        await api.moveFlow(current, "local");
        await refreshFlows();
        r = await api.save(current, tidy(doc));
      }
      if (!r.saved && !r.blocked_by_secret && r.errors?.length) {
        // half-done flows can be saved: warn, list the first problems and ask
        setTab("problems");
        const first = r.errors.slice(0, 5).map((e) => `• ${localizeServer(e, e.msg, lang)}`).join("\n");
        const more = r.errors.length > 5 ? `\n${t("app.moreProblems", { n: r.errors.length - 5 })}` : "";
        if (await dialog.confirm({ title: tn("app.saveWithProblemsTitle", r.errors.length),
          message: `${t("app.saveWithProblems")}\n\n${first}${more}`, ok: t("app.saveAnyway") })) {
          r = await api.save(current, tidy(doc), true);
        } else { setVal(r); return; }
      }
      setVal(r);
      if (r.saved) {
        setSaved(JSON.stringify(doc));
        flash(r.with_problems ? t("app.savedWithProblems", { name: current }) : t("app.savedTo", { name: current }),
          !!r.with_problems);
        refreshFlows();
      } else { setTab("problems"); flash(r.blocked_by_secret ? t("app.notSavedSecret") : t("app.notSaved"), true); }
    } catch (e) { flash(String(e), true); }
  }, [doc, current, refreshFlows, t]);

  // ---- shortcuts
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      const mod = e.ctrlKey || e.metaKey;
      if (mod && e.key === "s") { e.preventDefault(); save(); }
      const inField = (e.target as HTMLElement)?.closest("input,textarea,select");
      if (mod && e.key === "z" && !inField) { e.preventDefault(); if (e.shiftKey) redo(); else undo(); }
      if (mod && e.key === "y" && !inField) { e.preventDefault(); redo(); }
      // panels, as in VS Code: Ctrl+B palette, Ctrl+Alt+B right panel, Ctrl+J bottom panel
      // (physical keys: with Ctrl+Alt = AltGr, e.key changes on many keyboard layouts)
      if (mod && !e.shiftKey && e.code === "KeyB") { e.preventDefault(); togglePanel(e.altKey ? "insp" : "pal"); }
      if (mod && !e.shiftKey && !e.altKey && e.code === "KeyJ") { e.preventDefault(); togglePanel("bottom"); }
      // node shortcuts: only on the canvas selection, never while typing or in a dialog/menu
      const busy = inField || (e.target as HTMLElement)?.closest?.(".dialog-layer,.ctx-menu,.modal");
      const node = sel?.kind === "node" ? sel.id : null;
      if (busy || menu) return;
      const textSelected = !!window.getSelection()?.toString();
      if (mod && e.key === "c" && node && !textSelected) copyNode(node);
      if (mod && e.key === "v" && clip.current && !textSelected) { e.preventDefault(); pasteNode(); }
      if (mod && e.key === "d" && node) { e.preventDefault(); duplicateNode(node); }
      if (e.key === "F2" && node) { e.preventDefault(); renameDialog(node); }
    };
    const onUnload = (e: BeforeUnloadEvent) => { if (dirty) e.preventDefault(); };
    window.addEventListener("keydown", onKey);
    window.addEventListener("beforeunload", onUnload);
    return () => { window.removeEventListener("keydown", onKey); window.removeEventListener("beforeunload", onUnload); };
  });

  // ---- React Flow graph (derived from the flow; the flow is the single source of truth)
  const errNodes = useMemo(() => new Set((val?.errors ?? []).flatMap((e) => e.nodes)), [val]);
  const nodes: Node[] = useMemo(() => {
    if (!doc) return [];
    const s = doc.spec, pos = (id: string) => { const p = s.layout?.[id]; return { x: p?.x ?? 0, y: p?.y ?? 0 }; };
    // a size the person set (resize handles of the selected node); not for avatars
    const size = (id: string) => {
      const S = savedSize(s, id, nodeStyle);
      if (!S) return {};
      return { width: S.w, height: nodeSize(s, id, nodeStyle).h, className: "sized" };  // never hides the ports
    };
    return [
      ...Object.entries(s.agents).map(([id, a]) => {
        const ins: Port[] = Object.entries(a.inputs ?? {}).map(([n, i]) => ({
          name: n, type: inputKind(s, i), multiple: !!sourceIO(s, i.from)?.multiple, from: i.from,
        }));
        const outs: Port[] = Object.entries(a.outputs ?? {}).map(([n, o]) => ({
          name: n, type: o.type ?? "text", multiple: !!o.multiple,
        }));
        return {
          id, type: "agent", position: pos(id), measured: dims[id], ...size(id),
          selected: sel?.kind === "node" && sel.id === id,
          data: { name: id, agent: a, start: s.start === id, error: errNodes.has(id), run: trace.nodes[id], ins, outs,
            style: nodeStyle, lib: a.origin ? val?.origins?.[id] : undefined,
            model: a.action || a.subflow ? null
              : a.model && a.model !== "inherit" ? { name: a.model, from: "agent" as const }
              : s.model ? { name: s.model, from: "flow" as const }
              : { name: catalog?.default_model ?? null, from: "studio" as const } },
        };
      }),
      ...Object.entries(s.gates ?? {}).map(([id, g]) => ({
        id, type: "gate", position: pos(id), measured: dims[id], ...size(id),
        selected: sel?.kind === "node" && sel.id === id,
        data: { name: id, gate: g, error: errNodes.has(id), run: trace.nodes[id], style: nodeStyle },
      })),
      // "always" in a workspace: the context update is the last (system) step of every run
      ...(catalog?.workspace && catalog.workspace.name !== "studio" && s.context_update === "always" ? [(() => {
        const ps = Object.values(s.layout ?? {});
        const x = ps.length ? Math.min(...ps.map((p) => p.x)) : 0;
        const y = ps.length ? Math.max(...ps.map((p) => p.y)) + 220 : 0;
        return { id: CONTEXT_NODE, type: "context", position: { x, y }, draggable: false, selectable: false,
          deletable: false, connectable: false,
          data: { run: trace.nodes[CONTEXT_NODE], label: t("ctx.nodeLabel"), hint: t("ctx.nodeHint"), badge: t("ctx.nodeBadge") } };
      })()] : []),
    ];
  }, [doc, sel, errNodes, dims, trace, nodeStyle, val, catalog, t]);
  const edges: Edge[] = useMemo(() => {
    if (!doc) return [];
    const s = doc.spec;
    const control: Edge[] = (s.edges ?? []).map((e, i) => {
      // run state of the link: followed (green; moving while its target runs), not followed (faded)
      const run = trace.edges[edgeKey(e.from, e.to)];
      // a branch label ("yes", "no") reads better than the expression; the expression stays in the panel
      const cond = e.label?.trim() || [e.if?.trim(), e.when?.trim()].filter(Boolean).join(" · ");
      const fromDecision = s.gates?.[e.from]?.kind === "decision";
      const mark = run === "followed" ? "✓" : run === "not" ? "✗" : run === "error" ? "!" : "";
      return {
        id: `e${i}`, source: e.from, target: e.to, sourceHandle: "c", targetHandle: "c",  // the control points (not a data port)
        label: [mark, cond].filter(Boolean).join(" ") || undefined,
        selected: sel?.kind === "edge" && sel.index === i, type: "smoothstep",
        markerEnd: { type: MarkerType.ArrowClosed },
        animated: run === "followed" && trace.nodes[e.to] === "active",
        ariaLabel: run ? t(`run.edge.${run}`) : undefined,
        className: [e.to === s.start || (e.from in (s.gates ?? {}) && !fromDecision) ? "back" : "",
          fromDecision ? (e.if?.trim() ? "branch" : "branch else") : "", e.if?.trim() ? "cond" : "",
          run ? `run-${run}` : ""].join(" ").trim(),
      };
    });
    if (!showData) return control;
    const data: Edge[] = [];
    for (const [an, a] of Object.entries(s.agents))
      for (const [n, i] of Object.entries(a.inputs ?? {})) {
        const ref = i.from ?? "";
        const root = ref.slice(0, ref.indexOf("."));
        const out = ref.slice(ref.indexOf(".") + 1);
        if (!ref.includes(".") || root === "input" || !s.agents[root]?.outputs?.[out]) continue;
        data.push({
          id: dataEdgeId(an, n), source: root, target: an, sourceHandle: `out:${out}`, targetHandle: `in:${n}`,
          className: "data", type: "default", selected: sel?.kind === "data" && sel.agent === an && sel.input === n,
        });
      }
    return [...control, ...data];
  }, [doc, sel, showData, trace, t]);

  const onNodesChange = useCallback((changes: NodeChange[]) => {
    const moves = changes.filter((c) => c.type === "position" && c.position);
    if (moves.length) update((d) => {
      d.spec.layout = d.spec.layout ?? {};
      for (const c of moves) if (c.type === "position" && c.position) {
        d.spec.layout[c.id] = { ...d.spec.layout[c.id], x: Math.round(c.position.x), y: Math.round(c.position.y) };
      }
    }, false);
    // the resize handles: the new size goes into the layout (with the position, which moves when
    // a node is resized from its top or left side)
    const sized = changes.filter((c) => c.type === "dimensions" && c.dimensions && c.resizing !== undefined);
    if (sized.length) update((d) => {
      d.spec.layout = d.spec.layout ?? {};
      for (const c of sized) if (c.type === "dimensions" && c.dimensions) {
        const w = Math.round(c.dimensions.width), h = Math.round(c.dimensions.height);
        // only a size the flow format accepts (40–4000 × 30–4000); anything else keeps the old one;
        // each node style keeps its own size
        if (w >= 40 && w <= 4000 && h >= 30 && h <= 4000) setSavedSize(d.spec, c.id, nodeStyle, { w, h });
      }
    }, false);
    const dimsCh = changes.filter((c) => c.type === "dimensions" && c.dimensions);
    if (dimsCh.length) setDims((p) => {
      const n = { ...p };
      for (const c of dimsCh) if (c.type === "dimensions" && c.dimensions) n[c.id] = c.dimensions;
      return n;
    });
    for (const c of changes) {
      if (c.type === "select" && c.selected) setSel({ kind: "node", id: c.id });
    }
  }, [update, nodeStyle]);
  const onEdgesChange = useCallback((changes: EdgeChange[]) => {
    for (const c of changes) {
      if (c.type !== "select" || !c.selected) continue;
      if (c.id.startsWith("d:")) setSel({ kind: "data", ...parseDataEdge(c.id) });
      else setSel({ kind: "edge", index: Number(c.id.slice(1)) });
    }
  }, []);
  // Delete on the canvas: one single change (one undo step) for nodes and links
  const onDelete = useCallback(({ nodes: ns, edges: es }: { nodes: Node[]; edges: Edge[] }) => {
    if (!doc) return;
    const ids = ns.map((n) => n.id).filter((id) => id !== doc.spec.start);
    if (ids.length < ns.length) flash(t("app.startNotDeleted"), true);
    const drop = new Set(es.filter((e) => e.id.startsWith("e")).map((e) => Number(e.id.slice(1))));
    const links = es.filter((e) => e.id.startsWith("d:")).map((e) => parseDataEdge(e.id));
    if (!ids.length && !drop.size && !links.length) return;
    update((d) => {
      d.spec.edges = (d.spec.edges ?? []).filter((_, i) => !drop.has(i));
      for (const l of links) { const i = d.spec.agents[l.agent]?.inputs?.[l.input]; if (i) delete i.from; }
      for (const id of ids) removeNode(d.spec, id);
    });
    setSel(null);
  }, [doc, update, t]);
  // Data link: an output port (out:<x>) onto an input port (in:<y>) sets that input's `from`;
  // onto "+ input" (in:+) or the node's top handle it creates an input named after the output.
  const isValidConnection: IsValidConnection = useCallback((c) => {
    if (!doc || c.source === c.target) return false;
    if (c.sourceHandle?.startsWith("out:")) return c.target in doc.spec.agents;
    return true;
  }, [doc]);
  const onConnect = useCallback((c: Connection) => {
    if (!doc || !c.source || !c.target || c.source === c.target) return;
    if (c.sourceHandle?.startsWith("out:")) {
      const out = c.sourceHandle.slice(4), target = c.target, ref = `${c.source}.${out}`;
      if (!(target in doc.spec.agents)) return flash(t("app.dataToGate"), true);
      const ins = doc.spec.agents[target].inputs ?? {};
      const th = c.targetHandle?.startsWith("in:") && c.targetHandle !== "in:+" ? c.targetHandle.slice(3) : null;
      if (th && ins[th]?.from === ref) return flash(t("app.edgeExists"), true);
      const name = th ?? uniqueKey(Object.keys(ins), out);
      update((d) => {
        const a = d.spec.agents[target];
        a.inputs = { ...a.inputs, [name]: { ...a.inputs?.[name], from: ref } };
      });
      if (!showData) flash(t("app.dataLinkHidden"));
      setSel({ kind: "data", agent: target, input: name });
      return;
    }
    if ((doc.spec.edges ?? []).some((e) => e.from === c.source && e.to === c.target)) return flash(t("app.edgeExists"), true);
    const index = (doc.spec.edges ?? []).length;
    update((d) => { d.spec.edges = [...(d.spec.edges ?? []), { from: c.source!, to: c.target!, when: "" }]; });
    setSel({ kind: "edge", index });
  }, [doc, update, t, showData]);

  // ---- palette
  const addNode = (kind: PaletteKind, at?: { x: number; y: number }) => {
    if (!doc) return;
    const id = uniqueName(doc.spec, PALETTE.find((p) => p.kind === kind)!.base);
    const p = at ?? rf.screenToFlowPosition({ x: window.innerWidth / 2 - 120, y: window.innerHeight / 2 - 50 });
    update((d) => {
      if (kind === "agent") d.spec.agents[id] = newAgent(t("default.agentDescription"), t("default.agentPrompt"));
      // an action node: a connector action run by the studio (no prompt, no model)
      else if (kind === "action") d.spec.agents[id] = { description: t("default.actionDescription"),
        action: { connector: Object.keys(catalog?.connections ?? {})[0] ?? "", name: "" }, outputs: {} };
      // a subflow node: another flow of this workspace as one step (graph mode)
      else if (kind === "subflow") {
        d.spec.agents[id] = { description: t("default.subflowDescription"),
          subflow: { flow: flows.find((f) => f.name !== current)?.name ?? "", inputs: {}, outputs: {} }, outputs: {} };
        if (d.spec.execution !== "graph") flash(t("app.subflowNeedsGraph"));
      }
      else d.spec.gates = { ...d.spec.gates, [id]: newGate(kind, t(`node.${kind}`)) };
      d.spec.layout = { ...d.spec.layout, [id]: { x: Math.round(p.x), y: Math.round(p.y) } };
    });
    setSel({ kind: "node", id });
  };
  const onDrop = (e: DragEvent) => {
    e.preventDefault();
    const at = rf.screenToFlowPosition({ x: e.clientX - 120, y: e.clientY - 40 });
    const kind = e.dataTransfer.getData("application/x-flow-node") as PaletteKind;
    if (kind) addNode(kind, at);
    const lib = library.find((l) => l.id === e.dataTransfer.getData("application/x-flow-library"));
    if (lib) insertLibrary(lib, at);
  };

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
  const arrange = () => { update((d) => autoLayout(d.spec, undefined, nodeStyle)); setTimeout(() => rf.fitView({ padding: 0.15 }), 50); };
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
  const createFlow = async (copyFrom?: string) => {
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
          validate: (v) => !FLOW_NAME.test(v.trim()) ? t("app.flowNameHint")
            : flows.some((f) => f.name === v.trim()) ? t("app.flowExists") : null },
        { key: "title", label: t("app.flowTitle"),
          value: copyFrom ? t("app.copySuffix", { title: doc?.spec.title ?? copyFrom }) : "" },
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

  if (!catalog) return <div className="loading">{t("app.loading")}</div>;
  const currentFlow = flows.find((f) => f.name === current);
  const errors = val?.errors ?? [];
  const warnings = val?.warnings ?? [];
  // windows over the page: in the flow editor and in the Board alike
  const windows = <>
    {showLibrary && <LibraryManager library={library} locations={catalog.locations ?? ["local"]}
        onChange={async () => setLibrary(await api.library())} onClose={() => setShowLibrary(false)} />}
    {showConns && <Connections onClose={() => {
        setShowConns(false);
        api.catalog().then(setCatalog).catch(() => undefined);
        if (doc && current) api.validate(current, tidy(doc)).then(setVal).catch(() => undefined);
      }} />}
    {showProviders && <Providers focus={showProviders.focus ?? null} onClose={() => {
        setShowProviders(null);
        api.catalog().then(setCatalog).catch(() => undefined);
        if (doc && current) api.validate(current, tidy(doc)).then(setVal).catch(() => undefined);
      }} />}  </>;
  const board = view === "board" && !!catalog.workspace;
  const header = (
    <header className="topbar">
      <WorkspaceBar current={catalog.workspace} beforeSwitch={confirmDiscard} onEnv={refreshEnv} flash={flash} />
      <button className={view === "board" ? "on" : ""} aria-pressed={view === "board"} title={t("board.toggleTitle")}
        onClick={() => setView(view === "board" ? "flow" : "board")}>{view === "board" ? t("board.toFlow") : t("board.toBoard")}</button>
      <span className="sep" />
      {view !== "board" && <label className="bar-label" htmlFor="flow-select">{t("app.flowLabel")}</label>}
      <select id="flow-select" value={current ?? ""} onChange={(e) => switchFlow(e.target.value)} hidden={view === "board"}>
        {flows.map((f) => <option key={f.name} value={f.name}>{f.title} ({f.name}){f.valid ? "" : " ⚠"}
          {f.public ? ` · ${t("loc.public")}` : f.location && f.location !== "local" && f.location !== "workspace" ? ` · ${f.location}` : ""}</option>)}
      </select>
      {currentFlow && !board && <button className="link-btn" title={t("loc.moveTitle")} onClick={moveFlow}>
        <LocationBadge loc={currentFlow.location} isPublic={currentFlow.public} /></button>}
      <button onClick={() => createFlow()}>{t("app.new")}</button>
      {!board && <>
      <button onClick={() => current && createFlow(current)} disabled={!current}>{t("app.duplicate")}</button>
      <button disabled={!current} title={t("copy.flowTitle")} aria-haspopup="menu"
        onClick={(e) => { const r = (e.currentTarget as HTMLElement).getBoundingClientRect(); flowCopyMenu(r.left, r.bottom + 2); }}>
        {t("copy.short")} ▾</button>
      <button className="danger" onClick={deleteFlow} disabled={!current}>{t("app.delete")}</button>
      <span className="sep" />
      <button onClick={undo} disabled={!past.current.length} title="Ctrl+Z">{t("app.undo")}</button>
      <button onClick={redo} disabled={!future.current.length} title="Ctrl+Y">{t("app.redo")}</button>
      <button onClick={arrange}>{t("app.arrange")}</button>
      <button className={showData ? "on" : ""} aria-pressed={showData} onClick={toggleData}
        title={t("app.dataLinksTitle")}>{t("app.dataLinks")}</button>
      </>}
      <button onClick={() => setShowConns(true)}>{t("app.connectors")}</button>
      <span className="grow" />
      {msg && <span className={`msg${msg.bad ? " bad" : ""}`}>{msg.text}</span>}
      {!board && <span className={`status ${val?.ok ? "ok" : "bad"}`} onClick={() => setTab("problems")}>
        {val?.ok ? t("app.valid") : tn("app.problems", errors.length)}
      </span>}
      {!board && <span className="panel-toggles" role="group" aria-label={t("app.panels")}>
        {([["pal", "panelLeft", "Ctrl+B"], ["bottom", "panelBottom", "Ctrl+J"], ["insp", "panelRight", "Ctrl+Alt+B"]] as const)
          .map(([p, icon, keys]) => (
            <button key={p} className={`icon-btn${hidden[p] ? "" : " on"}`} aria-pressed={!hidden[p]}
              title={`${t(`app.panel.${p}`)} (${keys})`} aria-label={t(`app.panel.${p}`)} onClick={() => togglePanel(p)}>
              <Icon id={icon} size={16} />
            </button>))}
      </span>}
      <select className="lang" value={lang} title={t("app.language")} aria-label={t("app.language")}
        onChange={(e) => setLang(e.target.value as Lang)}>
        {Object.entries(LANGS).map(([k, l]) => <option key={k} value={k}>{l.label}</option>)}
      </select>
      <select className="node-style" value={nodeStyle} title={t("app.nodeStyle")} aria-label={t("app.nodeStyle")}
        onChange={(e) => setNodeStyle(e.target.value as NodeStyle)}>
        {NODE_STYLES.map((k) => <option key={k} value={k}>{t(`app.nodeStyle.${k}`)}</option>)}
      </select>
      <select className="theme" value={theme} title={t("app.theme")} aria-label={t("app.theme")}
        onChange={(e) => setTheme(e.target.value as Theme)}>
        {(Object.keys(THEMES) as Theme[]).map((k) => <option key={k} value={k}>{t(`theme.${k}`)}</option>)}
      </select>
      {!board && <button className="primary" onClick={save} disabled={!dirty} title="Ctrl+S">
        {dirty ? t("app.save") : t("app.saved")}
      </button>}
    </header>
  );
  if (view === "board" && catalog.workspace) return (
    <div className="app board-mode">
      {header}
      <Board ws={catalog.workspace} flash={flash} onSaved={refreshEnv}
        onConnectors={() => setShowConns(true)}
        onCatalog={() => { api.catalog().then(setCatalog).catch(() => undefined); }}
        side={sizes.insp ?? DEFAULT_SIZES.insp} onSide={resize("insp")}
        flowsKey={flows.map((f) => f.name).join(",")} onFlowsChanged={refreshFlows} onNewFlow={() => createFlow()}
        onOpen={async (name) => { if (name === current || (await confirmDiscard())) { setCurrent(name); setView("flow"); } }}
        onRun={async (name) => {
          if (name !== current && !(await confirmDiscard())) return;
          setCurrent(name); setView("flow"); setRight("run");
        }} />
      {windows}
    </div>
  );
  return (
    <div className={`app${hidden.pal ? " hide-pal" : ""}${hidden.insp ? " hide-insp" : ""}${hidden.bottom ? " hide-bottom" : ""}`} style={{
      ...(sizes.pal ? { "--pal-w": `${sizes.pal}px` } : {}),
      ...(sizes.insp ? { "--insp-w": `${sizes.insp}px` } : {}),
      ...(sizes.bottom ? { "--bottom-h": `${sizes.bottom}px` } : {}),
    } as CSSProperties}>
      {header}

      <aside className="palette">
        <h2>{t("palette.add")}</h2>
        {PALETTE.map((p) => (
          <div key={p.kind} className={`pal ${p.kind}`} draggable title={t(p.hint)}
            onDragStart={(e) => { e.dataTransfer.setData("application/x-flow-node", p.kind); e.dataTransfer.effectAllowed = "move"; }}
            onClick={() => addNode(p.kind)}>
            <b>{t(p.label)}</b><span>{t(p.hint)}</span>
          </div>
        ))}
        <p className="muted small">{t("palette.help")}</p>
        <h2 title={t("palette.libraryHint")} className="pal-head">{t("palette.library")}
          <button className="small" onClick={() => setShowLibrary(true)} title={t("libm.manage")}>{t("libm.manage")}</button></h2>
        {library.length === 0 && <p className="muted small">{t("palette.libraryEmpty")}</p>}
        {library.map((l) => (
          <div key={l.id} className="pal lib" draggable title={`${l.description}\n${t("palette.libraryHint")}`}
            onDragStart={(e) => { e.dataTransfer.setData("application/x-flow-library", l.id); e.dataTransfer.effectAllowed = "copy"; }}
            onClick={() => insertLibrary(l)}>
            <b><AgentIcon icon={l.icon} size={14} /> {l.title}</b>
            <span>{l.description}</span>
            <span className="pal-src">{l.source === "core" ? t("palette.libCore") : l.source === "local" ? t("palette.libLocal") : l.source}</span>
          </div>
        ))}
        <h2>{t("palette.tools")}</h2>
        <ul className="catalog">
          {catalog.servers.map((s) => (
            <li key={s}><b>{s}</b>
              <ul>{catalog.tools.filter((t) => t.server === s).map((t) => <li key={t.id} title={t.description}>{t.name}</li>)}</ul>
            </li>
          ))}
        </ul>
      </aside>

      <main className={`canvas${assistBusy ? " busy" : ""}`} onDragOver={(e) => { e.preventDefault(); e.dataTransfer.dropEffect = "move"; }} onDrop={onDrop}
        onKeyDown={onCanvasKey}>
        {doc && (
          <ReactFlow nodes={nodes} edges={edges} nodeTypes={nodeTypes} colorMode={base}
            onNodesChange={onNodesChange} onEdgesChange={onEdgesChange} onConnect={onConnect} onDelete={onDelete}
            isValidConnection={isValidConnection}
            onNodeContextMenu={(e, n) => {
              e.preventDefault();
              if (fromKeyboard() || n.id === CONTEXT_NODE) return;
              const p = menuAt(e, e.currentTarget as Element); nodeMenu(n.id, p.x, p.y);
            }}
            onEdgeContextMenu={(e, ed) => {
              e.preventDefault();
              if (fromKeyboard()) return;
              const p = menuAt(e, e.currentTarget as Element); edgeMenu(ed.id, p.x, p.y);
            }}
            onPaneContextMenu={(e) => {
              e.preventDefault();
              if (fromKeyboard()) return;
              const p = menuAt(e); paneMenu(p.x, p.y);
            }}
            onBeforeDelete={async ({ nodes: ns, edges: es }) => ({ nodes: ns.filter((n) => n.id !== doc.spec.start), edges: es })}
            onNodeDragStart={() => { if (doc) { past.current.push(doc); future.current = []; } }}
            onPaneClick={() => setSel(null)} deleteKeyCode={["Delete", "Backspace"]}
            fitView minZoom={0.2} maxZoom={2} proOptions={{ hideAttribution: true }}>
            <Background gap={20} />
            <Controls />
            <MiniMap pannable zoomable style={{ width: 160, height: 110 }} />
          </ReactFlow>
        )}
        {menu && <ContextMenu menu={menu} onClose={closeMenu} />}
      </main>

      <aside className="right">
        <div className="right-tabs">
          <button className={right === "props" ? "on" : ""} onClick={() => setRight("props")}>{t("tabs.properties")}</button>
          <button className={right === "chat" ? "on" : ""} onClick={() => setRight("chat")}>
            {t("tabs.assistant")}{assistBusy ? " ●" : ""}</button>
          <button className={right === "run" ? "on" : ""} onClick={() => setRight("run")}
            title={runNeeds ? t("rp.needsYouBody", { n: String(runNeeds) }) : undefined}>
            {t("tabs.run")}{runNeeds > 0 && <span className="rp-count" aria-label={t("rp.needsYouBody", { n: String(runNeeds) })}>{runNeeds}</span>}</button>
        </div>
        {doc && <div className="right-body" hidden={right !== "props"}>
          <Inspector doc={doc} sel={sel} catalog={catalog} val={val} update={update} select={setSel} remove={remove}
            flows={flows.map((f) => f.name)}
            openProviders={(focus) => setShowProviders({ focus })}
            library={{ state: libState, update: updateFromLibrary, detach: detachFromLibrary, save: saveToLibrary,
              has: (lib) => library.some((l) => l.id === lib) }}
            testAgent={(id) => {
              const until = id.startsWith("until:");
              setRunReq({ mode: until ? "until" : "agent", target: until ? id.slice(6) : id, nonce: Date.now() });
              setRight("run");
            }} />
        </div>}
        {doc && current && <div className="right-body" hidden={right !== "chat"}>
          <Chat key={current} flow={current} lang={lang} doc={doc} onBusy={setAssistBusy}
            onLibrary={() => api.library().then(setLibrary).catch((e) => flash(String(e), true))} apply={(d) => {
            const n = ensureLayout(d, nodeStyle);
            update((x) => { x.spec = n.spec; x.prompts = n.prompts; });
          }} />
        </div>}
        {doc && current && <div className="right-body" hidden={right !== "run"}>
          <RunPanel key={current} flow={current} doc={doc} dirty={dirty} mock={catalog.mock} onTrace={setTrace} request={runReq}
            focus={sel?.kind === "node" ? sel.id : null} onFocusNode={(id) => setSel({ kind: "node", id })} onAttention={setRunNeeds}
            onOpenRun={async (f, input) => {  // a run of another flow that waits: switch to that flow, then open it
              if (f !== current && !(await confirmDiscard())) return;
              setCurrent(f); setRunReq({ mode: "full", open: input, flow: f, nonce: Date.now() }); setRight("run");
            }} />
        </div>}
      </aside>

      <footer className="bottom">
        <div className="tabs">
          <button className={tab === "problems" ? "on" : ""} onClick={() => setTab("problems")}>{t("bottom.problems", { n: errors.length })}{warnings.length ? ` · ⚠ ${warnings.length}` : ""}</button>
          <button className={tab === "flow" ? "on" : ""} onClick={() => setTab("flow")}>{t("bottom.flow")}</button>
          {val?.allowed_tools && <span className="muted small">{t("bottom.allowedTools", { n: val.allowed_tools.length })}</span>}
        </div>
        <div className="tab-body">
          {tab === "problems" && (errors.length === 0 && warnings.length === 0
            ? <p className="muted">{t("bottom.noProblems")} <code>agent-flow-studio-flows validate</code>.</p>
            : <ul className="problems">
              {errors.map((e, i) => (
                <li key={i} onClick={() => e.nodes[0] && setSel({ kind: "node", id: e.nodes[0] })} className={e.nodes.length ? "link" : ""}>
                  {e.loc && <code>{e.loc}</code>} {localizeServer(e, e.msg, lang)}
                </li>))}
              {warnings.map((e, i) => (
                <li key={`w${i}`} className="warn-text">
                  ⚠ {e.loc && <code>{e.loc}</code>} {localizeServer(e, e.msg, lang)}
                </li>))}
            </ul>)}
          {tab === "flow" && <pre className="mono">{val?.flow_section ?? t("bottom.flowPending")}</pre>}
        </div>
      </footer>
      <div className="split-host split-pal">
        <Splitter edge="right" size={sizes.pal ?? DEFAULT_SIZES.pal} onSize={resize("pal")} label={t("app.resizePanel")} />
      </div>
      <div className="split-host split-insp">
        <Splitter edge="left" size={sizes.insp ?? DEFAULT_SIZES.insp} onSize={resize("insp")} label={t("app.resizePanel")} />
      </div>
      <div className="split-host split-bottom">
        <Splitter edge="top" size={sizes.bottom ?? DEFAULT_SIZES.bottom} onSize={resize("bottom")} label={t("app.resizePanel")} />
      </div>
      {windows}

    </div>
  );
}

export default function App() {
  return <ReactFlowProvider><Editor /></ReactFlowProvider>;
}

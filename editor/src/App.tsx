import { localizeServer } from "./i18n/server";
import { useCallback, useEffect, useRef, useState, type CSSProperties, type DragEvent } from "react";
import { clampSize, DEFAULT_SIZES, loadSizes, saveSizes, Splitter, type PanelSizes } from "./Splitter";
import { Background, Controls, MiniMap, ReactFlow, ReactFlowProvider, useReactFlow, type Connection, type Edge, type EdgeChange, type IsValidConnection, type Node, type NodeChange } from "@xyflow/react";
import { api } from "./api";
import { Chat } from "./Chat";
import { Connections } from "./Connections";
import { Providers } from "./Providers";
import { ContextMenu, type MenuState } from "./ContextMenu";
import { useDialog } from "./Dialog";
import { Icon } from "./icons";
import { LibraryManager, locLabel } from "./LibraryManager";
import { parseRunLink } from "./runLink";
import { CONTEXT_NODE, RunPanel, type RunRequest, type Trace } from "./RunPanel";
import { Inspector } from "./Inspector";
import { Board } from "./Board";
import { useWorkspaceActions, WorkspaceBar } from "./Workspaces";
import { Prefs } from "./appParts/Prefs";
import { Palette } from "./appParts/Palette";
import { useI18n } from "./i18n";
import { useTheme } from "./theme";
import { clone, ensureLayout, NODE_STYLES, nodeNames, nodeSize, type NodeStyle, newAgent, newGate, removeNode, setSavedSize, tidy, uniqueKey, uniqueName } from "./model";
import { nodeTypes } from "./nodes";
import type { Catalog, Doc, FlowSummary, FlowTemplate, LibraryAgent, Selection, Validation } from "./types";
import { useCanvasGraph } from "./appParts/graph";
import { useNodeActions } from "./appParts/nodeActions";
import { useLibraryActions } from "./appParts/libraryActions";
import { useCanvasMenus } from "./appParts/menus";
import { useFlowFiles } from "./appParts/flowFiles";
import { type Clip, PALETTE, type PaletteKind, type Panel, DATA_LINKS_KEY, LAST_FLOW_KEY, VIEW_KEY, HIDDEN_KEY, parseDataEdge, LocationBadge } from "./appParts/shared";

/** Fit the flow at a zoom where the cards stay readable (0.8–1). */
const FIT = { padding: 0.15, minZoom: 0.8, maxZoom: 1 };
const WIDE_MIN = 0.65;  // the first view may go this low so a slightly wide flow shows whole

function Editor() {
  const rf = useReactFlow();
  const { t, tn, lang } = useI18n();
  const { theme, setTheme, base } = useTheme();
  // node style: a preference of this browser, not part of the flow
  const [nodeStyle, setNodeStyleState] = useState<NodeStyle>(() => {
    const v = localStorage.getItem("afs.nodeStyle");
    return NODE_STYLES.includes(v as NodeStyle) ? (v as NodeStyle) : "card";
  });
  const setNodeStyle = (s: NodeStyle) => {
    setNodeStyleState(s);
    try { localStorage.setItem("afs.nodeStyle", s); } catch { /* storage not available */ }
    setTimeout(() => rf.fitView(FIT), 80);
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
    if (v === "flow") setTimeout(() => rf.fitView(FIT), 80);
  };
  // panels hidden by the user (this browser): palette, right panel, bottom panel
  const [hidden, setHidden] = useState<Record<Panel, boolean>>(() => {
    try { return { pal: false, insp: false, bottom: true, ...JSON.parse(localStorage.getItem(HIDDEN_KEY) ?? "{}") }; }
    catch { return { pal: false, insp: false, bottom: true }; }
  });
  const togglePanel = (p: Panel, show?: boolean) => setHidden((h) => {
    const n = { ...h, [p]: show === undefined ? !h[p] : !show };
    if (n[p] === h[p]) return h;
    try { localStorage.setItem(HIDDEN_KEY, JSON.stringify(n)); } catch { /* storage not available */ }
    setTimeout(() => rf.fitView({ ...FIT, duration: 150 }), 60);
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
  const [runNeeds, setRunNeeds] = useState(0);
  const [runState, setRunState] = useState<"idle" | "running" | "gate">("idle");  // gates and approvals that wait in the Run panel
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
  const kbMenu = useRef(0);
  const runFlowRef = useRef<() => void>(() => undefined);  // time of the last keyboard-opened menu (ignore the browser's own event)

  const dirty = doc !== null && JSON.stringify(doc) !== saved;
  useEffect(() => {
    document.title = `${dirty ? "• " : ""}${current ? `${current} — ` : ""}Agent Flow Studio`;
  }, [dirty, current]);
  const flash = useCallback((text: string, bad = false) => {
    setMsg({ text, bad }); setTimeout(() => setMsg(null), bad ? 8000 : 6000);
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
      setTimeout(() => {
        // the first view, from the flow's own layout (React Flow may still hold the previous flow):
        // all of it at a readable zoom (0.8–1); a flow too big for that shows its start at the top
        const el = document.querySelector(".canvas"), names = nodeNames(d.spec);
        const boxes = names.map((n) => { const p = d.spec.layout?.[n] ?? { x: 0, y: 0 }; const z = nodeSize(d.spec, n, nodeStyle); return { x: p.x, y: p.y, w: z.w, h: z.h }; });
        if (!el || !boxes.length) { rf.fitView(FIT); return; }
        const x0 = Math.min(...boxes.map((b) => b.x)), x1 = Math.max(...boxes.map((b) => b.x + b.w));
        const y0 = Math.min(...boxes.map((b) => b.y)), y1 = Math.max(...boxes.map((b) => b.y + b.h));
        const W = el.clientWidth, H = el.clientHeight, M = 48;  // M: room for the canvas tools and the edges
        const fitW = (W - M) / (x1 - x0), fitH = (H - M - 56) / (y1 - y0);
        // readable (≥ 0.8) when it fits; a flow a little too wide still shows all its width, down to
        // 0.65 (the person sees the whole flow first); wider than that, it starts at its start node
        const zoom = fitW < FIT.minZoom && fitW >= WIDE_MIN ? fitW : Math.max(FIT.minZoom, Math.min(FIT.maxZoom, fitW, fitH));
        const start = d.spec.layout?.[d.spec.start];
        const x = W / 2 - ((x0 + x1) / 2) * zoom;  // centred across (it fits at 0.8 unless it is very wide)
        const y = fitH >= zoom || !start ? 56 + (H - 56) / 2 - ((y0 + y1) / 2) * zoom : 56 - start.y * zoom;
        rf.setViewport({ x, y, zoom });
      }, 50);
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
        setTab("problems"); togglePanel("bottom", true);
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
      } else { setTab("problems"); togglePanel("bottom", true); flash(r.blocked_by_secret ? t("app.notSavedSecret") : t("app.notSaved"), true); }
    } catch (e) { flash(String(e), true); }
  }, [doc, current, refreshFlows, t]);

  // ---- shortcuts
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      const mod = e.ctrlKey || e.metaKey;
      if (mod && e.key === "s") { e.preventDefault(); save(); }
      if (mod && e.key === "Enter" && !(e.target as HTMLElement)?.closest?.("input,textarea,select,.dialog-layer,.ctx-menu,.modal,.right")) {
        e.preventDefault(); runFlowRef.current();
      }
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

  // what the parts in appParts/ use (see EditorCtx); later actions join it as they are made
  const ctx = { catalog, clip, copyAgent, current, dialog, dims, dirty, doc, flash, flows, kbMenu, library, nodeStyle, refreshFlows, remove, rf, sel, setCurrent, setDoc, setLibrary, setMenu, setRight, setRunReq, setSel, setView, showData, t, templates, trace, update, val };
  const { nodes, edges } = useCanvasGraph(ctx);
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

  const { showProps, requestRun, copyNode, pasteNode, duplicateNode, modelGrid, renameDialog, addPort } = useNodeActions(ctx);
  const { insertLibrary, updateFromLibrary, detachFromLibrary, saveToLibrary, libState } = useLibraryActions({ ...ctx, showProps });
  const { menuAt, nodeMenu, edgeMenu, paneMenu, arrange, fromKeyboard, onCanvasKey } = useCanvasMenus({ ...ctx, addNode, addPort, copyNode, detachFromLibrary, duplicateNode, libState, modelGrid, pasteNode, renameDialog, requestRun, saveToLibrary, showProps, updateFromLibrary });
  const { confirmDiscard, switchFlow, createFlow, deleteFlow, moveFlow } = useFlowFiles(ctx);
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
  const envKind = catalog.workspace && (catalog.workspace.environments?.length ?? 0) > 0 ? catalog.workspace.kind ?? "dev" : null;
  const statusKind = !val ? "pending" : !val.ok ? "bad" : warnings.length ? "warn" : "ok";
  const runShown = runNeeds > 0 || runState === "gate" ? "gate" : runState;
  const runFlow = () => {
    if (runShown === "idle") {
      setRunReq({ mode: "full", nonce: Date.now() });
      let tries = 0;  // the form appears a moment later: focus its first input once it is there
      const focusFirst = () => {
        const el = document.querySelector<HTMLElement>(
          "#rp-view-setup fieldset :is(input:not([type=hidden]), textarea, select)");  // the flow's inputs
        if (el && document.activeElement !== el) el.focus();
        if ((!el || document.activeElement !== el) && ++tries < 12) setTimeout(focusFirst, 80);
      };
      setTimeout(focusFirst, 60);
    }
    setRight("run");
  };
  const flowMenu = (x: number, y: number) => setMenu({ x, y, title: current ?? "", items: [
    ...(currentFlow ? [{ label: t("loc.menuItem", { place: currentFlow.public ? t("loc.public") : locLabel(t, currentFlow.location ?? "local") }), onSelect: moveFlow },
      { kind: "sep" as const }] : []),
    { label: t("app.duplicate"), onSelect: () => current && createFlow(current) },
    { label: t("copy.button"), title: t("copy.flowTitle"), onSelect: () => copyFlow("copy") },
    { label: t("copy.moveButton"), title: t("copy.moveFlowTitle"), onSelect: () => copyFlow("move") },
    { label: t("loc.moveMenu"), title: t("loc.moveTitle"), onSelect: moveFlow },
    { kind: "sep" },
    { label: t("app.deleteFlow"), danger: true, onSelect: deleteFlow },
  ] });
  runFlowRef.current = runFlow;
  const header = (
    <header className={`topbar${envKind ? ` env-${envKind}` : ""}`}>
      <div className="tb-zone">
        <WorkspaceBar current={catalog.workspace} beforeSwitch={confirmDiscard} onEnv={refreshEnv} flash={flash} />
        <span className="sep" />
        <span className="tb-seg" role="group" aria-label={t("board.viewLabel")}>
          <button className={view === "board" ? "on" : ""} aria-pressed={view === "board"} title={t("board.toggleTitle")}
            onClick={() => setView("board")}>{t("board.toBoard")}</button>
          <button className={view !== "board" ? "on" : ""} aria-pressed={view !== "board"} onClick={() => setView("flow")}>{t("board.toFlow")}</button>
        </span>
        {!board && <>
          <span className="sep" />
          <span className="tb-flow">
            {dirty && <span className="dirty-dot" aria-hidden="true" title={t("app.unsaved")} />}
            <select id="flow-select" value={current ?? ""} aria-label={t("app.flowLabel")} aria-describedby={dirty ? "flow-dirty" : undefined}
              onChange={(e) => switchFlow(e.target.value)}>
              {flows.map((f) => <option key={f.name} value={f.name} title={f.name}>{f.title}{f.valid ? "" : " ⚠"}
                {f.public ? ` · ${t("loc.public")}` : f.location && f.location !== "local" && f.location !== "workspace" ? ` · ${f.location}` : ""}</option>)}
            </select>
            {dirty && <span id="flow-dirty" className="sr-only">{t("app.unsaved")}</span>}
          </span>
          <button className="tb-flowmenu" aria-label={t("app.flowActions")} title={t("app.flowActions")} aria-haspopup="menu" disabled={!current}
            onClick={(e) => { const r = (e.currentTarget as HTMLElement).getBoundingClientRect(); flowMenu(r.left, r.bottom + 2); }}>
            <span className="tb-flowmenu-text" aria-hidden="true">{t("app.flowMenu")} </span>▾</button>
          {currentFlow && <button className="tb-loc" title={t("loc.moveTitle")} onClick={moveFlow}
            aria-label={t("loc.chipLabel", { place: currentFlow.public ? t("loc.public") : locLabel(t, currentFlow.location ?? "local") })}>
            <LocationBadge loc={currentFlow.location} isPublic={currentFlow.public} /></button>}
          <button className={`tb-status ${statusKind}`} data-status={statusKind} onClick={() => { setTab("problems"); togglePanel("bottom", true); }}>
            {statusKind === "bad" ? <>✕ {tn("app.problems", errors.length)}</>
              : statusKind === "warn" ? <>⚠ {tn("app.warnings", warnings.length)}</>
              : statusKind === "pending" ? t("app.checking") : t("app.valid")}
          </button>
        </>}
      </div>
      <span className="grow" />
      <div className="tb-zone">
        <button onClick={() => createFlow()}>{t("app.new")}</button>
        {!board && <>
          <span className="sep" />
          <span className="tb-history" role="group" aria-label={t("app.history")}>
            <button onClick={undo} disabled={!past.current.length} aria-label={t("app.undo")} title={t("app.undoTitle")}
              aria-keyshortcuts="Control+Z"><Icon id="undo" size={16} /></button>
            <button onClick={redo} disabled={!future.current.length} aria-label={t("app.redo")} title={t("app.redoTitle")}
              aria-keyshortcuts="Control+Y Control+Shift+Z"><Icon id="redo" size={16} /></button>
          </span>
          {dirty
            ? <button className="primary tb-save" onClick={save} aria-keyshortcuts="Control+S" title={t("app.saveTitle")}>
                {t("app.save")}<kbd aria-hidden="true">Ctrl+S</kbd></button>
            : <button className="tb-saved" aria-disabled="true" title={t("app.allSaved")} onClick={() => undefined}>✓ {t("app.saved")}</button>}
          <button className={`tb-run btn-run ${runShown}`} onClick={runFlow} aria-keyshortcuts="Control+Enter"
            aria-label={runShown === "running" ? t("app.runningLabel") : runShown === "gate" ? t("app.needsYouLabel") : t("app.runFlow")}
            title={runShown === "idle" ? t("app.runFlowTitle") : t("app.openRun")}>
            <span aria-hidden="true">{runShown === "running" ? "●" : runShown === "gate" ? "?" : "▶"}</span>
            {runShown === "running" ? t("app.running") : runShown === "gate" ? t("app.needsYou") : t("app.runFlow")}</button>
        </>}
        <button className="tb-connmenu" aria-haspopup="menu" aria-label={t("app.connections")} title={t("app.connectionsTitle")}
          onClick={(e) => { const r = (e.currentTarget as HTMLElement).getBoundingClientRect(); setMenu({ x: r.right - 180, y: r.bottom + 2, title: t("app.connections"), items: [
            { label: `${t("app.connectors")}…`, onSelect: () => setShowConns(true) },
            { label: `${t("app.providers")}…`, onSelect: () => setShowProviders({}) },
          ] }); }}>{t("app.connections")} ▾</button>
        <button className="tb-conn" onClick={() => setShowConns(true)} aria-label={t("app.connectors")} title={t("app.connectors")}>
          <Icon id="database" size={16} /><span className="tb-conn-text" aria-hidden="true">{t("app.connectors")}</span></button>
        <button className="tb-conn" onClick={() => setShowProviders({})} aria-label={t("app.providers")} title={t("app.providersTitle")}>
          <Icon id="cloud" size={16} /><span className="tb-conn-text" aria-hidden="true">{t("app.providers")}</span></button>
        <Prefs theme={theme} setTheme={setTheme} nodeStyle={nodeStyle} setNodeStyle={setNodeStyle}
          panels={board ? null : hidden} togglePanel={togglePanel} />
      </div>
      <div className="sr-only" role="status" aria-live="polite">{msg?.text ?? ""}</div>
      {msg && <span className={`msg tb-toast${msg.bad ? " bad" : ""}`} aria-hidden="true">{msg.text}</span>}
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

      <Palette catalog={catalog} library={library} addNode={(k) => addNode(k)} insertLibrary={(l) => insertLibrary(l)}
        manage={() => setShowLibrary(true)} />

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
            minZoom={0.2} maxZoom={2} proOptions={{ hideAttribution: true }}>
            <Background gap={20} />
            <Controls />
            <MiniMap pannable zoomable style={{ width: 160, height: 110 }} />
          </ReactFlow>
        )}
        {doc && <div className="canvas-tools" role="toolbar" aria-label={t("app.canvasTools")}>
          <button onClick={arrange}>{t("app.arrange")}</button>
          <button className={showData ? "on" : ""} aria-pressed={showData} onClick={toggleData}
            title={t("app.dataLinksTitle")}>{t("app.dataLinks")}</button>
        </div>}
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
            onRunState={setRunState}
            onRetrySetting={() => {  // the retry card's link: Properties of the flow, at its Retry field
              setSel(null); setRight("props");
              setTimeout(() => {
                const el = document.getElementById("flow-retry");
                el?.scrollIntoView({ block: "center" });
                el?.querySelector<HTMLElement>("input")?.focus();
              }, 60);
            }}
            onOpenRun={async (f, input) => {  // a run of another flow that waits: switch to that flow, then open it
              if (f !== current && !(await confirmDiscard())) return;
              setCurrent(f); setRunReq({ mode: "full", open: input, flow: f, nonce: Date.now() }); setRight("run");
            }} />
        </div>}
      </aside>

      <footer className={`bottom${hidden.bottom && errors.length ? " has-bad" : hidden.bottom && warnings.length ? " has-warn" : ""}`}>
        <div className="tabs">
          <button className={tab === "problems" && !hidden.bottom ? "on" : ""} onClick={() => { setTab("problems"); togglePanel("bottom", true); }}>
            {t("bottom.problemsTab")}
            {errors.length > 0 && <span className="cnt bad" aria-label={tn("app.problems", errors.length)}>✕ {errors.length}</span>}
            {warnings.length > 0 && <span className="cnt warn" aria-label={tn("app.warnings", warnings.length)}>⚠ {warnings.length}</span>}</button>
          {doc?.spec.execution !== "graph" && <button className={tab === "flow" && !hidden.bottom ? "on" : ""} onClick={() => { setTab("flow"); togglePanel("bottom", true); }}>{t("bottom.flow")}</button>}
          {val?.allowed_tools && <span className="muted small" title={t("bottom.allowedToolsTitle")}>{t("bottom.allowedTools", { n: val.allowed_tools.length })}</span>}
          <span className="grow" />
          <button className="icon-btn bottom-toggle" aria-expanded={!hidden.bottom} aria-keyshortcuts="Control+J"
            aria-label={hidden.bottom ? t("bottom.open") : t("bottom.close")} title={`${hidden.bottom ? t("bottom.open") : t("bottom.close")} (Ctrl+J)`}
            onClick={() => togglePanel("bottom")}>{hidden.bottom ? "▴" : "▾"}</button>
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
          {tab === "flow" && doc?.spec.execution !== "graph" && <pre className="mono">{val?.flow_section ?? t("bottom.flowPending")}</pre>}
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

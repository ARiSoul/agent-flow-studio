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
import { AgentIcon, Icon } from "./icons";
import { LibraryManager } from "./LibraryManager";
import { parseRunLink } from "./runLink";
import { CONTEXT_NODE, RunPanel, type RunRequest, type Trace } from "./RunPanel";
import { Inspector } from "./Inspector";
import { Board } from "./Board";
import { useWorkspaceActions, WorkspaceBar } from "./Workspaces";
import { LANGS, useI18n, type Lang } from "./i18n";
import { THEMES, useTheme, type Theme } from "./theme";
import { clone, ensureLayout, NODE_STYLES, type NodeStyle, newAgent, newGate, removeNode, setSavedSize, tidy, uniqueKey, uniqueName } from "./model";
import { nodeTypes } from "./nodes";
import type { Catalog, Doc, FlowSummary, FlowTemplate, LibraryAgent, Selection, Validation } from "./types";
import { useCanvasGraph } from "./appParts/graph";
import { useNodeActions } from "./appParts/nodeActions";
import { useLibraryActions } from "./appParts/libraryActions";
import { useCanvasMenus } from "./appParts/menus";
import { useFlowFiles } from "./appParts/flowFiles";
import { type Clip, PALETTE, type PaletteKind, type Panel, DATA_LINKS_KEY, LAST_FLOW_KEY, VIEW_KEY, HIDDEN_KEY, parseDataEdge, LocationBadge } from "./appParts/shared";

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

import { useCallback, useEffect, useMemo, useRef, useState, type DragEvent } from "react";
import {
  Background, Controls, MiniMap, ReactFlow, ReactFlowProvider, useReactFlow, MarkerType,
  type Connection, type Edge, type EdgeChange, type Node, type NodeChange,
} from "@xyflow/react";
import { api } from "./api";
import { Chat } from "./Chat";
import { Connections } from "./Connections";
import { RunPanel, type NodeRun, type RunRequest } from "./RunPanel";
import { Inspector } from "./Inspector";
import { LANGS, useI18n, type Lang } from "./i18n";
import { THEMES, useTheme, type Theme } from "./theme";
import { autoLayout, clone, ensureLayout, newAgent, newGate, removeNode, tidy, uniqueName } from "./model";
import { nodeTypes } from "./nodes";
import type { Catalog, Doc, FlowSummary, Selection, Validation } from "./types";

const PALETTE = [
  { kind: "agent", label: "palette.agent", hint: "palette.agentHint", base: "agent" },
  { kind: "human", label: "palette.human", hint: "palette.humanHint", base: "approval" },
  { kind: "stop", label: "palette.stop", hint: "palette.stopHint", base: "stop" },
] as const;
type PaletteKind = (typeof PALETTE)[number]["kind"];

function Editor() {
  const rf = useReactFlow();
  const { t, tn, lang, setLang } = useI18n();
  const { theme, setTheme, base } = useTheme();
  const [catalog, setCatalog] = useState<Catalog | null>(null);
  const [flows, setFlows] = useState<FlowSummary[]>([]);
  const [current, setCurrent] = useState<string | null>(null);
  const [doc, setDoc] = useState<Doc | null>(null);
  const [saved, setSaved] = useState<string>("");
  const [sel, setSel] = useState<Selection>(null);
  const [val, setVal] = useState<Validation | null>(null);
  const [tab, setTab] = useState<"problems" | "flow">("problems");
  const [msg, setMsg] = useState<{ text: string; bad?: boolean } | null>(null);
  const [right, setRight] = useState<"props" | "chat" | "run">("props");
  const [trace, setTrace] = useState<Record<string, NodeRun>>({});
  const [assistBusy, setAssistBusy] = useState(false);
  const [showConns, setShowConns] = useState(false);
  const [runReq, setRunReq] = useState<RunRequest | null>(null);
  const [dims, setDims] = useState<Record<string, { width: number; height: number }>>({});
  const past = useRef<Doc[]>([]);
  const future = useRef<Doc[]>([]);

  const dirty = doc !== null && JSON.stringify(doc) !== saved;
  const flash = (text: string, bad = false) => { setMsg({ text, bad }); setTimeout(() => setMsg(null), 4000); };

  // ---- load
  const refreshFlows = useCallback(() => api.flows().then(setFlows), []);
  useEffect(() => {
    api.catalog().then(setCatalog).catch((e) => flash(String(e), true));
    refreshFlows().then(() => undefined);
  }, [refreshFlows]);
  useEffect(() => {
    if (!current && flows.length) setCurrent(flows[0].name);
  }, [flows, current]);
  useEffect(() => {
    if (!current) return;
    api.load(current).then((r) => {
      const d = ensureLayout({ spec: r.spec, prompts: r.prompts });
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
    } else update((d) => { d.spec.edges = (d.spec.edges ?? []).filter((_, i) => i !== s.index); });
    setSel(null);
  }, [doc, update, t]);

  const save = useCallback(async () => {
    if (!doc || !current) return;
    try {
      const r = await api.save(current, tidy(doc));
      setVal(r);
      if (r.saved) { setSaved(JSON.stringify(doc)); flash(t("app.savedTo", { name: current })); refreshFlows(); }
      else { setTab("problems"); flash(t("app.notSaved"), true); }
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
    const s = doc.spec, pos = (id: string) => s.layout?.[id] ?? { x: 0, y: 0 };
    return [
      ...Object.entries(s.agents).map(([id, a]) => ({
        id, type: "agent", position: pos(id), measured: dims[id],
        selected: sel?.kind === "node" && sel.id === id,
        data: { name: id, agent: a, start: s.start === id, error: errNodes.has(id), run: trace[id] },
      })),
      ...Object.entries(s.gates ?? {}).map(([id, g]) => ({
        id, type: "gate", position: pos(id), measured: dims[id],
        selected: sel?.kind === "node" && sel.id === id,
        data: { name: id, gate: g, error: errNodes.has(id), run: trace[id] },
      })),
    ];
  }, [doc, sel, errNodes, dims, trace]);
  const edges: Edge[] = useMemo(() => (doc?.spec.edges ?? []).map((e, i) => ({
    id: `e${i}`, source: e.from, target: e.to, label: e.when || undefined,
    selected: sel?.kind === "edge" && sel.index === i, type: "smoothstep",
    markerEnd: { type: MarkerType.ArrowClosed },
    className: e.to === doc?.spec.start || e.from in (doc?.spec.gates ?? {}) ? "back" : "",
  })), [doc, sel]);

  const onNodesChange = useCallback((changes: NodeChange[]) => {
    const moves = changes.filter((c) => c.type === "position" && c.position);
    if (moves.length) update((d) => {
      d.spec.layout = d.spec.layout ?? {};
      for (const c of moves) if (c.type === "position" && c.position) d.spec.layout[c.id] = { x: Math.round(c.position.x), y: Math.round(c.position.y) };
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
  }, [update]);
  const onEdgesChange = useCallback((changes: EdgeChange[]) => {
    for (const c of changes) {
      if (c.type === "select" && c.selected) setSel({ kind: "edge", index: Number(c.id.slice(1)) });
    }
  }, []);
  // Delete on the canvas: one single change (one undo step) for nodes and links
  const onDelete = useCallback(({ nodes: ns, edges: es }: { nodes: Node[]; edges: Edge[] }) => {
    if (!doc) return;
    const ids = ns.map((n) => n.id).filter((id) => id !== doc.spec.start);
    if (ids.length < ns.length) flash(t("app.startNotDeleted"), true);
    const drop = new Set(es.map((e) => Number(e.id.slice(1))));
    if (!ids.length && !drop.size) return;
    update((d) => {
      d.spec.edges = (d.spec.edges ?? []).filter((_, i) => !drop.has(i));
      for (const id of ids) removeNode(d.spec, id);
    });
    setSel(null);
  }, [doc, update, t]);
  const onConnect = useCallback((c: Connection) => {
    if (!doc || !c.source || !c.target || c.source === c.target) return;
    if ((doc.spec.edges ?? []).some((e) => e.from === c.source && e.to === c.target)) return flash(t("app.edgeExists"), true);
    const index = (doc.spec.edges ?? []).length;
    update((d) => { d.spec.edges = [...(d.spec.edges ?? []), { from: c.source!, to: c.target!, when: "" }]; });
    setSel({ kind: "edge", index });
  }, [doc, update, t]);

  // ---- palette
  const addNode = (kind: PaletteKind, at?: { x: number; y: number }) => {
    if (!doc) return;
    const id = uniqueName(doc.spec, PALETTE.find((p) => p.kind === kind)!.base);
    const p = at ?? rf.screenToFlowPosition({ x: window.innerWidth / 2 - 120, y: window.innerHeight / 2 - 50 });
    update((d) => {
      if (kind === "agent") d.spec.agents[id] = newAgent(t("default.agentDescription"), t("default.agentPrompt"));
      else d.spec.gates = { ...d.spec.gates, [id]: newGate(kind, t(kind === "human" ? "node.human" : "node.stop")) };
      d.spec.layout = { ...d.spec.layout, [id]: { x: Math.round(p.x), y: Math.round(p.y) } };
    });
    setSel({ kind: "node", id });
  };
  const onDrop = (e: DragEvent) => {
    e.preventDefault();
    const kind = e.dataTransfer.getData("application/x-flow-node") as PaletteKind;
    if (kind) addNode(kind, rf.screenToFlowPosition({ x: e.clientX - 120, y: e.clientY - 40 }));
  };

  // ---- flow management
  const confirmDiscard = () => !dirty || confirm(t("app.discard"));
  const switchFlow = (name: string) => { if (confirmDiscard()) setCurrent(name); };
  const createFlow = async (copyFrom?: string) => {
    if (!confirmDiscard()) return;
    const name = prompt(copyFrom ? t("app.newFlowCopy", { name: copyFrom }) : t("app.newFlowName"), "");
    if (!name) return;
    const title = prompt(t("app.title"), copyFrom ? t("app.copySuffix", { title: doc?.spec.title ?? copyFrom }) : name) || name;
    try { await api.create(name, title, copyFrom); await refreshFlows(); setCurrent(name); flash(t("app.created", { name })); }
    catch (e) { flash(String(e), true); }
  };
  const deleteFlow = async () => {
    if (!current || !confirm(t("app.confirmDelete", { name: current }))) return;
    try {
      const r = await api.remove(current);
      flash(t("app.trashed", { path: r.trashed }));
      setCurrent(null); setDoc(null);
      await refreshFlows();
    } catch (e) { flash(String(e), true); }
  };

  if (!catalog) return <div className="loading">{t("app.loading")}</div>;
  const errors = val?.errors ?? [];
  return (
    <div className="app">
      <header className="topbar">
        <span className="brand">{t("app.brand")}</span>
        <select value={current ?? ""} onChange={(e) => switchFlow(e.target.value)}>
          {flows.map((f) => <option key={f.name} value={f.name}>{f.title} ({f.name}){f.valid ? "" : " ⚠"}</option>)}
        </select>
        <button onClick={() => createFlow()}>{t("app.new")}</button>
        <button onClick={() => current && createFlow(current)} disabled={!current}>{t("app.duplicate")}</button>
        <button className="danger" onClick={deleteFlow} disabled={!current}>{t("app.delete")}</button>
        <span className="sep" />
        <button onClick={undo} disabled={!past.current.length} title="Ctrl+Z">{t("app.undo")}</button>
        <button onClick={redo} disabled={!future.current.length} title="Ctrl+Y">{t("app.redo")}</button>
        <button onClick={() => { update((d) => autoLayout(d.spec)); setTimeout(() => rf.fitView({ padding: 0.15 }), 50); }}>{t("app.arrange")}</button>
        <button onClick={() => setShowConns(true)}>{t("app.connectors")}</button>
        <span className="grow" />
        {msg && <span className={`msg${msg.bad ? " bad" : ""}`}>{msg.text}</span>}
        <span className={`status ${val?.ok ? "ok" : "bad"}`} onClick={() => setTab("problems")}>
          {val?.ok ? t("app.valid") : tn("app.problems", errors.length)}
        </span>
        <select className="lang" value={lang} title={t("app.language")} aria-label={t("app.language")}
          onChange={(e) => setLang(e.target.value as Lang)}>
          {Object.entries(LANGS).map(([k, l]) => <option key={k} value={k}>{l.label}</option>)}
        </select>
        <select className="theme" value={theme} title={t("app.theme")} aria-label={t("app.theme")}
          onChange={(e) => setTheme(e.target.value as Theme)}>
          {(Object.keys(THEMES) as Theme[]).map((k) => <option key={k} value={k}>{t(`theme.${k}`)}</option>)}
        </select>
        <button className="primary" onClick={save} disabled={!dirty} title="Ctrl+S">
          {dirty ? t("app.save") : t("app.saved")}
        </button>
      </header>

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
        <h2>{t("palette.tools")}</h2>
        <ul className="catalog">
          {catalog.servers.map((s) => (
            <li key={s}><b>{s}</b>
              <ul>{catalog.tools.filter((t) => t.server === s).map((t) => <li key={t.id} title={t.description}>{t.name}</li>)}</ul>
            </li>
          ))}
        </ul>
      </aside>

      <main className={`canvas${assistBusy ? " busy" : ""}`} onDragOver={(e) => { e.preventDefault(); e.dataTransfer.dropEffect = "move"; }} onDrop={onDrop}>
        {doc && (
          <ReactFlow nodes={nodes} edges={edges} nodeTypes={nodeTypes} colorMode={base}
            onNodesChange={onNodesChange} onEdgesChange={onEdgesChange} onConnect={onConnect} onDelete={onDelete}
            onBeforeDelete={async ({ nodes: ns, edges: es }) => ({ nodes: ns.filter((n) => n.id !== doc.spec.start), edges: es })}
            onNodeDragStart={() => { if (doc) { past.current.push(doc); future.current = []; } }}
            onPaneClick={() => setSel(null)} deleteKeyCode={["Delete", "Backspace"]}
            fitView minZoom={0.2} maxZoom={2} proOptions={{ hideAttribution: true }}>
            <Background gap={20} />
            <Controls />
            <MiniMap pannable zoomable style={{ width: 160, height: 110 }} />
          </ReactFlow>
        )}
      </main>

      <aside className="right">
        <div className="right-tabs">
          <button className={right === "props" ? "on" : ""} onClick={() => setRight("props")}>{t("tabs.properties")}</button>
          <button className={right === "chat" ? "on" : ""} onClick={() => setRight("chat")}>
            {t("tabs.assistant")}{assistBusy ? " ●" : ""}</button>
          <button className={right === "run" ? "on" : ""} onClick={() => setRight("run")}>{t("tabs.run")}</button>
        </div>
        {doc && <div className="right-body" hidden={right !== "props"}>
          <Inspector doc={doc} sel={sel} catalog={catalog} update={update} select={setSel} remove={remove}
            testAgent={(id) => {
              const until = id.startsWith("until:");
              setRunReq({ mode: until ? "until" : "agent", target: until ? id.slice(6) : id, nonce: Date.now() });
              setRight("run");
            }} />
        </div>}
        {doc && current && <div className="right-body" hidden={right !== "chat"}>
          <Chat key={current} flow={current} lang={lang} doc={doc} onBusy={setAssistBusy} apply={(d) => {
            const n = ensureLayout(d);
            update((x) => { x.spec = n.spec; x.prompts = n.prompts; });
          }} />
        </div>}
        {doc && current && <div className="right-body" hidden={right !== "run"}>
          <RunPanel key={current} flow={current} doc={doc} dirty={dirty} mock={catalog.mock} onTrace={setTrace} request={runReq} />
        </div>}
      </aside>

      <footer className="bottom">
        <div className="tabs">
          <button className={tab === "problems" ? "on" : ""} onClick={() => setTab("problems")}>{t("bottom.problems", { n: errors.length })}</button>
          <button className={tab === "flow" ? "on" : ""} onClick={() => setTab("flow")}>{t("bottom.flow")}</button>
          {val?.allowed_tools && <span className="muted small">{t("bottom.allowedTools", { n: val.allowed_tools.length })}</span>}
        </div>
        <div className="tab-body">
          {tab === "problems" && (errors.length === 0
            ? <p className="muted">{t("bottom.noProblems")} <code>agent-flow-studio-flows validate</code>.</p>
            : <ul className="problems">{errors.map((e, i) => (
              <li key={i} onClick={() => e.nodes[0] && setSel({ kind: "node", id: e.nodes[0] })} className={e.nodes.length ? "link" : ""}>
                {e.loc && <code>{e.loc}</code>} {e.msg}
              </li>))}</ul>)}
          {tab === "flow" && <pre className="mono">{val?.flow_section ?? t("bottom.flowPending")}</pre>}
        </div>
      </footer>
      {showConns && <Connections onClose={() => {
        setShowConns(false);
        api.catalog().then(setCatalog).catch(() => undefined);
        if (doc && current) api.validate(current, tidy(doc)).then(setVal).catch(() => undefined);
      }} />}
    </div>
  );
}

export default function App() {
  return <ReactFlowProvider><Editor /></ReactFlowProvider>;
}

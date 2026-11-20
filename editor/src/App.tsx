import { useCallback, useEffect, useMemo, useRef, useState, type DragEvent } from "react";
import {
  Background, Controls, MiniMap, ReactFlow, ReactFlowProvider, useReactFlow, MarkerType,
  type Connection, type Edge, type EdgeChange, type IsValidConnection, type Node, type NodeChange,
} from "@xyflow/react";
import { api } from "./api";
import { Chat } from "./Chat";
import { Connections } from "./Connections";
import { useDialog } from "./Dialog";
import { RunPanel, type NodeRun, type RunRequest } from "./RunPanel";
import { Inspector } from "./Inspector";
import { LANGS, useI18n, type Lang } from "./i18n";
import { THEMES, useTheme, type Theme } from "./theme";
import {
  autoLayout, clone, ensureLayout, NODE_STYLES, type NodeStyle, inputKind, newAgent, newGate, removeNode, sourceIO, tidy, uniqueKey, uniqueName,
} from "./model";
import { nodeTypes, type Port } from "./nodes";
import type { Catalog, Doc, FlowSummary, FlowTemplate, Selection, Validation } from "./types";

const PALETTE = [
  { kind: "agent", label: "palette.agent", hint: "palette.agentHint", base: "agent" },
  { kind: "human", label: "palette.human", hint: "palette.humanHint", base: "approval" },
  { kind: "stop", label: "palette.stop", hint: "palette.stopHint", base: "stop" },
] as const;
type PaletteKind = (typeof PALETTE)[number]["kind"];
const FLOW_NAME = /^[a-z0-9][a-z0-9_-]{0,39}$/;  // same rule as the server
const DATA_LINKS_KEY = "afs.dataLinks";

/** Id of a data link on the canvas: d:<agent>:<input> (agent and input names are slugs, no ":"). */
const dataEdgeId = (agent: string, input: string) => `d:${agent}:${input}`;
const parseDataEdge = (id: string) => { const [, agent, input] = id.split(":"); return { agent, input }; };

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
  const [showData, setShowData] = useState<boolean>(() => {
    try { return localStorage.getItem(DATA_LINKS_KEY) !== "0"; } catch { return true; }
  });
  const toggleData = () => setShowData((v) => {
    try { localStorage.setItem(DATA_LINKS_KEY, v ? "0" : "1"); } catch { /* storage not available */ }
    return !v;
  });
  const past = useRef<Doc[]>([]);
  const future = useRef<Doc[]>([]);

  const dirty = doc !== null && JSON.stringify(doc) !== saved;
  const flash = (text: string, bad = false) => { setMsg({ text, bad }); setTimeout(() => setMsg(null), 4000); };

  // ---- load
  const refreshFlows = useCallback(() => api.flows().then(setFlows), []);
  useEffect(() => {
    api.catalog().then(setCatalog).catch((e) => flash(String(e), true));
    refreshFlows().then(() => undefined);
    api.templates().then(setTemplates).catch(() => undefined);
  }, [refreshFlows]);
  useEffect(() => {
    if (!current && flows.length) setCurrent(flows[0].name);
  }, [flows, current]);
  useEffect(() => {
    if (!current) return;
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
      ...Object.entries(s.agents).map(([id, a]) => {
        const ins: Port[] = Object.entries(a.inputs ?? {}).map(([n, i]) => ({
          name: n, type: inputKind(s, i), multiple: !!sourceIO(s, i.from)?.multiple, from: i.from,
        }));
        const outs: Port[] = Object.entries(a.outputs ?? {}).map(([n, o]) => ({
          name: n, type: o.type ?? "text", multiple: !!o.multiple,
        }));
        return {
          id, type: "agent", position: pos(id), measured: dims[id],
          selected: sel?.kind === "node" && sel.id === id,
          data: { name: id, agent: a, start: s.start === id, error: errNodes.has(id), run: trace[id], ins, outs,
            style: nodeStyle },
        };
      }),
      ...Object.entries(s.gates ?? {}).map(([id, g]) => ({
        id, type: "gate", position: pos(id), measured: dims[id],
        selected: sel?.kind === "node" && sel.id === id,
        data: { name: id, gate: g, error: errNodes.has(id), run: trace[id], style: nodeStyle },
      })),
    ];
  }, [doc, sel, errNodes, dims, trace, nodeStyle]);
  const edges: Edge[] = useMemo(() => {
    if (!doc) return [];
    const s = doc.spec;
    const control: Edge[] = (s.edges ?? []).map((e, i) => ({
      id: `e${i}`, source: e.from, target: e.to,
      label: [e.if?.trim(), e.when?.trim()].filter(Boolean).join(" · ") || undefined,
      selected: sel?.kind === "edge" && sel.index === i, type: "smoothstep",
      markerEnd: { type: MarkerType.ArrowClosed },
      className: [e.to === s.start || e.from in (s.gates ?? {}) ? "back" : "", e.if?.trim() ? "cond" : ""].join(" ").trim(),
    }));
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
  }, [doc, sel, showData]);

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
  const confirmDiscard = async () => !dirty || dialog.confirm({
    title: t("app.discardTitle"), message: t("app.discard"), ok: t("app.discardOk"), danger: true });
  const switchFlow = async (name: string) => { if (await confirmDiscard()) setCurrent(name); };
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
      await api.create(name, r.title || name, copyFrom ? { copy_from: copyFrom } : tmpl ? { template: tmpl.id, params } : undefined);
      await refreshFlows(); setCurrent(name); flash(t("app.created", { name }));
    }
    catch (e) { flash(String((e as Error).message ?? e), true); }
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
        <button onClick={() => { update((d) => autoLayout(d.spec, undefined, nodeStyle)); setTimeout(() => rf.fitView({ padding: 0.15 }), 50); }}>{t("app.arrange")}</button>
        <button className={showData ? "on" : ""} aria-pressed={showData} onClick={toggleData}
          title={t("app.dataLinksTitle")}>{t("app.dataLinks")}</button>
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
        <select className="node-style" value={nodeStyle} title={t("app.nodeStyle")} aria-label={t("app.nodeStyle")}
          onChange={(e) => setNodeStyle(e.target.value as NodeStyle)}>
          {NODE_STYLES.map((k) => <option key={k} value={k}>{t(`app.nodeStyle.${k}`)}</option>)}
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
            isValidConnection={isValidConnection}
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
          <Inspector doc={doc} sel={sel} catalog={catalog} val={val} update={update} select={setSel} remove={remove}
            testAgent={(id) => {
              const until = id.startsWith("until:");
              setRunReq({ mode: until ? "until" : "agent", target: until ? id.slice(6) : id, nonce: Date.now() });
              setRight("run");
            }} />
        </div>}
        {doc && current && <div className="right-body" hidden={right !== "chat"}>
          <Chat key={current} flow={current} lang={lang} doc={doc} onBusy={setAssistBusy} apply={(d) => {
            const n = ensureLayout(d, nodeStyle);
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

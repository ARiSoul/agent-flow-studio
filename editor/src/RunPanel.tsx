import { Markdown } from "./Markdown";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { api, currentWorkspaceName } from "./api";
import { useDialog } from "./Dialog";
import { useI18n, type I18n } from "./i18n";
import { localizeServer, type ServerMsg } from "./i18n/server";
import { GrowingTextarea } from "./fields";
import { Splitter } from "./Splitter";
import { flowInputs, inputKind, isTyped, sourceIO } from "./model";
import { formValue, sendValue, ValueField, type FormVal } from "./RunForm";
import type { Doc, FlowRaw, IOTypeRaw, RunIO } from "./types";

const MUTATE = { "Content-Type": "application/json", "X-Flow-Editor": "1" };
const RUN_ID = /^[A-Za-z0-9_-]{1,40}$/;  // same rule as the runner

export type NodeRun = "active" | "done" | "error" | "gate" | "skipped" | "stopped";
/** State of a link in a run: followed, not followed (its condition did not hold), or its `if`
 * expression failed. Key: `${from}>${to}`. */
export type EdgeRun = "followed" | "not" | "error";
export type Trace = { nodes: Record<string, NodeRun>; edges: Record<string, EdgeRun> };
export const edgeKey = (from: string, to: string) => `${from}>${to}`;
type Ev = { i: number; type: string; [k: string]: unknown };
type RunInfo = { id: string; status: string; gate: { gate: string; kind: string; label: string; reason: string } | null; cost: number; input: string; mode?: string; target?: string | null;
  workspace?: string; environment?: string | null };
export type RunMode = "full" | "step" | "until" | "agent";
/** The system step after a run that updates the workspace context (drawn on the canvas). */
export const CONTEXT_NODE = "__workspace_context";
export type RunRequest = { mode: RunMode; target?: string; nonce: number };

class ApiError extends Error {
  constructor(message: string, public status: number, public detail: unknown) { super(message); }
}

async function post<T>(url: string, body?: unknown): Promise<T> {
  const r = await fetch(url, { method: "POST", headers: MUTATE, body: body ? JSON.stringify(body) : undefined });
  const data = await r.json().catch(() => ({}));
  if (!r.ok) {
    const d = (data as { detail?: unknown }).detail;
    throw new ApiError(typeof d === "string" ? d : d ? JSON.stringify(d) : r.statusText, r.status, d);
  }
  return data as T;
}

/** State of each node from the events (for the canvas). In graph mode several agents run at once. */
export function traceOf(events: Ev[]): Record<string, NodeRun> {
  const s: Record<string, NodeRun> = {};
  let graph = false;
  for (const e of events) {
    if (e.sub) continue;  // inside a subflow: not a node of this canvas
    const a = e.agent as string | undefined;
    if (e.type === "context") s[CONTEXT_NODE] = e.status === "running" ? "active" : e.status === "done" ? "done" : "error";
    if (e.type === "start") graph = e.execution === "graph";
    if (e.type === "delegate" && a) {
      if (!graph) for (const k of Object.keys(s)) if (s[k] === "active") s[k] = "done";
      s[a] = "active";
    }
    if (e.type === "agent_done" && a) s[a] = e.error ? "error" : "done";
    if (e.type === "agent_blocked" && a) delete s[a];  // denied by step-by-step mode
    if (e.type === "skipped" && e.node) s[e.node as string] = "skipped";
    if (e.type === "gate") s[e.gate as string] = "gate";
    if (e.type === "decision") s[e.gate as string] = "done";
    if (e.type === "decided" && e.node) s[e.node as string] = "done";  // a decision node
    // the run ended (stopped, error, gate…) while an agent was still marked as running: a stop
    // cancels it without an agent_done event, so it would look "running" forever
    if (e.type === "end") for (const k of Object.keys(s)) if (s[k] === "active") s[k] = "stopped";
    // a resumed run (continue / decision) can start the same agents again
    if (e.type === "continue" || e.type === "decision") {
      for (const k of Object.keys(s)) if (s[k] === "stopped") delete s[k];
    }
  }
  return s;
}

/** State of each link from the events. Graph mode: the engine's `edge` events. Orchestrator mode
 * (no edge events): a link counts as followed when its target starts (delegate or gate) right after
 * its source finished; at the end, the other links out of the nodes that ran were not followed. */
export function edgeTraceOf(events: Ev[], links: { from: string; to: string }[]): Record<string, EdgeRun> {
  const s: Record<string, EdgeRun> = {};
  const exists = new Set(links.map((l) => edgeKey(l.from, l.to)));
  let graph = false;
  let last: string | null = null;  // orchestrator: the node that finished last
  const ran = new Set<string>();
  const follow = (to: string) => {
    if (last && exists.has(edgeKey(last, to))) s[edgeKey(last, to)] = "followed";
  };
  for (const e of events) {
    if (e.sub) continue;  // inside a subflow
    const a = e.agent as string | undefined;
    if (e.type === "start") { graph = e.execution === "graph"; continue; }
    if (graph) {
      if (e.type === "edge" && e.from && e.to) {
        const k = edgeKey(String(e.from), String(e.to));
        s[k] = e.error ? "error" : e.fired ? "followed" : "not";
      }
      continue;
    }
    if (e.type === "delegate" && a) { follow(a); ran.add(a); }
    if (e.type === "agent_done" && a && !e.error) last = a;
    if (e.type === "gate" && e.gate) { follow(String(e.gate)); ran.add(String(e.gate)); }
    if (e.type === "decision" && e.gate) last = String(e.gate);
    if (e.type === "end" && e.status === "done") {
      for (const l of links) {
        const k = edgeKey(l.from, l.to);
        if (ran.has(l.from) && !s[k]) s[k] = "not";
      }
    }
  }
  return s;
}

const status = (i18n: I18n, s: unknown) => i18n.tOr(`run.status.${String(s)}`, String(s));

function line(e: Ev, i18n: I18n): { cls: string; text: string } | null {
  // an event of a subflow: the same line, indented and tagged with the subflow node
  if (e.sub) {
    const l = line({ ...e, sub: undefined }, i18n);
    return l && { cls: `${l.cls} subev`, text: `↳ ${String(e.sub)} · ${l.text}` };
  }
  const { t, tOr } = i18n;
  const who = e.agent ? `${e.agent}: ` : "";
  const mode = String(e.mode ?? "full");
  switch (e.type) {
    case "start": return { cls: "info", text: `${t("run.ev.start", { flow: String(e.flow), input: String(e.input), mode: tOr(`run.mode.${mode}`, mode) })}${e.target ? ` ${e.target}` : ""}${e.execution === "graph" ? t("run.ev.graph") : ""}${e.workspace && e.workspace !== "studio" ? ` · ${t("run.ev.workspace", { ws: String(e.workspace), env: String(e.environment ?? "—") })}` : ""}${e.handoff ? ` · ${t("run.ev.handoff", { list: Object.entries(e.handoff as Record<string, { flow: string; run: string }>).map(([k, v]) => `${k} ← ${v.flow}/${v.run}`).join(", ") })}` : ""}` };
    case "context": return e.status === "running"
      ? { cls: "deleg", text: t("run.ev.contextStart", { root: String(e.root), mode: tOr(`ctx.mode.${String(e.mode)}`, String(e.mode)) }) }
      : { cls: e.status === "done" ? "ok" : "err", text: t(e.status === "done" ? "run.ev.contextDone" : "run.ev.contextError",
        { root: String(e.root), detail: String(e.summary ?? e.message ?? "") }) };
    case "subflow": return { cls: "deleg", text: t("run.ev.subflow", { agent: String(e.agent), flow: String(e.flow), run: String(e.run) }) };
    case "delegate": return { cls: "deleg", text: `→ ${e.agent}${e.task ? ` — ${e.task}` : ""}` };
    case "tool": return { cls: "tool", text: `${who}${e.tool}${e.detail ? ` — ${e.detail}` : e.artifact ? ` (${e.artifact})` : ""}` };
    // the tool and its error text (older runs have neither)
    case "tool_error": return { cls: "err", text: e.error
      ? t("run.ev.toolErrorDetail", { who, tool: String(e.tool ?? "?"), error: String(e.error) })
      : t("run.ev.toolError", { who }) };
    case "agent_done": return { cls: e.error ? "err" : "ok", text: t(e.error ? "run.ev.agentDoneError" : "run.ev.agentDone", { agent: String(e.agent) }) };
    case "text": return { cls: e.agent ? "sub" : "say", text: `${who}${e.text}` };
    // a stop with a message: the message is the result of the run
    case "gate": return e.message ? { cls: "result-msg", text: `■ ${e.label}: ${e.message}` }
      : { cls: "gate", text: `⏸ ${e.label} — ${e.reason}` };
    case "decided": return { cls: "edge", text: t(e.to ? "run.ev.decided" : "run.ev.decidedNone", { node: String(e.node), to: String(e.to ?? "") }) };
    case "approval": return { cls: "gate", text: t("run.ev.approval", { agent: String(e.agent), summary: String(e.summary) }) };
    case "approval_result": return { cls: e.approved ? "ok" : "err", text: `${t(e.approved ? "run.ev.approved" : "run.ev.rejected")}: ${e.summary}${e.comment ? ` — ${e.comment}` : ""}` };
    case "agent_blocked": return null;
    case "continue": return { cls: "info", text: t(e.how === "full" ? "run.ev.continueFull" : e.how === "auto" ? "run.ev.continueAuto" : "run.ev.continueStep") };
    case "ask": return { cls: "you", text: `${t("run.ev.ask", { who: e.agent ? String(e.agent) : t("run.askFlow") })}\n${e.text}` };
    case "decision": return { cls: "info", text: `${t("run.ev.decision", { gate: String(e.gate), decision: tOr(`run.decision.${String(e.decision)}`, String(e.decision)) })}${e.comment ? ` — ${e.comment}` : ""}` };
    case "result": return { cls: "info", text: `${who}${t("run.ev.result", { session: String(e.session_id).slice(0, 8), turns: String(e.turns), cost: Number(e.cost ?? 0).toFixed(3) })}${e.denials ? t("run.ev.denials", { n: String(e.denials) }) : ""}` };
    case "skipped": return { cls: "info", text: t("run.ev.skipped", { node: String(e.node) }) };
    case "edge": {
      const by = String(e.by ?? "always");
      return { cls: e.fired ? "edge" : "edge off", text: t(e.fired ? "run.ev.edgeFired" : "run.ev.edgeNotFired", { from: String(e.from), to: String(e.to), by: tOr(`run.by.${by}`, by) }) };
    }
    case "outputs": return { cls: "ok", text: t("run.ev.outputs", { agent: String(e.agent), names: (Array.isArray(e.names) ? e.names : []).join(", ") || "—" }) };
    case "end": return { cls: e.status === "error" ? "err" : "info", text: `${t("run.ev.end", { status: status(i18n, e.status) })}${e.message ? ` — ${e.message}` : ""}${e.note ? ` (${e.note})` : ""}` };
    default: return null;
  }
}

/** Default run id, as the runner makes it: <flow[:20]>-<yyyymmdd-hhmmss>. */
export function defaultRunId(flow: string): string {
  const d = new Date(), p = (n: number) => String(n).padStart(2, "0");
  return `${flow.slice(0, 20)}-${d.getFullYear()}${p(d.getMonth() + 1)}${p(d.getDate())}-${p(d.getHours())}${p(d.getMinutes())}${p(d.getSeconds())}`;
}

/** The field type of an agent input in the agent-mode form (its resolved type and its source's options). */
function agentInputIO(spec: FlowRaw, agent: string, name: string): IOTypeRaw {
  const i = spec.agents[agent].inputs![name];
  const src = sourceIO(spec, i.from);
  const type = inputKind(spec, i);
  return {
    type, multiple: !!src?.multiple, options: type === "choice" ? src?.options : undefined,
    required: i.required !== false, description: i.description || (i.from ? `← ${i.from}` : undefined), accept: src?.accept,
  };
}

/** The value an agent input gets from its source in a saved run (undefined: none yet). */
function sourceValue(spec: FlowRaw, io: RunIO | null, agent: string, name: string): unknown {
  const from = spec.agents[agent].inputs?.[name]?.from;
  if (!io || !from) return undefined;
  const [root, key] = [from.slice(0, from.indexOf(".")), from.slice(from.indexOf(".") + 1)];
  return root === "input" ? io.inputs[key] : io.outputs[root]?.[key];
}

/** Prefill of the agent-test form: the source's saved value; for a free input, the last test value. */
function savedInput(spec: FlowRaw, io: RunIO | null, agent: string, name: string): unknown {
  const v = sourceValue(spec, io, agent, name);
  if (v !== undefined) return v;
  return spec.agents[agent].inputs?.[name]?.from ? undefined : io?.given[agent]?.[name];
}

/** Label and "where it comes from" text of an agent input in the agent-test form. */
function sourceLabel(spec: FlowRaw, agent: string, name: string, t: I18n["t"]): { label: string; hint: string } {
  const i = spec.agents[agent].inputs![name];
  const own = i.description ? ` ${i.description}` : "";
  if (!i.from) return { label: name, hint: t("run.form.freeInput") + own };
  const [root, key] = [i.from.slice(0, i.from.indexOf(".")), i.from.slice(i.from.indexOf(".") + 1)];
  if (root === "input") {
    const fi = flowInputs(spec)[key];
    return { label: fi?.label || key, hint: t("run.form.fromFlowInput", { name: key, label: fi?.label || key }) + own };
  }
  return { label: `${name}`, hint: t("run.form.fromOutput", { agent: root, output: key }) + own };
}

const same = (a: unknown, b: unknown) => JSON.stringify(a ?? null) === JSON.stringify(b ?? null);

// same test as AUTH_ERROR in editor/runner.py
const AUTH_ERROR = /failed to authenticate|oauth (session|token)|not logged in|please run \/login|invalid (x-)?api[ -]?key|authentication_error|could not be refreshed/i;

// Core tool servers ignore AFS_MOCK; only extension tools can use simulated data.
const CORE_SERVERS = new Set(["artifacts", "connections", "flow_control", "io", "workspace"]);
/** Does the flow use tools from an extension (the only ones AFS_MOCK changes)? */
export function usesExtensionTools(spec: FlowRaw): boolean {
  const external = new Set(Object.keys(spec.mcp_servers ?? {}));
  return Object.values(spec.agents).some((a) => (a.tools ?? []).some((tl) => {
    const server = tl.split(".")[0];
    return !CORE_SERVERS.has(server) && !external.has(server);
  }));
}

const pretty = (v: unknown) => (typeof v === "string" ? v : JSON.stringify(v, null, 2));

/** A value kept in this browser (localStorage). */
function useStored<T>(key: string, initial: T): [T, (v: T) => void] {
  const [v, setV] = useState<T>(() => {
    try { const raw = localStorage.getItem(key); return raw === null ? initial : (JSON.parse(raw) as T); } catch { return initial; }
  });
  const set = (x: T) => { setV(x); try { localStorage.setItem(key, JSON.stringify(x)); } catch { /* no storage */ } };
  return [v, set];
}

export function RunPanel({ flow, doc, dirty, mock, onTrace, request }: {
  flow: string; doc: Doc; dirty: boolean; mock: boolean; onTrace: (t: Trace) => void;
  request?: RunRequest | null;
}) {
  const i18n = useI18n();
  const { t } = i18n;
  const dialog = useDialog();
  const spec = doc.spec;
  const typed = isTyped(spec);
  // a flow without inputs (no legacy `input` or `task` of its own): only a run id, prefilled
  const plain = !typed && !spec.input && !spec.task;
  const idOnly = typed || plain;
  const [mode, setMode] = useState<RunMode>("full");
  const [target, setTarget] = useState("");
  const [instruction, setInstruction] = useState("");
  const [inputs, setInputs] = useState<string[]>([]);  // artifacts that already exist for the run
  useEffect(() => {
    if (request) { setMode(request.mode); setTarget(request.target ?? ""); }
  }, [request]);
  const [input, setInput] = useState("");  // legacy flows: the one input (also the run id)
  const [runId, setRunId] = useState(() => defaultRunId(flow));  // typed flows
  const [values, setValues] = useState<Record<string, FormVal>>(
    () => Object.fromEntries(Object.entries(spec.inputs ?? {}).map(([n, x]) => [n, formValue(x, x.default)])));
  const [given, setGiven] = useState<Record<string, FormVal>>({});
  const [fieldErr, setFieldErr] = useState<Record<string, string>>({});
  // inputs filled from a run of another flow (provenance, saved with the run)
  const [handoff, setHandoff] = useState<Record<string, { flow: string; run: string; agent: string; output: string }>>({});
  const [runIdErr, setRunIdErr] = useState<string | null>(null);
  const [saved, setSaved] = useState<RunIO | null>(null);
  const [run, setRun] = useState<RunInfo | null>(null);
  const [events, setEvents] = useState<Ev[]>([]);
  const [comment, setComment] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [arts, setArts] = useState<{ name: string; size: number }[]>([]);
  const [view, setView] = useState<{ name: string; text: string } | null>(null);
  const [sent, setSent] = useState<Set<string>>(new Set());
  const evRef = useRef<Ev[]>([]);
  const end = useRef<HTMLDivElement>(null);
  const label = spec.input?.label ?? t("insp.inputDefault");
  const rid = idOnly ? runId.trim() : input.trim();  // the run folder in runs/
  // a studio workspace keeps its runs in runs/@<workspace>/
  const wsName = run?.workspace ?? currentWorkspaceName();
  const wsDir = wsName && wsName !== "studio" ? `@${wsName}/` : "";

  const links = doc.spec.edges ?? [];
  useEffect(() => {
    onTrace({ nodes: traceOf(events), edges: edgeTraceOf(events, links) });
    end.current?.scrollIntoView({ block: "nearest" });
  }, [events, onTrace]);  // eslint-disable-line react-hooks/exhaustive-deps -- the links of this run
  useEffect(() => () => onTrace({ nodes: {}, edges: {} }), [onTrace]);

  // inputs hidden by their show_if for the values typed so far (the server evaluates it)
  const [hiddenIn, setHiddenIn] = useState<Set<string>>(new Set());
  useEffect(() => {
    const defs = spec.inputs ?? {};
    if (!Object.values(defs).some((x) => x.show_if)) { setHiddenIn(new Set()); return; }
    const cur: Record<string, unknown> = {};
    for (const [n, x] of Object.entries(defs)) {
      if (x.type === "file") continue;
      const r = sendValue(x, values[n] ?? formValue(x, x.default));
      if (r.value !== undefined) cur[n] = r.value;
    }
    const tm = setTimeout(() => {
      fetch("/api/inputs/visible", { method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ inputs: defs, values: cur }) })
        .then((r) => r.json()).then((d: { hidden?: string[] }) => setHiddenIn(new Set(d.hidden ?? [])))
        .catch(() => undefined);
    }, 150);
    return () => clearTimeout(tm);
  }, [spec.inputs, values]);
  // new flow inputs (edited in the Inspector) get their default
  useEffect(() => {
    setValues((v) => {
      const next = { ...v };
      for (const [n, x] of Object.entries(spec.inputs ?? {})) if (!(n in next)) next[n] = formValue(x, x.default);
      return next;
    });
  }, [spec.inputs]);

  const loadArts = async (inp: string) => {
    const r = await fetch(`/api/artifacts/${encodeURIComponent(inp)}`);
    setArts(r.ok ? await r.json() : []);
  };
  const loadIO = useCallback((inp: string) => {
    if (!RUN_ID.test(inp)) { setSaved(null); return; }
    api.io(inp, flow).then(setSaved).catch(() => setSaved(null));
  }, [flow]);

  const follow = async (id: string, inp: string) => {
    const r = await fetch(`/api/runs/${id}/events?after=${evRef.current.length}`);
    if (!r.body) return;
    const reader = r.body.getReader();
    const dec = new TextDecoder();
    let buf = "";
    for (;;) {
      const { value, done } = await reader.read();
      if (done) break;
      buf += dec.decode(value, { stream: true });
      let nl;
      while ((nl = buf.indexOf("\n")) >= 0) {
        const raw = buf.slice(0, nl).trim();
        buf = buf.slice(nl + 1);
        if (!raw) continue;
        const ev = JSON.parse(raw) as Ev;
        if (ev.type === "state") setRun(ev as unknown as RunInfo);
        else { evRef.current = [...evRef.current, ev]; setEvents(evRef.current); }
        if (ev.type === "tool" || ev.type === "agent_done") loadArts(inp);
        if (ev.type === "agent_done" || ev.type === "outputs" || ev.type === "decision") loadIO(inp);
      }
    }
    loadArts(inp);
    loadIO(inp);
  };

  // saved values of the run (outputs view, agent-mode form), and artifacts the target agent needs
  useEffect(() => {
    if (!RUN_ID.test(rid)) { setInputs([]); setSaved(null); return; }
    const h = setTimeout(() => {
      loadIO(rid);
      if (mode === "agent") fetch(`/api/artifacts/${encodeURIComponent(rid)}`)
        .then((r) => (r.ok ? r.json() : [])).then((a: { name: string }[]) => setInputs(a.map((x) => x.name)))
        .catch(() => setInputs([]));
    }, 300);
    return () => clearTimeout(h);
  }, [mode, rid, loadIO]);
  const needs = mode === "agent" && target ? spec.agents[target]?.reads ?? [] : [];
  const missing = needs.filter((n) => !inputs.includes(n));

  // agent mode: the target agent's inputs, prefilled from the saved run
  const targetInputs = mode === "agent" && target && spec.agents[target] ? Object.keys(spec.agents[target].inputs ?? {}) : [];
  const targetKey = `${target}|${targetInputs.join(",")}`;
  useEffect(() => {
    if (mode !== "agent" || !target || !spec.agents[target]) { setGiven({}); return; }
    setGiven(Object.fromEntries(targetInputs.map((n) =>
      [n, formValue(agentInputIO(spec, target, n), savedInput(spec, saved, target, n))])));
  }, [mode, targetKey, saved]);  // eslint-disable-line react-hooks/exhaustive-deps -- refill when the agent or the saved run changes

  const collect = (): { body: Record<string, unknown> } | null => {
    const errs: Record<string, string> = {};
    const out: Record<string, unknown> = {};
    if (typed && mode !== "agent") for (const [n, x] of Object.entries(spec.inputs ?? {})) {
      if (hiddenIn.has(n)) continue;  // hidden by show_if: not sent
      const r = sendValue(x, values[n] ?? formValue(x, x.default));
      if (r.error) errs[n] = t("run.form.badJson");
      else if (r.value !== undefined) out[n] = r.value;
    }
    const g: Record<string, unknown> = {};
    for (const n of targetInputs) {
      const x = agentInputIO(spec, target, n);
      if (x.type === "file") continue;  // an agent under test reads the saved file
      const r = sendValue(x, given[n] ?? "");
      if (r.error) errs[`given.${n}`] = t("run.form.badJson");
      // only what you changed: an unchanged value still comes from its source
      else if (r.value !== undefined && !same(r.value, sourceValue(spec, saved, target, n))) g[n] = r.value;
    }
    setFieldErr(errs);
    if (Object.keys(errs).length) return null;
    const base = { flow, mode, target: target || null, instruction: mode === "agent" ? instruction : "",
      ...(Object.keys(g).length ? { given: g } : {}),
      ...(typed && Object.keys(handoff).length ? { handoff } : {}) };
    return { body: typed ? { ...base, run_id: runId.trim() || null, inputs: out } : { ...base, input: rid } };
  };

  /** "Fill from a run": the value of an output of a finished run of another flow of this
   * workspace (the input's `source` flow first); typing a value by hand stays possible. */
  const fillFromRun = async (n: string, x: IOTypeRaw) => {
    setError(null);
    const all = (await api.flows().catch(() => [])).filter((f) => f.name !== flow);
    let src = x.source?.flow && all.some((f) => f.name === x.source!.flow) ? x.source.flow : null;
    if (!src) {
      if (!all.length) return setError(t("run.fill.noFlows"));
      const r = await dialog.form({ title: t("run.fill.pickFlow", { input: x.label || n }), ok: t("app.next"), fields: [
        { key: "flow", label: t("run.fill.flow"), value: all[0].name,
          options: all.map((f) => ({ value: f.name, label: `${f.title} (${f.name})` })) }] });
      if (!r) return;
      src = r.flow;
    }
    const from = src;
    let runs;
    try { runs = await api.handoffRuns(from, x.type ?? "text"); } catch (e) { return setError(String((e as Error).message ?? e)); }
    if (!runs.length) return setError(t("run.fill.noRuns", { flow: from }));
    const want = x.source?.flow === from ? x.source.output : null;
    const first = runs[0].outputs.find((o) => `${o.agent}.${o.name}` === want) ?? runs[0].outputs[0];
    const r = await dialog.form({ title: t("run.fill.pickOutput", { input: x.label || n, flow: from }), message: t("run.fill.message"),
      ok: t("run.fill.ok"), fields: [{ key: "pick", label: t("run.fill.output"), value: `${runs[0].input}|${first.agent}|${first.name}`,
        options: runs.flatMap((ru) => ru.outputs.map((o) => ({ value: `${ru.input}|${o.agent}|${o.name}`,
          label: `${ru.input} · ${o.agent}.${o.name} — ${o.preview.replace(/\s+/g, " ").slice(0, 60)}` }))) }] });
    if (!r) return;
    const [runIn, agent, output] = r.pick.split("|");
    try {
      const v = await api.handoffValue(from, runIn, agent, output);
      setValues((p) => ({ ...p, [n]: formValue(x, v.value) }));
      setFieldErr(({ [n]: _, ...rest }) => rest);
      setHandoff((h) => ({ ...h, [n]: { flow: from, run: runIn, agent, output } }));
    } catch (e) { setError(String((e as Error).message ?? e)); }
  };

  const start = async () => {
    setError(null); setRunIdErr(null);
    if (!rid && !typed) return;
    if ((mode === "until" || mode === "agent") && !target) return setError(t("run.pickAgent"));
    const c = collect();
    if (!c) return setError(t("run.form.fix"));
    if (usesExtensionTools(spec) && !mock && !(await dialog.confirm({ title: t("run.realTitle"), message: t("run.confirmReal"), ok: t("run.realOk"), danger: true }))) return;
    try {
      const r = await post<RunInfo>("/api/runs", c.body);
      evRef.current = []; setEvents([]); setView(null); setRun(r);
      await follow(r.id, r.input);
    } catch (e) {
      const d = e instanceof ApiError && e.status === 422 ? e.detail : null;
      if (d && typeof d === "object" && !Array.isArray(d)) {
        const o = d as { inputs?: Record<string, string>; given?: Record<string, string>; run_id?: string;
          codes?: Record<string, ServerMsg>; given_codes?: Record<string, ServerMsg> };
        // the reasons come in English with a code: show them in the UI language
        const here = (texts: Record<string, string>, codes: Record<string, ServerMsg> | undefined) =>
          Object.entries(texts).map(([k, v]) => [k, localizeServer(codes?.[k], v, i18n.lang)] as const);
        if (o.inputs) setFieldErr(Object.fromEntries(here(o.inputs, o.codes)));
        if (o.given) setFieldErr(Object.fromEntries(here(o.given, o.given_codes).map(([k, v]) => [`given.${k}`, v])));
        if (o.run_id) setRunIdErr(o.run_id);
        setError(t("run.form.fix"));
      } else setError(String((e as Error).message));
    }
  };

  // past runs of this flow (runs/): open one again to look at it, ask for more or answer its gate
  const [past, setPast] = useState<{ input: string; run: string; started: number; status: string;
    gate?: string | null; cost: number; mode?: string }[]>([]);
  const loadPast = useCallback(() => {
    fetch(`/api/flows/${encodeURIComponent(flow)}/runs`).then((r) => (r.ok ? r.json() : { runs: [] }))
      .then((d) => setPast(d.runs ?? [])).catch(() => setPast([]));
  }, [flow]);
  useEffect(() => { if (run?.status !== "running") loadPast(); }, [loadPast, run?.status]);
  const openPast = async (inp: string) => {
    setError(null);
    try {
      const r = await post<RunInfo>("/api/runs/open", { input: inp });
      evRef.current = []; setEvents([]); setView(null); setShown(null);
      if (idOnly) setRunId(inp); else setInput(inp);
      setRun(r);
      await follow(r.id, r.input);
    } catch (e) { setError(String((e as Error).message)); }
  };

  const cont = async (how: "step" | "full") => {
    if (!run) return;
    setError(null);
    try {
      const r = await post<RunInfo>(`/api/runs/${run.id}/continue`, { how });
      setRun(r);
      await follow(r.id, run.input);
    } catch (e) { setError(String((e as Error).message)); }
  };

  const decide = async (decision: "approve" | "reject" | "answer") => {
    if (!run) return;
    setError(null);
    try {
      const r = await post<RunInfo>(`/api/runs/${run.id}/decision`, { decision, comment });
      setComment(""); setRun(r);
      await follow(r.id, run.input);
    } catch (e) { setError(String((e as Error).message)); }
  };

  const open = async (name: string) => {
    if (!run) return;
    const r = await fetch(`/api/artifacts/${encodeURIComponent(run.input)}/${encodeURIComponent(name)}`);
    if (r.ok) setView(await r.json());
  };

  const decided = new Set(events.filter((e) => e.type === "approval_result").map((e) => e.id as string));
  const pending = events.filter((e) => e.type === "approval" && !decided.has(e.id as string) && !sent.has(e.id as string));
  const answer = async (id: string, ok: boolean) => {
    if (!run || sent.has(id)) return;
    setSent((s) => new Set(s).add(id));  // hide the card now (avoids a double click)
    try { await post(`/api/runs/${run.id}/approvals/${id}`, { approve: ok, comment }); setComment(""); }
    catch (e) { setError(String((e as Error).message)); }
  };
  const running = run?.status === "running";
  // the setup block and the outputs/artifacts blocks can be collapsed, and a splitter sets how
  // much height the outputs take from the conversation (kept in this browser)
  const [headOpen, setHeadOpen] = useStored("afs.run.head", true);
  const [outsOpen, setOutsOpen] = useStored("afs.run.outs", true);
  const [artsOpen, setArtsOpen] = useStored("afs.run.arts", true);
  const [artsH, setArtsH] = useStored<number | null>("afs.run.artsH", null);
  const panelRef = useRef<HTMLDivElement>(null);
  const resizeArts = (px: number | null) => {
    const max = (panelRef.current?.getBoundingClientRect().height ?? 600) - 120;
    setArtsH(px === null ? null : Math.round(Math.max(60, Math.min(max, px))));
  };
  const headToggle = (
    <button className="icon" title={t(headOpen ? "run.headHide" : "run.headShow")} aria-expanded={headOpen}
      onClick={() => setHeadOpen(!headOpen)}>{headOpen ? "▴" : "▾"}</button>);
  // after a run (or at a gate): who can be asked for more (the agents that ran, the last first)
  const [askable, setAskable] = useState<string[]>([]);
  const [askTo, setAskTo] = useState<string>("");
  const [askText, setAskText] = useState("");
  useEffect(() => {
    if (!run || run.status === "running") { setAskable([]); return; }
    fetch(`/api/runs/${run.id}/askable`).then((r) => (r.ok ? r.json() : { agents: [] }))
      .then((d: { agents: string[] }) => { setAskable(d.agents); setAskTo((x) => (d.agents.includes(x) ? x : d.agents[0] ?? "")); })
      .catch(() => setAskable([]));
  }, [run?.id, run?.status]);  // eslint-disable-line react-hooks/exhaustive-deps
  const ask = async () => {
    if (!run || !askText.trim()) return;
    setError(null);
    try {
      const r = await post<RunInfo>(`/api/runs/${run.id}/ask`, { message: askText, agent: askTo });
      setAskText(""); setRun(r);
      await follow(r.id, run.input);
    } catch (e) { setError(String((e as Error).message)); }
  };
  // the end of the run: a workspace flow may suggest updating the workspace context
  const lastEnd = useMemo(() => [...events].reverse().find((e) => e.type === "end"), [events]);
  const updateContext = async () => {
    if (!run) return;
    setError(null);
    try {
      const r = await api.updateContext("update", null, run.input);
      setRun(r as unknown as RunInfo);
      evRef.current = []; setEvents([]);
      await follow(r.id, r.input);
    } catch (e) { setError(String((e as Error).message ?? e)); }
  };
  const gate = run?.status === "gate" ? run.gate : null;
  const paused = run?.status === "paused";
  const agents = Object.keys(spec.agents);
  const savedInputs = typed && saved && Object.keys(saved.inputs).length > 0;
  const outputs = useMemo(() => Object.entries(saved?.outputs ?? {}).filter(([, o]) => Object.keys(o).length), [saved]);
  // the agent that saved outputs last in this run: its group starts open (the result)
  const lastOut = useMemo(() => [...events].reverse().find((e) => e.type === "outputs")?.agent as string | undefined, [events]);
  const [shown, setShown] = useState<{ title: string; value: unknown; md: boolean } | null>(null);
  const isMarkdown = (agent: string, n: string) => {
    const tp = spec.agents[agent]?.outputs?.[n]?.type;
    return tp === "markdown" || tp === "text";
  };
  const [copied, setCopied] = useState<string | null>(null);  // the output just copied ("Copied")
  const copyValue = (v: unknown, key: string) => {
    navigator.clipboard?.writeText(pretty(v)).then(() => {
      setCopied(key);
      setTimeout(() => setCopied((c) => (c === key ? null : c)), 1500);
    }).catch(() => undefined);
  };
  const download = (name: string, v: unknown, md: boolean) => {
    const text = pretty(v);
    const url = URL.createObjectURL(new Blob([text], { type: md ? "text/markdown" : "application/json" }));
    const a = document.createElement("a");
    a.href = url;
    a.download = `${name}${md ? ".md" : typeof v === "string" ? ".txt" : ".json"}`;
    a.click();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  };
  const openFolder = () => {
    if (rid) post(`/api/runs/${encodeURIComponent(rid)}/reveal`).catch((e) => setError(String(e)));
  };
  // the Claude Code CLI is not signed in: the runner marks these errors with hint "auth"; the same
  // test runs here too, so the card also shows with events from an older server
  const authErr = events.find((e) => e.hint === "auth"
    || (["end", "agent_done", "result", "text"].includes(e.type) && AUTH_ERROR.test(String(e.message ?? e.text ?? ""))));

  const runButton = running
    ? <button className="danger" onClick={() => run && post(`/api/runs/${run.id}/stop`)}>{t("run.stop")}</button>
    : <button className="primary" disabled={dirty || (idOnly ? !RUN_ID.test(rid) : !rid)} onClick={start}
      title={dirty ? t("run.saveFirstTitle") : t("run.run")}>{t("run.run")}</button>;
  return (
    <div className="runp" ref={panelRef}>
      <div className="run-head">
        {running && <div className="kit-bar global" aria-hidden="true" />}
        {idOnly ? <>
          <div className="row">
            <input className={`mono${runIdErr || (rid && !RUN_ID.test(rid)) ? " bad" : ""}`} value={runId} disabled={running}
              title={t("run.runIdTitle")} aria-label={t("run.runId")} placeholder={t("run.runId")}
              onChange={(e) => { setRunId(e.target.value); setRunIdErr(null); }} />
            <button className="icon" disabled={running} title={t("run.newRunId")} onClick={() => setRunId(defaultRunId(flow))}>↻</button>
            {runButton}
            {headToggle}
          </div>
          {(runIdErr || (rid && !RUN_ID.test(rid))) && <span className="field-err">{runIdErr ?? t("run.runIdHint")}</span>}
        </> : (
          <div className="row">
            <input placeholder={t("run.inputPlaceholder", { label })} value={input} disabled={running}
              onChange={(e) => setInput(e.target.value)} onKeyDown={(e) => e.key === "Enter" && start()} />
            {runButton}
            {headToggle}
          </div>)}
        {headOpen && <>{/* the rest of the setup: past runs, mode, inputs (collapsible) */}
        {!running && past.length > 0 && (
          <details className="past-runs">
            <summary className="small">{t("run.pastRuns", { n: past.length })}</summary>
            <ul>
              {past.map((p) => (
                <li key={p.input} className={run?.input === p.input ? "on" : undefined}>
                  <button className="small link" onClick={() => openPast(p.input)} title={t("run.pastOpenTitle")}>
                    <span className="mono">{p.input}</span>
                  </button>
                  <span className="muted small">{p.started ? new Date(p.started * 1000).toLocaleString(i18n.lang) : ""}</span>
                  <span className={`badge${p.status === "error" ? " warn" : ""}`}>{status(i18n, p.status)}{p.gate ? ` · ${p.gate}` : ""}</span>
                  {p.cost > 0 && <span className="muted small">≈ {p.cost.toFixed(3)} USD</span>}
                </li>))}
            </ul>
          </details>)}
        <div className="row mode-row">
          <select value={mode} disabled={running} onChange={(e) => setMode(e.target.value as RunMode)}
            title={t("run.modeTitle")}>
            <option value="full">{t("run.opt.full")}</option>
            <option value="step">{t("run.opt.step")}</option>
            <option value="until">{t("run.opt.until")}</option>
            <option value="agent">{t("run.opt.agent")}</option>
          </select>
          {(mode === "until" || mode === "agent") && (
            <select value={target} disabled={running} onChange={(e) => setTarget(e.target.value)}>
              <option value="">{t("run.agentOption")}</option>
              {agents.map((a) => <option key={a}>{a}</option>)}
            </select>)}
        </div>
        {typed && mode !== "agent" && (
          <details className="run-form" open>
            <summary className="small">{t("run.form.inputs")}
              {savedInputs && <button className="small link" disabled={running} title={t("run.form.useSavedTitle")}
                onClick={(e) => {
                  e.preventDefault();
                  setValues((v) => ({ ...v, ...Object.fromEntries(Object.entries(spec.inputs ?? {})
                    .filter(([n, x]) => x.type !== "file" && n in saved!.inputs).map(([n, x]) => [n, formValue(x, saved!.inputs[n])])) }));
                }}>{t("run.form.useSaved")}</button>}
            </summary>
            {Object.entries(spec.inputs ?? {}).filter(([n]) => !hiddenIn.has(n)).map(([n, x]) => (
              <ValueField key={n} name={n} t={x} value={values[n] ?? formValue(x, x.default)} disabled={running}
                error={fieldErr[n]} saved={saved?.inputs[n]}
                onChange={(v) => { setValues((p) => ({ ...p, [n]: v })); setFieldErr(({ [n]: _, ...rest }) => rest);
                  setHandoff(({ [n]: _h, ...rest }) => rest); }}
                extra={x.type !== "file" && <span className="fill-row">
                  <button className="small link" disabled={running} title={t("run.fill.title")} onClick={() => fillFromRun(n, x)}>
                    {t("run.fill.button")}{x.source ? ` · ${x.source.flow}` : ""}</button>
                  {handoff[n] && <span className="badge" title={t("run.fill.fromTitle")}>← {handoff[n].flow}/{handoff[n].run} · {handoff[n].agent}.{handoff[n].output}</span>}
                </span>} />
            ))}
          </details>)}
        {mode === "agent" && <>
          <GrowingTextarea minRows={2} disabled={running} placeholder={t("run.instruction")}
            value={instruction} onChange={(e) => setInstruction(e.target.value)} />
          {target && targetInputs.length > 0 && (
            <details className="run-form" open>
              <summary className="small">{t("run.form.agentInputs", { agent: target })}</summary>
              <p className="muted small">{t("run.form.agentInputsHint")}</p>
              {targetInputs.map((n) => {
                const x = agentInputIO(spec, target, n);
                const s = sourceLabel(spec, target, n, t);
                return x.type === "file"
                  ? <div key={n} className="field run-field"><span className="field-label">{s.label}</span>
                    <span className="field-hint">{s.hint} · {t("run.form.agentFile")}</span></div>
                  : <ValueField key={n} name={n} t={{ ...x, label: s.label, description: s.hint }} value={given[n] ?? ""}
                    disabled={running} error={fieldErr[`given.${n}`]}
                    onChange={(v) => { setGiven((p) => ({ ...p, [n]: v })); setFieldErr(({ [`given.${n}`]: _, ...rest }) => rest); }} />;
              })}
            </details>)}
          {target && needs.length > 0 && (
            <p className={`small ${missing.length ? "field-err" : "muted"}`}>
              {missing.length ? t("run.missingInputs", { input: rid || t("run.inputFolder"), names: missing.join(", ") })
                : t("run.presentInputs", { names: needs.join(", ") })}</p>)}
        </>}
        {usesExtensionTools(spec) && (
          <span className={`badge ${mock ? "" : "warn"}`} title={t("run.mockTitle")}>{mock ? t("run.mock") : t("run.real")}</span>)}
        </>}
        {dirty && <span className="muted small">{t("run.saveFirst")}</span>}
        {run && <span className="muted small"> · {status(i18n, run.status)}{run.cost ? ` · ≈ ${run.cost.toFixed(3)} USD` : ""}</span>}
        {error && <div className="field-err">{error}</div>}
      </div>

      <div className="run-log">
        {!run && <p className="muted">{t("run.intro")} <code>runs/{t("run.inputFolder")}/decisions.jsonl</code> {t("run.introEnd")}</p>}
        {authErr && (
          <div className="gate-card auth" role="alert">
            <b>{t("run.auth.title")}</b>
            <p className="small">{t("run.auth.why")}</p>
            <ol className="small">
              <li>{t("run.auth.step1")}</li>
              <li>{t("run.auth.step2")} <code>claude</code></li>
              <li>{t("run.auth.step3")} <code>/login</code></li>
              <li>{t("run.auth.step4")}</li>
            </ol>
            <p className="muted small">{t("run.auth.apiKey")} <code>ANTHROPIC_API_KEY</code></p>
            <details><summary className="small">{t("run.auth.detail")}</summary>
              <pre className="mono art-view">{String(authErr.message ?? authErr.text ?? "")}</pre></details>
          </div>)}
        {events.map((e) => {
          const l = line(e, i18n);
          if (!l) return null;
          // what the model writes is Markdown (lists, bold, code)
          if (e.type === "text") return <div key={e.i} className={`ev ${l.cls} ev-md`}>{e.agent ? <b>{String(e.agent)}: </b> : null}<Markdown text={String(e.text)} /></div>;
          return <div key={e.i} className={`ev ${l.cls}`}>{l.text}</div>;
        })}
        {pending.map((p) => (
          <div key={p.id as string} className="gate-card write">
            <b>{t("run.writeBy", { agent: String(p.agent), env: String(p.environment) })}</b>
            <p className="small mono">{String(p.summary)}</p>
            <details open><summary className="small">{t("run.detail")}</summary>
              <pre className="mono art-view">{JSON.stringify(p.detail, null, 2)}</pre></details>
            <GrowingTextarea minRows={2} placeholder={t("run.writeComment")} value={comment} onChange={(e) => setComment(e.target.value)} />
            <div className="row">
              <button className="primary small" onClick={() => answer(p.id as string, true)}>{t("run.approveWrite")}</button>
              <button className="danger small" onClick={() => answer(p.id as string, false)}>{t("run.reject")}</button>
            </div>
          </div>
        ))}
        {running && !pending.length && <div className="ev tool">{t("run.working")}</div>}
        {paused && (
          <div className="gate-card step">
            <b>{run?.target && run.mode === "until" ? t("run.pausedUntil", { target: run.target }) : t("run.pausedAfter")}</b>
            <p className="small muted">{t("run.pausedHelp")}</p>
            <div className="row">
              <button className="primary small" onClick={() => cont("step")}>{t("run.nextStep")}</button>
              <button className="small" onClick={() => cont("full")}>{t("run.continueToEnd")}</button>
              <button className="danger small" onClick={() => run && post(`/api/runs/${run.id}/stop`).then(() => setRun({ ...run, status: "stopped" }))}>{t("run.finish")}</button>
            </div>
          </div>
        )}
        {gate && gate.kind === "human" && (
          <div className="gate-card">
            <b>{t("run.humanApproval", { label: gate.label })}</b>
            <p className="small">{gate.reason}</p>
            <GrowingTextarea minRows={3} placeholder={t("run.gateComment")} value={comment} onChange={(e) => setComment(e.target.value)} />
            <div className="row">
              <button className="primary small" onClick={() => decide("approve")}>{t("run.approve")}</button>
              <button className="small" onClick={() => decide("answer")} disabled={!comment.trim()}>{t("run.answerContinue")}</button>
              <button className="danger small" onClick={() => decide("reject")}>{t("run.rejectGate")}</button>
            </div>
            <p className="muted small">{t("run.gateHelp")}</p>
          </div>
        )}
        {!running && run && lastEnd?.context === "suggest" && (
          <div className="gate-card ctx-card">
            <b>{t("run.ctxTitle")}</b>
            <p className="small muted">{t("run.ctxHelp")}</p>
            <div className="row">
              <button className="primary small" onClick={updateContext}>{t("run.ctxUpdate")}</button>
            </div>
          </div>)}
        {!running && askable.length > 0 && (
          <div className="gate-card ask-card">
            <b>{t("run.askTitle")}</b>
            <p className="small muted">{t("run.askHelp")}</p>
            {(askable.length > 1 || askable[0] !== "") && (
              <label className="row small">
                {t("run.askTo")}
                <select value={askTo} onChange={(e) => setAskTo(e.target.value)}>
                  {askable.map((a) => <option key={a} value={a}>{a || t("run.askFlow")}</option>)}
                </select>
              </label>)}
            <GrowingTextarea minRows={2} placeholder={t("run.askPlaceholder")} value={askText}
              onChange={(e) => setAskText(e.target.value)}
              onKeyDown={(e) => { if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) { e.preventDefault(); ask(); } }} />
            <div className="row">
              <button className="primary small" disabled={!askText.trim()} onClick={ask} title="Ctrl+Enter">{t("run.askSend")}</button>
            </div>
          </div>)}
        <div ref={end} />
      </div>

      {(arts.length > 0 || outputs.length > 0) && (
        <div className={`run-arts${outsOpen || artsOpen ? "" : " folded"}`}
          style={artsH !== null && (outsOpen || artsOpen) ? { height: artsH, maxHeight: "none" } : undefined}>
          {(outsOpen || artsOpen) && <Splitter edge="top" size={artsH ?? 200} onSize={resizeArts} label={t("run.resizeOutputs")} />}
          {outputs.length > 0 && <>
            <div className="row out-head">
              <h2 className="fold-h"><button className="fold" aria-expanded={outsOpen} onClick={() => setOutsOpen(!outsOpen)}>
                {outsOpen ? "▾" : "▸"} {t("run.outputs", { input: `${wsDir}${rid}` })}</button></h2>
              <button className="small" title={t("run.openFolderTitle")} onClick={openFolder}>{t("run.openFolder")}</button>
            </div>
            {outsOpen && <div className="outs">{outputs.map(([agent, o]) => (
              <details key={agent} className="out-group" open={agent === lastOut}>
                <summary><b>{agent}</b> <span className="muted small">{Object.keys(o).join(", ")}</span></summary>
                {Object.entries(o).map(([n, v]) => (
                  <div key={n} className="out-row">
                    <div className="row">
                      <code>{n}</code>
                      <span className="out-actions">
                        <button className="small" onClick={() => setShown({ title: `${agent}.${n}`, value: v, md: isMarkdown(agent, n) })}>{t("run.outOpen")}</button>
                        <button className="small" onClick={() => copyValue(v, `${agent}.${n}`)}>{t(copied === `${agent}.${n}` ? "run.outCopied" : "run.outCopy")}</button>
                        <button className="small" onClick={() => download(`${agent}.${n}`, v, isMarkdown(agent, n))}>{t("run.outDownload")}</button>
                      </span>
                    </div>
                    <pre className="mono art-view">{pretty(v)}</pre>
                  </div>))}
              </details>))}
            </div>}
          </>}
          {arts.length > 0 && <>
            <h2 className="fold-h"><button className="fold" aria-expanded={artsOpen} onClick={() => setArtsOpen(!artsOpen)}>
              {artsOpen ? "▾" : "▸"} {t("run.artifacts", { input: `${wsDir}${run?.input ?? ""}` })}</button></h2>
            {artsOpen && <div className="arts">{arts.map((a) => (
              <button key={a.name} className={`small${view?.name === a.name ? " on" : ""}`} onClick={() => open(a.name)}>{a.name}</button>
            ))}</div>}
            {artsOpen && view && <pre className="mono art-view">{view.name.endsWith(".json") ? (() => { try { return JSON.stringify(JSON.parse(view.text), null, 2); } catch { return view.text; } })() : view.text}</pre>}
          </>}
        </div>
      )}
      {shown && <OutputViewer {...shown} copied={copied === shown.title} onClose={() => setShown(null)} onCopy={() => copyValue(shown.value, shown.title)}
        onDownload={() => download(shown.title, shown.value, shown.md)} />}
    </div>
  );
}

/** One output in a large view: Markdown formatted (or the plain text), with copy and download. */
function OutputViewer({ title, value, md, copied, onClose, onCopy, onDownload }: {
  title: string; value: unknown; md: boolean; copied: boolean; onClose: () => void; onCopy: () => void; onDownload: () => void;
}) {
  const { t } = useI18n();
  const [raw, setRaw] = useState(!md);
  useEffect(() => {
    const k = (e: KeyboardEvent) => e.key === "Escape" && onClose();
    window.addEventListener("keydown", k);
    return () => window.removeEventListener("keydown", k);
  }, [onClose]);
  const text = pretty(value);
  return (
    <div className="modal" onMouseDown={(e) => e.target === e.currentTarget && onClose()}>
      <div className="modal-box out-viewer" role="dialog" aria-label={title}>
        <div className="panel-head">
          <h2><code>{title}</code></h2>
          {md && <button className="small" onClick={() => setRaw((x) => !x)}>{t(raw ? "run.viewFormatted" : "run.viewRaw")}</button>}
          <button className="small" onClick={onCopy}>{t(copied ? "run.outCopied" : "run.outCopy")}</button>
          <button className="small" onClick={onDownload}>{t("run.outDownload")}</button>
          <button className="small" onClick={onClose}>{t("conn.close")}</button>
        </div>
        <div className="out-viewer-body">
          {raw || typeof value !== "string" ? <pre className="mono">{text}</pre> : <Markdown text={value} />}
        </div>
      </div>
    </div>
  );
}

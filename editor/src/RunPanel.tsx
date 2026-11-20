import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { api } from "./api";
import { useDialog } from "./Dialog";
import { useI18n, type I18n } from "./i18n";
import { flowInputs, inputKind, isTyped, sourceIO } from "./model";
import { formValue, sendValue, ValueField, type FormVal } from "./RunForm";
import type { Doc, FlowRaw, IOTypeRaw, RunIO } from "./types";

const MUTATE = { "Content-Type": "application/json", "X-Flow-Editor": "1" };
const RUN_ID = /^[A-Za-z0-9_-]{1,40}$/;  // same rule as the runner

export type NodeRun = "active" | "done" | "error" | "gate" | "skipped" | "stopped";
type Ev = { i: number; type: string; [k: string]: unknown };
type RunInfo = { id: string; status: string; gate: { gate: string; kind: string; label: string; reason: string } | null; cost: number; input: string; mode?: string; target?: string | null };
export type RunMode = "full" | "step" | "until" | "agent";
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
    const a = e.agent as string | undefined;
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

const status = (i18n: I18n, s: unknown) => i18n.tOr(`run.status.${String(s)}`, String(s));

function line(e: Ev, i18n: I18n): { cls: string; text: string } | null {
  const { t, tOr } = i18n;
  const who = e.agent ? `${e.agent}: ` : "";
  const mode = String(e.mode ?? "full");
  switch (e.type) {
    case "start": return { cls: "info", text: `${t("run.ev.start", { flow: String(e.flow), input: String(e.input), mode: tOr(`run.mode.${mode}`, mode) })}${e.target ? ` ${e.target}` : ""}${e.execution === "graph" ? t("run.ev.graph") : ""}` };
    case "delegate": return { cls: "deleg", text: `→ ${e.agent}${e.task ? ` — ${e.task}` : ""}` };
    case "tool": return { cls: "tool", text: `${who}${e.tool}${e.artifact ? ` (${e.artifact})` : ""}` };
    case "tool_error": return { cls: "err", text: t("run.ev.toolError", { who }) };
    case "agent_done": return { cls: e.error ? "err" : "ok", text: t(e.error ? "run.ev.agentDoneError" : "run.ev.agentDone", { agent: String(e.agent) }) };
    case "text": return { cls: e.agent ? "sub" : "say", text: `${who}${e.text}` };
    case "gate": return { cls: "gate", text: `⏸ ${e.label} — ${e.reason}` };
    case "approval": return { cls: "gate", text: t("run.ev.approval", { agent: String(e.agent), summary: String(e.summary) }) };
    case "approval_result": return { cls: e.approved ? "ok" : "err", text: `${t(e.approved ? "run.ev.approved" : "run.ev.rejected")}: ${e.summary}${e.comment ? ` — ${e.comment}` : ""}` };
    case "agent_blocked": return null;
    case "continue": return { cls: "info", text: t(e.how === "full" ? "run.ev.continueFull" : e.how === "auto" ? "run.ev.continueAuto" : "run.ev.continueStep") };
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

export function RunPanel({ flow, doc, dirty, mock, onTrace, request }: {
  flow: string; doc: Doc; dirty: boolean; mock: boolean; onTrace: (t: Record<string, NodeRun>) => void;
  request?: RunRequest | null;
}) {
  const i18n = useI18n();
  const { t } = i18n;
  const dialog = useDialog();
  const spec = doc.spec;
  const typed = isTyped(spec);
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
  const rid = typed ? runId.trim() : input.trim();  // the run folder in runs/

  useEffect(() => { onTrace(traceOf(events)); end.current?.scrollIntoView({ block: "nearest" }); }, [events, onTrace]);
  useEffect(() => () => onTrace({}), [onTrace]);

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
      ...(Object.keys(g).length ? { given: g } : {}) };
    return { body: typed ? { ...base, run_id: runId.trim() || null, inputs: out } : { ...base, input: input.trim() } };
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
        const o = d as { inputs?: Record<string, string>; given?: Record<string, string>; run_id?: string };
        if (o.inputs) setFieldErr(o.inputs);
        if (o.given) setFieldErr(Object.fromEntries(Object.entries(o.given).map(([k, v]) => [`given.${k}`, v])));
        if (o.run_id) setRunIdErr(o.run_id);
        setError(t("run.form.fix"));
      } else setError(String((e as Error).message));
    }
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
  const gate = run?.status === "gate" ? run.gate : null;
  const paused = run?.status === "paused";
  const agents = Object.keys(spec.agents);
  const savedInputs = typed && saved && Object.keys(saved.inputs).length > 0;
  const outputs = useMemo(() => Object.entries(saved?.outputs ?? {}).filter(([, o]) => Object.keys(o).length), [saved]);
  // the Claude Code CLI is not signed in: the runner marks these errors with hint "auth"; the same
  // test runs here too, so the card also shows with events from an older server
  const authErr = events.find((e) => e.hint === "auth"
    || (["end", "agent_done", "result", "text"].includes(e.type) && AUTH_ERROR.test(String(e.message ?? e.text ?? ""))));

  const runButton = running
    ? <button className="danger" onClick={() => run && post(`/api/runs/${run.id}/stop`)}>{t("run.stop")}</button>
    : <button className="primary" disabled={dirty || (typed ? !RUN_ID.test(rid) : !rid)} onClick={start}
      title={dirty ? t("run.saveFirstTitle") : t("run.run")}>{t("run.run")}</button>;
  return (
    <div className="runp">
      <div className="run-head">
        {running && <div className="kit-bar global" aria-hidden="true" />}
        {typed ? <>
          <div className="row">
            <input className={`mono${runIdErr || (rid && !RUN_ID.test(rid)) ? " bad" : ""}`} value={runId} disabled={running}
              title={t("run.runIdTitle")} aria-label={t("run.runId")} placeholder={t("run.runId")}
              onChange={(e) => { setRunId(e.target.value); setRunIdErr(null); }} />
            <button className="icon" disabled={running} title={t("run.newRunId")} onClick={() => setRunId(defaultRunId(flow))}>↻</button>
            {runButton}
          </div>
          {(runIdErr || (rid && !RUN_ID.test(rid))) && <span className="field-err">{runIdErr ?? t("run.runIdHint")}</span>}
        </> : (
          <div className="row">
            <input placeholder={t("run.inputPlaceholder", { label })} value={input} disabled={running}
              onChange={(e) => setInput(e.target.value)} onKeyDown={(e) => e.key === "Enter" && start()} />
            {runButton}
          </div>)}
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
                onChange={(v) => { setValues((p) => ({ ...p, [n]: v })); setFieldErr(({ [n]: _, ...rest }) => rest); }} />
            ))}
          </details>)}
        {mode === "agent" && <>
          <textarea rows={2} disabled={running} placeholder={t("run.instruction")}
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
        {events.map((e) => { const l = line(e, i18n); return l && <div key={e.i} className={`ev ${l.cls}`}>{l.text}</div>; })}
        {pending.map((p) => (
          <div key={p.id as string} className="gate-card write">
            <b>{t("run.writeBy", { agent: String(p.agent), env: String(p.environment) })}</b>
            <p className="small mono">{String(p.summary)}</p>
            <details open><summary className="small">{t("run.detail")}</summary>
              <pre className="mono art-view">{JSON.stringify(p.detail, null, 2)}</pre></details>
            <textarea rows={2} placeholder={t("run.writeComment")} value={comment} onChange={(e) => setComment(e.target.value)} />
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
            <textarea rows={3} placeholder={t("run.gateComment")} value={comment} onChange={(e) => setComment(e.target.value)} />
            <div className="row">
              <button className="primary small" onClick={() => decide("approve")}>{t("run.approve")}</button>
              <button className="small" onClick={() => decide("answer")} disabled={!comment.trim()}>{t("run.answerContinue")}</button>
              <button className="danger small" onClick={() => decide("reject")}>{t("run.rejectGate")}</button>
            </div>
            <p className="muted small">{t("run.gateHelp")}</p>
          </div>
        )}
        <div ref={end} />
      </div>

      {(arts.length > 0 || outputs.length > 0) && (
        <div className="run-arts">
          {outputs.length > 0 && <>
            <h2>{t("run.outputs", { input: rid })}</h2>
            <div className="outs">{outputs.map(([agent, o]) => (
              <details key={agent} className="out-group">
                <summary><b>{agent}</b> <span className="muted small">{Object.keys(o).join(", ")}</span></summary>
                {Object.entries(o).map(([n, v]) => (
                  <div key={n} className="out-row">
                    <code>{n}</code>
                    <pre className="mono art-view">{pretty(v)}</pre>
                  </div>))}
              </details>))}
            </div>
          </>}
          {arts.length > 0 && <>
            <h2>{t("run.artifacts", { input: run?.input ?? "" })}</h2>
            <div className="arts">{arts.map((a) => (
              <button key={a.name} className={`small${view?.name === a.name ? " on" : ""}`} onClick={() => open(a.name)}>{a.name}</button>
            ))}</div>
            {view && <pre className="mono art-view">{view.name.endsWith(".json") ? (() => { try { return JSON.stringify(JSON.parse(view.text), null, 2); } catch { return view.text; } })() : view.text}</pre>}
          </>}
        </div>
      )}
    </div>
  );
}

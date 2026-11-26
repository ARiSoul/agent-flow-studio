// The New run view: what to run (a mode, and a step or a saved run when the mode needs one), the
// run id and the inputs; Run stays at the bottom. "Run from step" shows, from the server, the
// state of each step in the saved run and what the new run keeps and runs again.
import { useCallback, useEffect, useState } from "react";
import { api } from "./api";
import { useDialog } from "./Dialog";
import { GrowingTextarea } from "./fields";
import { useI18n, type I18n } from "./i18n";
import { localizeServer, type ServerMsg } from "./i18n/server";
import { flowInputs, inputKind, isTyped, sourceIO } from "./model";
import { formValue, sendValue, ValueField, type FormVal } from "./RunForm";
import { oneLine, statusText, when } from "./RunParts";
import type { PastRun } from "./RunHistory";
import { ApiError, runApi } from "./runActions";
import type { Doc, FlowRaw, IOTypeRaw, RunIO } from "./types";

export type RunMode = "full" | "step" | "until" | "agent" | "from";  // from: an earlier run, from a step
export type RunInfo = { id: string; status: string; gate: { gate: string; kind: string; label: string; reason: string; message?: string } | null;
  cost: number; input: string; mode?: string; target?: string | null; workspace?: string; environment?: string | null;
  execution?: string; flow?: string;
  /** A decided run whose decision is being changed: held until then (seconds, epoch). */
  held_until?: number };
/** What to put in the form: a mode, a step, a saved run, inputs (from "Edit and run", "Run from step"…). */
export type Prefill = { nonce: number; mode: RunMode; target?: string; base?: string; inputs?: Record<string, unknown> };
type FromPlan = { status: string; states: Record<string, string>; reuse?: string[]; redo?: string[]; unreached?: string[] };

export const RUN_ID = /^[A-Za-z0-9_-]{1,40}$/;  // same rule as the runner
const ra = runApi();
export { ApiError };
export const post = <T,>(url: string, body?: unknown): Promise<T> => ra.call<T>(url, "POST", body);

/** A run waiting in the queue (GET /api/runs-queue), and what happened to the ones that left it. */
/** What keeps a queued run from starting: the run that goes, or a run that waits for the person. */
export type Hold = { kind: "running" | "gate" | "paused" | "approval"; input: string; flow: string; title?: string; label?: string; since?: number | null };
export type Queued = { id: string; flow: string; name: string; mode: string; queued: number; place: number; held_by: Hold[] };
export type QueueLog = { id: string; flow: string; name: string; ts: number; started?: string; input?: string; error?: string };

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

/** The body of a POST /api/runs for these values (also used by "Re-run"). */
export function runBody(spec: FlowRaw, flow: string, runId: string, inputs: Record<string, unknown>, extra: Record<string, unknown> = {}) {
  return isTyped(spec) ? { flow, mode: "full", run_id: runId, inputs, ...extra } : { flow, mode: "full", input: runId, ...extra };
}

export function SetupView({ flow, doc, dirty, mock, running, past, openRun, prefill, onStarted, queue, queueLog, onQueued, onUnqueue, onOpenQueued }: {
  flow: string; doc: Doc; dirty: boolean; mock: boolean; running: boolean; past: PastRun[];
  openRun?: string; prefill: Prefill | null; onStarted: (r: RunInfo) => void;
  /** Runs that wait to start (only one runs at a time): while one runs, Run puts the new one here. */
  queue: Queued[]; queueLog: QueueLog[]; onQueued: () => void; onUnqueue: (id: string) => void; onOpenQueued: (input: string) => void;
}) {
  const i18n = useI18n();
  const { t, lang } = i18n;
  const dialog = useDialog();
  const spec = doc.spec;
  const typed = isTyped(spec);
  const graph = spec.execution === "graph";
  const plain = !typed && !spec.input && !spec.task;
  const idOnly = typed || plain;
  const [mode, setMode] = useState<RunMode>("full");
  const [target, setTarget] = useState("");
  const [instruction, setInstruction] = useState("");
  const [input, setInput] = useState("");  // legacy flows: the one input (also the run id)
  const [runId, setRunId] = useState(() => defaultRunId(flow));
  const [baseRun, setBaseRun] = useState("");
  const [values, setValues] = useState<Record<string, FormVal>>(
    () => Object.fromEntries(Object.entries(spec.inputs ?? {}).map(([n, x]) => [n, formValue(x, x.default)])));
  const [given, setGiven] = useState<Record<string, FormVal>>({});
  const [fieldErr, setFieldErr] = useState<Record<string, string>>({});
  const [handoff, setHandoff] = useState<Record<string, { flow: string; run: string; agent: string; output: string }>>({});
  const [runIdErr, setRunIdErr] = useState<string | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [note, setNote] = useState<string | null>(null);
  const [baseSaved, setBaseSaved] = useState<RunIO | null>(null);
  const [baseArts, setBaseArts] = useState<string[]>([]);
  const [plan, setPlan] = useState<FromPlan | null>(null);
  const [inputChoice, setInputChoice] = useState<"saved" | "manual">("saved");
  const label = spec.input?.label ?? t("insp.inputDefault");
  const agentLike = mode === "agent" || mode === "from";
  const needsTarget = mode === "until" || agentLike;
  const bases = past.filter((p) => p.status !== "running");
  const newRid = agentLike && baseRun ? baseRun : idOnly ? runId.trim() : input.trim();

  // from outside: "Edit and run", "Run from step", the canvas menu
  useEffect(() => {
    if (!prefill) return;
    setMode(prefill.mode); setTarget(prefill.target ?? ""); setErr(null); setNote(null);
    if (prefill.base !== undefined) setBaseRun(prefill.base);
    if (prefill.mode !== "from" && prefill.mode !== "agent") setRunId(defaultRunId(flow));
    if (prefill.inputs) {
      const files = Object.entries(spec.inputs ?? {}).filter(([n, x]) => x.type === "file" && n in prefill.inputs!).map(([n]) => n);
      setValues((v) => ({ ...v, ...Object.fromEntries(Object.entries(spec.inputs ?? {})
        .filter(([n, x]) => x.type !== "file" && n in prefill.inputs!).map(([n, x]) => [n, formValue(x, prefill.inputs![n])])) }));
      if (files.length) setNote(t("rp.filesAgain", { names: files.join(", ") }));
      setHandoff({});
    }
  }, [prefill]);  // eslint-disable-line react-hooks/exhaustive-deps -- only when a new prefill comes
  // a saved run that is gone (deleted) or running cannot be the base; pick one when the mode needs it
  useEffect(() => {
    if (baseRun && !bases.some((p) => p.input === baseRun)) setBaseRun("");
    else if (mode === "from" && !baseRun && bases.length) setBaseRun(bases.some((p) => p.input === openRun) ? openRun! : bases[0].input);
    else if (mode === "agent" && !baseRun && openRun && bases.some((p) => p.input === openRun)) setBaseRun(openRun);
  }, [mode, past, openRun]);  // eslint-disable-line react-hooks/exhaustive-deps -- when the mode or the list changes
  const loadIO = useCallback((inp: string, set: (v: RunIO | null) => void) => {
    if (!RUN_ID.test(inp)) { set(null); return; }
    api.io(inp, flow).then(set).catch(() => set(null));
  }, [flow]);
  useEffect(() => {
    if (!agentLike || !baseRun) { setBaseSaved(null); setBaseArts([]); return; }
    loadIO(baseRun, setBaseSaved);
    fetch(`/api/artifacts/${encodeURIComponent(baseRun)}`).then((r) => (r.ok ? r.json() : []))
      .then((a: { name: string }[]) => setBaseArts(a.map((x) => x.name))).catch(() => setBaseArts([]));
  }, [agentLike, baseRun, loadIO]);
  // "run from step": the base run's step states and the server's plan for the chosen step
  useEffect(() => {
    if (mode !== "from" || !baseRun) { setPlan(null); return; }
    let live = true;
    fetch(`/api/runs/${encodeURIComponent(baseRun)}/from-plan${target ? `?node=${encodeURIComponent(target)}` : ""}`)
      .then((r) => (r.ok ? r.json() : null)).then((p) => { if (live) setPlan(p); }).catch(() => { if (live) setPlan(null); });
    return () => { live = false; };
  }, [mode, baseRun, target]);

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
  useEffect(() => {  // new flow inputs (edited in the Inspector) get their default
    setValues((v) => {
      const next = { ...v };
      for (const [n, x] of Object.entries(spec.inputs ?? {})) if (!(n in next)) next[n] = formValue(x, x.default);
      return next;
    });
  }, [spec.inputs]);

  const targetInputs = agentLike && target && spec.agents[target] ? Object.keys(spec.agents[target].inputs ?? {}) : [];
  const haveSaved = mode === "from" && !!target && targetInputs.every((n) =>
    spec.agents[target].inputs![n].required === false || savedInput(spec, baseSaved, target, n) !== undefined);
  useEffect(() => { setInputChoice(haveSaved ? "saved" : "manual"); }, [mode, target, haveSaved]);
  const targetKey = `${target}|${targetInputs.join(",")}`;
  useEffect(() => {
    if (!agentLike || !target || !spec.agents[target]) { setGiven({}); return; }
    setGiven(Object.fromEntries(targetInputs.map((n) =>
      [n, formValue(agentInputIO(spec, target, n), savedInput(spec, baseSaved, target, n))])));
  }, [mode, targetKey, baseSaved]);  // eslint-disable-line react-hooks/exhaustive-deps -- refill when the agent or the base run changes
  const needs = mode === "agent" && target ? spec.agents[target]?.reads ?? [] : [];
  const missing = needs.filter((n) => !baseArts.includes(n));

  const collect = (): Record<string, unknown> | null => {
    const errs: Record<string, string> = {};
    const out: Record<string, unknown> = {};
    if (typed && !agentLike) for (const [n, x] of Object.entries(spec.inputs ?? {})) {
      if (hiddenIn.has(n)) continue;
      const r = sendValue(x, values[n] ?? formValue(x, x.default));
      if (r.error) errs[n] = t("run.form.badJson");
      else if (r.value !== undefined) out[n] = r.value;
    }
    const g: Record<string, unknown> = {};
    for (const n of mode === "from" && inputChoice === "saved" ? [] : targetInputs) {
      const x = agentInputIO(spec, target, n);
      if (x.type === "file") continue;
      const r = sendValue(x, given[n] ?? "");
      if (r.error) errs[`given.${n}`] = t("run.form.badJson");
      else if (r.value !== undefined && !same(r.value, sourceValue(spec, baseSaved, target, n))) g[n] = r.value;
    }
    setFieldErr(errs);
    if (Object.keys(errs).length) return null;
    const base = { flow, mode, target: target || null, instruction: mode === "agent" ? instruction : "",
      ...(Object.keys(g).length ? { given: g } : {}), ...(typed && Object.keys(handoff).length ? { handoff } : {}) };
    return typed ? { ...base, run_id: newRid || null, inputs: out } : { ...base, input: newRid };
  };
  const canStart = !dirty && (needsTarget ? !!target : true)
    && (mode === "from" ? !!baseRun : idOnly ? RUN_ID.test(newRid) : !!newRid);
  const start = async () => {
    setErr(null); setRunIdErr(null);
    if (!canStart) return;
    const body = collect();
    if (!body) return setErr(t("run.form.fix"));
    if (usesExtensionTools(spec) && !mock && !(await dialog.confirm({ title: t("run.realTitle"), message: t("run.confirmReal"), ok: t("run.realOk"), danger: true }))) return;
    try {
      if (running) {  // one run at a time: this one starts by itself when the current one stops
        await ra.queue(body);
        if (!agentLike) setRunId(defaultRunId(flow));
        setNote(null);
        onQueued();
        return;
      }
      const r = await ra.start<RunInfo>(body);
      if (!agentLike) setRunId(defaultRunId(flow));  // the next run gets a new id
      setNote(null);
      onStarted(r);
    } catch (e) {
      const d = e instanceof ApiError && e.status === 422 ? e.detail : null;
      if (d && typeof d === "object" && !Array.isArray(d)) {
        const o = d as { inputs?: Record<string, string>; given?: Record<string, string>; run_id?: string;
          codes?: Record<string, ServerMsg>; given_codes?: Record<string, ServerMsg> };
        const here = (texts: Record<string, string>, codes: Record<string, ServerMsg> | undefined) =>
          Object.entries(texts).map(([k, v]) => [k, localizeServer(codes?.[k], v, lang)] as const);
        if (o.inputs) setFieldErr(Object.fromEntries(here(o.inputs, o.codes)));
        if (o.given) setFieldErr(Object.fromEntries(here(o.given, o.given_codes).map(([k, v]) => [`given.${k}`, v])));
        if (o.run_id) setRunIdErr(o.run_id);
        setErr(t("run.form.fix"));
      } else setErr(String((e as Error).message));
    }
  };

  /** "From a run…": the value of an output of a finished run of another flow of this workspace. */
  const fillFromRun = async (n: string, x: IOTypeRaw) => {
    setErr(null);
    const all = (await api.flows().catch(() => [])).filter((f) => f.name !== flow);
    let src = x.source?.flow && all.some((f) => f.name === x.source!.flow) ? x.source.flow : null;
    if (!src) {
      if (!all.length) return setErr(t("run.fill.noFlows"));
      const r = await dialog.form({ title: t("run.fill.pickFlow", { input: x.label || n }), ok: t("app.next"), fields: [
        { key: "flow", label: t("run.fill.flow"), value: all[0].name,
          options: all.map((f) => ({ value: f.name, label: `${f.title} (${f.name})` })) }] });
      if (!r) return;
      src = r.flow;
    }
    const from = src;
    let runs;
    try { runs = await api.handoffRuns(from, x.type ?? "text"); } catch (e) { return setErr(String((e as Error).message ?? e)); }
    if (!runs.length) return setErr(t("run.fill.noRuns", { flow: from }));
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
    } catch (e) { setErr(String((e as Error).message ?? e)); }
  };

  const modes: { v: RunMode; off?: string }[] = [
    { v: "full" }, { v: "step" }, { v: "until" }, { v: "agent" },
    { v: "from", off: !graph ? t("rp.fromGraphOnly") : !bases.length ? t("rp.fromNoRuns") : undefined },
  ];
  const off = modes.find((m) => m.v === mode)?.off;
  const stepOptions = mode === "from"
    ? [...Object.keys(spec.agents), ...Object.entries(spec.gates ?? {}).filter(([, g]) => g.kind === "human").map(([n]) => n)]
    : Object.keys(spec.agents);
  const baseInfo = bases.find((p) => p.input === baseRun);
  return (
    <>
      <div className="rp-body" onKeyDown={(e) => { if (e.key === "Enter" && (e.ctrlKey || e.metaKey) && canStart) { e.preventDefault(); start(); } }}>
        <div className="field">
          <label className="field-label" htmlFor="rp-mode">{t("rp.whatToRun")}</label>
          <select id="rp-mode" value={mode} aria-describedby="rp-mode-hint"
            onChange={(e) => { setMode(e.target.value as RunMode); setErr(null); }}>
            {modes.map(({ v, off: o }) => <option key={v} value={v} disabled={!!o}>{t(`rp.mode.${v}`)}</option>)}
          </select>
          <span className="field-hint" id="rp-mode-hint">{off ?? t(`rp.mode.${mode}.hint`)}</span>
        </div>
        {agentLike && (
          <div className="field">
            <label className="field-label" htmlFor="rp-base">{t(mode === "from" ? "rp.baseRun" : "rp.agentRun")}</label>
            <select id="rp-base" aria-describedby="rp-base-hint" value={baseRun} onChange={(e) => setBaseRun(e.target.value)}>
              {mode === "agent" && <option value="">{t("rp.newEmptyRun")}</option>}
              {mode === "from" && !baseRun && <option value="">—</option>}
              {bases.map((p) => <option key={p.input} value={p.input}>
                {`${p.title || p.input} · ${statusText(i18n, p.status)} · ${when(p.started, lang)}`}</option>)}
            </select>
            <span className="field-hint" id="rp-base-hint">{t(mode === "from" ? "rp.baseRunHint" : "rp.agentRunHint")}</span>
            {mode === "from" && baseInfo?.status === "gate" && <span className="field-hint warn-text">{t("rp.baseAtGate", { gate: baseInfo.gate ?? "" })}</span>}
          </div>)}
        {needsTarget && (
          <div className="field">
            <label className="field-label" htmlFor="rp-step">{t("rp.step")}{" *"}</label>
            <select id="rp-step" value={target} onChange={(e) => setTarget(e.target.value)}>
              <option value="">{t("run.agentOption")}</option>
              {stepOptions.map((a) => <option key={a} value={a}>
                {a}{mode === "from" && plan?.states[a] ? ` — ${i18n.tOr(`rp.engine.${plan.states[a]}`, plan.states[a])}` : ""}</option>)}
            </select>
          </div>)}
        {mode === "from" && target && plan?.redo && (
          <div className="rp-preview small" aria-live="polite">
            <div><b className="rp-ok">✓ {t("rp.keeps")}</b> {plan.reuse?.join(", ") || "—"}</div>
            <div><b className="rp-redo">↻ {t("rp.repeats")}</b> {plan.redo.join(", ")}</div>
            {!!plan.unreached?.length && <div className="warn-text">{t("rp.unreached", { names: plan.unreached.join(", ") })}</div>}
            <div className="muted">{t("rp.sameFolder", { run: baseRun })}</div>
          </div>)}

        {(!agentLike || (mode === "agent" && !baseRun)) && (idOnly ? (
          <div className="field">
            <label className="field-label" htmlFor="rp-runid">{t("run.runId")}</label>
            <span className="row">
              <input id="rp-runid" className={`mono grow${runIdErr || (runId.trim() && !RUN_ID.test(runId.trim())) ? " bad" : ""}`} value={runId}
                aria-describedby="rp-runid-hint" onChange={(e) => { setRunId(e.target.value); setRunIdErr(null); }} />
              <button className="small" title={t("run.newRunId")} onClick={() => setRunId(defaultRunId(flow))}>{t("rp.newId")}</button>
            </span>
            {(runIdErr || (runId.trim() && !RUN_ID.test(runId.trim())))
              ? <span className="field-err" id="rp-runid-hint">{runIdErr ?? t("run.runIdHint")}</span>
              : <span className="field-hint" id="rp-runid-hint">{t("rp.runIdHint")}</span>}
          </div>
        ) : (
          <div className="field">
            <label className="field-label" htmlFor="rp-input">{label}</label>
            <input id="rp-input" placeholder={t("run.inputPlaceholder", { label })} value={input}
              onChange={(e) => setInput(e.target.value)} />
          </div>))}

        {note && <p className="small warn-text" role="status">{note}</p>}
        {typed && !agentLike && Object.keys(spec.inputs ?? {}).length > 0 && (
          <fieldset className="rp-inputs">
            <legend>{t("run.form.inputs")}</legend>
            {Object.entries(spec.inputs ?? {}).filter(([n]) => !hiddenIn.has(n)).map(([n, x]) => (
              <ValueField key={n} name={n} t={x} value={values[n] ?? formValue(x, x.default)}
                error={fieldErr[n]}
                onChange={(v) => { setValues((p) => ({ ...p, [n]: v })); setFieldErr(({ [n]: _, ...rest }) => rest);
                  setHandoff(({ [n]: _h, ...rest }) => rest); }}
                labelAction={x.type !== "file" && <button className="small link" title={t("run.fill.title")}
                  onClick={() => fillFromRun(n, x)}>{t("rp.fromRun")}</button>}
                extra={handoff[n] && <span className="badge" title={t("run.fill.fromTitle")}>← {handoff[n].flow}/{handoff[n].run} · {handoff[n].agent}.{handoff[n].output}</span>} />
            ))}
          </fieldset>)}

        {mode === "agent" && (
          <div className="field">
            <label className="field-label" htmlFor="rp-instr">{t("rp.instruction")}</label>
            <GrowingTextarea id="rp-instr" minRows={2} placeholder={t("run.instruction")}
              value={instruction} onChange={(e) => setInstruction(e.target.value)} />
          </div>)}
        {agentLike && target && targetInputs.length > 0 && (
          <fieldset className="rp-inputs">
            <legend>{t("run.form.agentInputs", { agent: target })}</legend>
            {mode === "from" && <div className="rp-choice" role="radiogroup" aria-label={t("run.form.agentInputs", { agent: target })}>
              <label className="small"><input type="radio" name="from-inputs" checked={inputChoice === "saved"}
                disabled={!haveSaved} onChange={() => setInputChoice("saved")} /> {t("run.fromSaved")}</label>
              <label className="small"><input type="radio" name="from-inputs" checked={inputChoice === "manual"}
                onChange={() => setInputChoice("manual")} /> {t("run.fromManual")}</label>
              {!haveSaved && <p className="muted small">{t("run.fromNoSaved")}</p>}
            </div>}
            {mode === "agent" && <p className="muted small">{t("run.form.agentInputsHint")}</p>}
            {mode === "from" && inputChoice === "saved" ? (
              <ul className="rp-kv small">{targetInputs.map((n) => {
                const v = savedInput(spec, baseSaved, target, n);
                return <li key={n} title={sourceLabel(spec, target, n, t).hint}><code>{n}</code>
                  <span className="muted">{v === undefined ? "—" : oneLine(v, 120)}</span></li>;
              })}</ul>
            ) : targetInputs.map((n) => {
              const x = agentInputIO(spec, target, n);
              const s = sourceLabel(spec, target, n, t);
              return x.type === "file"
                ? <div key={n} className="field run-field"><span className="field-label">{s.label}</span>
                  <span className="field-hint">{s.hint} · {t("run.form.agentFile")}</span></div>
                : <ValueField key={n} name={n} t={{ ...x, label: s.label, description: s.hint }} value={given[n] ?? ""}
                  error={fieldErr[`given.${n}`]}
                  onChange={(v) => { setGiven((p) => ({ ...p, [n]: v })); setFieldErr(({ [`given.${n}`]: _, ...rest }) => rest); }} />;
            })}
          </fieldset>)}
        {mode === "agent" && target && needs.length > 0 && (
          <p className={`small ${missing.length ? "field-err" : "muted"}`}>
            {missing.length ? t("run.missingInputs", { input: baseRun || t("rp.newEmptyRun"), names: missing.join(", ") })
              : t("run.presentInputs", { names: needs.join(", ") })}</p>)}
      </div>
      <div className="rp-foot">
        {usesExtensionTools(spec) && (
          <span className={`badge ${mock ? "" : "warn"}`} title={t("run.mockTitle")}>{mock ? t("run.mock") : t("run.real")}</span>)}
        {dirty && <span className="field-err small">{t("run.saveFirst")}</span>}
        {running && <span className="muted small">{t("rp.oneAtATime")}</span>}
        {err && <span className="field-err small" role="alert">{err}</span>}
        <button className="primary rp-go" disabled={!canStart} onClick={start}
          title={dirty ? t("run.saveFirstTitle") : running ? t("rp.queueTitle") : `${t("rp.go")} (Ctrl+Enter)`}>
          {running ? `⧗ ${t("rp.queueGo")}` : `▶ ${t(mode === "from" ? "rp.runFromStep" : mode === "agent" ? "rp.goAgent" : "rp.go")}`}</button>
      </div>
      {(queue.length > 0 || queueLog.length > 0) && (
        <section className="rp-queue small" aria-label={t("rp.queued")}>
          {queue.length > 0 && <><b>{t("rp.queued")}</b>
            <ol>{queue.map((q) => (
              <li key={q.id}><span className="mono">{q.place}.</span> <span className="grow">{q.name || q.flow}{q.flow !== flow ? ` (${q.flow})` : ""}</span>
                <button className="link small" onClick={() => onUnqueue(q.id)}>{t("rp.unqueue")}</button></li>))}</ol></>}
          {queueLog.map((q) => (
            <div key={q.id} className={q.error ? "field-err" : "muted"}>
              {q.name || q.flow}: {q.error ? t("rp.queueFailed", { error: q.error }) : t("rp.queueStarted")}
              {q.input && q.flow === flow && <button className="link small" onClick={() => onOpenQueued(q.input!)}>{t("rp.open")}</button>}
            </div>))}
        </section>)}
    </>);
}

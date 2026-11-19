import { useEffect, useRef, useState } from "react";
import { useI18n, type I18n } from "./i18n";
import type { Doc } from "./types";

const MUTATE = { "Content-Type": "application/json", "X-Flow-Editor": "1" };

export type NodeRun = "active" | "done" | "error" | "gate";
type Ev = { i: number; type: string; [k: string]: unknown };
type RunInfo = { id: string; status: string; gate: { gate: string; kind: string; label: string; reason: string } | null; cost: number; input: string; mode?: string; target?: string | null };
export type RunMode = "full" | "step" | "until" | "agent";
export type RunRequest = { mode: RunMode; target?: string; nonce: number };

async function post<T>(url: string, body?: unknown): Promise<T> {
  const r = await fetch(url, { method: "POST", headers: MUTATE, body: body ? JSON.stringify(body) : undefined });
  const data = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error((data as { detail?: string }).detail ?? r.statusText);
  return data as T;
}

/** State of each node from the events (for the canvas). */
export function traceOf(events: Ev[]): Record<string, NodeRun> {
  const s: Record<string, NodeRun> = {};
  for (const e of events) {
    const a = e.agent as string | undefined;
    if (e.type === "delegate" && a) {
      for (const k of Object.keys(s)) if (s[k] === "active") s[k] = "done";
      s[a] = "active";
    }
    if (e.type === "agent_done" && a) s[a] = e.error ? "error" : "done";
    if (e.type === "agent_blocked" && a) delete s[a];  // denied by step-by-step mode
    if (e.type === "gate") s[e.gate as string] = "gate";
    if (e.type === "decision") s[e.gate as string] = "done";
  }
  return s;
}

const status = (i18n: I18n, s: unknown) => i18n.tOr(`run.status.${String(s)}`, String(s));

function line(e: Ev, i18n: I18n): { cls: string; text: string } | null {
  const { t, tOr } = i18n;
  const who = e.agent ? `${e.agent}: ` : "";
  const mode = String(e.mode ?? "full");
  switch (e.type) {
    case "start": return { cls: "info", text: `${t("run.ev.start", { flow: String(e.flow), input: String(e.input), mode: tOr(`run.mode.${mode}`, mode) })}${e.target ? ` ${e.target}` : ""}${t(e.mock ? "run.ev.mock" : "run.ev.real")}` };
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
    case "result": return { cls: "info", text: `${t("run.ev.result", { session: String(e.session_id).slice(0, 8), turns: String(e.turns), cost: Number(e.cost ?? 0).toFixed(3) })}${e.denials ? t("run.ev.denials", { n: String(e.denials) }) : ""}` };
    case "end": return { cls: e.status === "error" ? "err" : "info", text: `${t("run.ev.end", { status: status(i18n, e.status) })}${e.message ? ` — ${e.message}` : ""}${e.note ? ` (${e.note})` : ""}` };
    default: return null;
  }
}

export function RunPanel({ flow, doc, dirty, mock, onTrace, request }: {
  flow: string; doc: Doc; dirty: boolean; mock: boolean; onTrace: (t: Record<string, NodeRun>) => void;
  request?: RunRequest | null;
}) {
  const i18n = useI18n();
  const { t } = i18n;
  const [mode, setMode] = useState<RunMode>("full");
  const [target, setTarget] = useState("");
  const [instruction, setInstruction] = useState("");
  const [inputs, setInputs] = useState<string[]>([]);  // artifacts that already exist for the input
  useEffect(() => {
    if (request) { setMode(request.mode); setTarget(request.target ?? ""); }
  }, [request]);
  const [input, setInput] = useState("");
  const [run, setRun] = useState<RunInfo | null>(null);
  const [events, setEvents] = useState<Ev[]>([]);
  const [comment, setComment] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [arts, setArts] = useState<{ name: string; size: number }[]>([]);
  const [view, setView] = useState<{ name: string; text: string } | null>(null);
  const [sent, setSent] = useState<Set<string>>(new Set());
  const evRef = useRef<Ev[]>([]);
  const end = useRef<HTMLDivElement>(null);
  const label = doc.spec.input?.label ?? t("insp.inputDefault");

  useEffect(() => { onTrace(traceOf(events)); end.current?.scrollIntoView({ block: "nearest" }); }, [events, onTrace]);
  useEffect(() => () => onTrace({}), [onTrace]);

  const loadArts = async (inp: string) => {
    const r = await fetch(`/api/artifacts/${encodeURIComponent(inp)}`);
    setArts(r.ok ? await r.json() : []);
  };

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
      }
    }
    loadArts(inp);
  };

  const start = async () => {
    setError(null);
    const inp = input.trim();
    if (!inp) return;
    if (!mock && !confirm(t("run.confirmReal"))) return;
    try {
      if ((mode === "until" || mode === "agent") && !target) throw new Error(t("run.pickAgent"));
      const r = await post<RunInfo>("/api/runs", { flow, input: inp, mode, target: target || null, instruction: mode === "agent" ? instruction : "" });
      evRef.current = []; setEvents([]); setView(null); setRun(r);
      await follow(r.id, inp);
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

  // artifacts the target agent needs ("single agent" mode)
  useEffect(() => {
    const inp = input.trim();
    if (mode !== "agent" || !/^[A-Za-z0-9_-]{1,40}$/.test(inp)) { setInputs([]); return; }
    const t = setTimeout(() => fetch(`/api/artifacts/${encodeURIComponent(inp)}`)
      .then((r) => (r.ok ? r.json() : [])).then((a: { name: string }[]) => setInputs(a.map((x) => x.name)))
      .catch(() => setInputs([])), 300);
    return () => clearTimeout(t);
  }, [mode, input]);
  const needs = mode === "agent" && target ? doc.spec.agents[target]?.reads ?? [] : [];
  const missing = needs.filter((n) => !inputs.includes(n));

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
  const agents = Object.keys(doc.spec.agents);
  return (
    <div className="runp">
      <div className="run-head">
        <div className="row">
          <input placeholder={t("run.inputPlaceholder", { label })} value={input} disabled={running}
            onChange={(e) => setInput(e.target.value)} onKeyDown={(e) => e.key === "Enter" && start()} />
          {running
            ? <button className="danger" onClick={() => run && post(`/api/runs/${run.id}/stop`)}>{t("run.stop")}</button>
            : <button className="primary" disabled={!input.trim() || dirty} onClick={start}
              title={dirty ? t("run.saveFirstTitle") : t("run.run")}>{t("run.run")}</button>}
        </div>
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
        {mode === "agent" && <>
          <textarea rows={2} disabled={running} placeholder={t("run.instruction")}
            value={instruction} onChange={(e) => setInstruction(e.target.value)} />
          {target && needs.length > 0 && (
            <p className={`small ${missing.length ? "field-err" : "muted"}`}>
              {missing.length ? t("run.missingInputs", { input: input || t("run.inputFolder"), names: missing.join(", ") })
                : t("run.presentInputs", { names: needs.join(", ") })}</p>)}
        </>}
        <span className={`badge ${mock ? "" : "warn"}`}>{mock ? t("run.mock") : t("run.real")}</span>
        {dirty && <span className="muted small">{t("run.saveFirst")}</span>}
        {run && <span className="muted small"> · {status(i18n, run.status)}{run.cost ? ` · ≈ ${run.cost.toFixed(3)} USD` : ""}</span>}
        {error && <div className="field-err">{error}</div>}
      </div>

      <div className="run-log">
        {!run && <p className="muted">{t("run.intro")} <code>runs/{t("run.inputFolder")}/decisions.jsonl</code> {t("run.introEnd")}</p>}
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

      {arts.length > 0 && (
        <div className="run-arts">
          <h2>{t("run.artifacts", { input: run?.input ?? "" })}</h2>
          <div className="arts">{arts.map((a) => (
            <button key={a.name} className={`small${view?.name === a.name ? " on" : ""}`} onClick={() => open(a.name)}>{a.name}</button>
          ))}</div>
          {view && <pre className="mono art-view">{view.name.endsWith(".json") ? (() => { try { return JSON.stringify(JSON.parse(view.text), null, 2); } catch { return view.text; } })() : view.text}</pre>}
        </div>
      )}
    </div>
  );
}

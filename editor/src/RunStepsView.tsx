// The steps of a run as a pipeline: one row per step (state, time, cost), steps that run at the
// same time (or that a decision picks from) grouped, a bar with when each step ran, and the step's
// detail on demand: what it received (inputs and instructions), its attempts, tokens and model,
// what it did (tool calls with their outcome), a gate's decision, and links to its outputs.
import { useEffect, useMemo, useRef, useState } from "react";
import { useI18n } from "./i18n";
import { Markdown } from "./Markdown";
import { line, outcomeText } from "./RunLog";
import { Clamp, clock, fullTime, MoreMenu, oneLine, useCopy } from "./RunParts";
import { count, duration, groups, shortModel, toolOutcome, usd, type Ev, type Step, type StepState } from "./runSteps";
import type { FlowRaw } from "./types";

const ICON: Record<StepState, string> = { pending: "○", running: "●", retrying: "⟳", paused: "⏸", done: "✓", failed: "✗", waiting: "⏸", skipped: "–", interrupted: "■" };

/** What a step received: each input with where it comes from and its value (undefined: none saved). */
export type StepInput = { name: string; from?: string; value: unknown };
/** A tool call read on demand from the local session (GET /api/runs/<id>/tools/<tool>). */
export type ToolCall = { tool: string; input: string; result: string; result_size: number; cut: boolean; is_error: boolean };

export function StepsList({ spec, steps, events, span, now, running, reveal, focus, graph, askable, model, inputsOf, promptOf,
  onToolCall, onCanvas, onRunFrom, onAsk, onOutput, onLink, onClaude, onPause }: {
  spec: FlowRaw; steps: Step[]; events: Ev[]; span: [number, number] | null; now: number; running: boolean;
  /** A step to open and bring into view (a canvas click, a link): its nonce changes each time. */
  reveal: { node: string; nonce: number } | null;
  focus?: string | null; graph: boolean; askable: string[];
  model: (node: string) => string | null;
  inputsOf: (node: string) => StepInput[];
  /** The instructions of a step (its prompt text, or the text of its prompt file). */
  promptOf: (node: string) => string;
  onToolCall: (id: string) => Promise<ToolCall>;
  onCanvas?: (node: string) => void; onRunFrom?: (node: string) => void; onAsk?: (node: string) => void;
  onOutput: (agent: string, name: string) => void;
  /** A link to this step of this run (copied to the clipboard). */
  onLink?: (node: string) => string;
  /** Go on with this step's session in Claude Code: a terminal there, or the command to copy. */
  onClaude?: (node: string, how: "terminal" | "copy") => void;
  /** Pause this running step (graph runs): it goes on later in its own session. */
  onPause?: (node: string) => void;
}) {
  const i18n = useI18n();
  const { t, lang } = i18n;
  const [open, setOpen] = useState<Record<string, boolean>>({});
  const refs = useRef<Record<string, HTMLLIElement | null>>({});
  const outcome = useMemo(() => toolOutcome(events), [events]);
  const [copied, copy] = useCopy();
  const [calls, setCalls] = useState<Record<string, ToolCall | string | null>>({});  // null: loading; string: error
  const toggleCall = (id: string) => {
    if (id in calls) { setCalls(({ [id]: _, ...rest }) => rest); return; }
    setCalls((c) => ({ ...c, [id]: null }));
    onToolCall(id).then((r) => setCalls((c) => ({ ...c, [id]: r })))
      .catch((e) => setCalls((c) => ({ ...c, [id]: String((e as Error).message ?? e) })));
  };
  // only a failed step, one that waits for you, or the one chosen opens by itself
  const isOpen = (s: Step) => open[s.node] ?? (s.state === "failed" || s.state === "waiting");
  useEffect(() => {
    if (!reveal) return;
    setOpen((o) => ({ ...o, [reveal.node]: true }));
    requestAnimationFrame(() => refs.current[reveal.node]?.scrollIntoView({ block: "nearest", behavior: "smooth" }));
  }, [reveal]);
  const t0 = span?.[0];
  const total = span ? Math.max(0.001, span[1] - span[0]) : 0;
  const rel = (ts?: number) => (ts && t0 ? `+${duration(ts - t0) || "0.0 s"}` : "");

  const row = (s: Step) => {
    const on = isOpen(s);
    const live = s.state === "running" || s.state === "waiting";
    const est = s.state === "running" && s.live?.est ? `≈ ${usd(s.live.est)}` : "";
    const end = s.end ?? (live ? now / 1000 : undefined);
    const took = s.start && end ? duration(end - s.start) : "";
    const kind = s.kind === "agent" ? null : t(`rp.kind.${s.kind}`);
    const bar = span && s.start ? { left: `${((s.start - span[0]) / total) * 100}%`,
      width: `${Math.max(1, (((end ?? s.start) - s.start) / total) * 100)}%` } : null;
    const m = s.kind === "agent" ? s.model ?? model(s.node) : null;
    const a = spec.agents[s.node];
    const inputs = a ? inputsOf(s.node) : [];
    const prompt = a && !a.action && !a.subflow ? promptOf(s.node) : "";
    const menu = [
      ...(onPause && s.state === "running" && s.kind === "agent" ? [{ label: t("rp.pauseStep"), title: t("rp.pauseTitle"), onSelect: () => onPause(s.node) }] : []),
      ...(graph && onRunFrom && s.kind !== "stop" ? [{ label: t("rp.runFromStep"), title: t("rp.fromHereTitle"), onSelect: () => onRunFrom(s.node) }] : []),
      ...(onAsk && askable.includes(s.node) ? [{ label: t("rp.askThis"), onSelect: () => onAsk(s.node) }] : []),
      ...(onLink ? [{ label: t(copied === `l-${s.node}` ? "run.outCopied" : "rp.copyStepLink"), onSelect: () => copy(onLink(s.node), `l-${s.node}`) }] : []),
      ...(s.session && onClaude ? [{ kind: "sep" as const },
        { label: t("rp.ccOpen"), title: t("rp.ccOpenTitle"), onSelect: () => onClaude(s.node, "terminal") },
        { label: t("rp.ccCopy"), title: t("rp.ccCopyTitle"), onSelect: () => onClaude(s.node, "copy") }] : []),
      ...(s.session ? [{ label: t(copied === `s-${s.node}` ? "run.outCopied" : "rp.copySession"), title: s.session, onSelect: () => copy(s.session!, `s-${s.node}`) }] : []),
    ];
    return (
      <li key={s.node} ref={(el) => { refs.current[s.node] = el; }} className={`rp-step st-${s.state}${focus === s.node ? " focus" : ""}`}>
        <button className="rp-step-head" aria-expanded={on} onClick={() => setOpen((o) => ({ ...o, [s.node]: !on }))}>
          <span className="rp-ico" aria-hidden="true">{ICON[s.state]}</span>
          <span className="rp-step-name mono">{s.node}</span>
          {kind && <span className="badge">{kind}</span>}
          {s.state === "done" && s.retries > 0 ? <span className="badge warn" title={t("rp.recoveredTitle")}>↻ {i18n.tn("rp.recovered", s.retries)}</span>
            : s.attempts.length > 1 && <span className="badge" title={t("rp.attemptsTitle")}>×{s.attempts.length}</span>}
          <span className="grow" />
          <span className="rp-step-meta"><span className="sr-only">{t(`rp.state.${s.state}`)} </span>
            {s.state === "pending" ? t("rp.state.pending") : s.state === "retrying" && s.retry
              ? t("rp.retryIn", { t: duration(Math.max(0, s.retry.at - now / 1000)) || "0 s" })
              : [s.state === "waiting" ? t("rp.waited", { t: took }) : took,
              s.cost > 0 ? usd(s.cost) : est, s.live ? t("rp.liveOut", { n: count(s.live.out) }) : ""].filter(Boolean).join(" · ")}</span>
        </button>
        {bar && <div className="rp-bar" aria-hidden="true"><span style={bar} /></div>}
        {on && (
          <div className="rp-step-body">
            <div className="rp-step-facts small muted">
              <span className={`rp-state st-${s.state}`}>{t(`rp.state.${s.state}`)}</span>
              {s.start && <span title={fullTime(s.start, lang)}>{clock(s.start, lang)}{s.end ? `–${clock(s.end, lang)}` : ""}</span>}
              {s.turns > 0 && <span>{i18n.tn("rp.turns", s.turns)}</span>}
              {(s.tokens.in || s.tokens.out) ? <span title={t("rp.tokensTitle")}>{t("rp.tokens", { in: count(s.tokens.in), out: count(s.tokens.out) })}
                {s.tokens.cache_read ? ` · ${t("rp.cache", { n: count(s.tokens.cache_read) })}` : ""}</span> : null}
              {m && <span className="mono" title={m}>{shortModel(m)}</span>}
              {s.state === "running" && s.live && <span title={t("rp.liveTitle")}>{t("rp.live", { out: count(s.live.out), ctx: count(s.live.context) })}
                {est ? ` · ${t("rp.liveCost", { cost: usd(s.live.est) })}` : ""}</span>}
              {s.state === "running" && s.last && <span className={now / 1000 - s.last > 120 ? "warn-text" : ""}>{t("rp.lastActivity", { t: duration(now / 1000 - s.last) })}</span>}
            </div>
            {s.error && <div className="rp-err">{s.error}
              {s.transient && s.state === "failed" && <div className="rp-transient">{t(s.wroteNoRetry ? "rp.transientWrote" : "rp.transient")}</div>}</div>}
            {s.decision && (
              <div className="small"><b>{i18n.tOr(`run.decision.${s.decision.decision}`, s.decision.decision)}</b>
                {s.decision.waited !== undefined && <span className="muted"> · {t("rp.decidedAfter", { t: duration(s.decision.waited) || "0 s" })}</span>}
                {s.decision.comment && <span className="muted"> — “{s.decision.comment}”</span>}</div>)}
            {(inputs.length > 0 || prompt) && (
              <details className="rp-received">
                <summary className="small">{i18n.tn("rp.received", inputs.length)}</summary>
                {inputs.length > 0 && <ul className="rp-kv small">{inputs.map((x) => (
                  <li key={x.name} title={x.from ? `← ${x.from}` : t("run.form.freeInput")}><code>{x.name}</code>
                    <span className="muted">{x.value === undefined ? t("rp.noValue") : oneLine(x.value, 160)}
                      {x.from && <span className="rp-from"> ← {x.from}</span>}</span></li>))}</ul>}
                {prompt && <div className="small"><span className="muted">{t("rp.instructions")}</span><Clamp lines={4}><Markdown text={prompt} /></Clamp></div>}
              </details>)}
            {s.attempts.length > 1 && (
              <ol className="rp-attempts small">{s.attempts.map((x) => (
                <li key={x.n} className={`st-${x.state}`}><span className="rp-ico" aria-hidden="true">{ICON[x.state]}</span>
                  {t("rp.attempt", { n: String(x.n) })} · {t(`rp.state.${x.state}`)}
                  {x.start && x.end ? ` · ${duration(x.end - x.start)}` : ""}{x.cost ? ` · ${usd(x.cost)}` : ""}
                  {x.error ? <span className="muted"> — {x.error.slice(0, 120)}</span> : null}</li>))}</ol>)}
            {s.events.length === 0 ? <p className="muted small">{t(s.state === "pending" ? "rp.notYet" : "rp.noEvents")}</p> : (
              <ol className="rp-lines" aria-label={t("rp.whatItDid")}>
                {s.events.map((e: Ev) => {
                  if (["agent_done", "result", "outputs", "tool_result", "decision"].includes(e.type)) return null;  // shown above
                  if (e.type === "tool_error" && e.id) return null;  // on its tool's line
                  const l = line(e, i18n);
                  if (!l) return null;
                  const o = e.type === "tool" && typeof e.id === "string" ? outcomeText(outcome[e.id], t) : null;
                  const id = e.type === "tool" && typeof e.id === "string" ? e.id : null;
                  const call = id ? calls[id] : undefined;
                  return <li key={e.i} className={`ev ${l.cls}${o?.bad ? " err" : ""}`}>
                    <span className="rp-t" title={fullTime(e.ts, lang)}>{rel(e.ts)}</span>
                    {e.type === "text" ? <Clamp><Markdown text={l.text} /></Clamp>
                      : <span>{l.text}{o && <span className={o.bad ? "" : "muted"}> · {o.text}</span>}
                        {id && <button className="link small rp-call-btn" aria-expanded={call !== undefined} onClick={() => toggleCall(id)}
                          title={t("rp.showCallTitle")}>{t(call !== undefined ? "rp.hideCall" : "rp.showCall")}</button>}
                        {id && call === null && <div className="muted small">{t("rp.loadingCall")}</div>}
                        {id && typeof call === "string" && <div className="field-err small">{call}</div>}
                        {id && call && typeof call === "object" && (
                          <div className="rp-call">
                            <div className="muted small">{t("rp.callArgs")}</div><pre className="mono small">{call.input || "—"}</pre>
                            <div className="muted small">{t(call.is_error ? "rp.callError" : "rp.callResult", { size: String(call.result_size) })}</div>
                            <pre className="mono small">{call.result || "—"}{call.cut ? "\n…" : ""}</pre>
                            <div className="muted small">{t("rp.callNote")}</div>
                          </div>)}
                      </span>}
                  </li>;
                })}
              </ol>)}
            {s.outputs.length > 0 && (
              <div className="rp-step-outs small"><span className="muted">{t("rp.stepOutputs")}</span>
                {s.outputs.map((n) => <button key={n} className="link small mono" onClick={() => onOutput(s.node, n)}>{n}</button>)}</div>)}
            <div className="rp-step-actions">
              {onCanvas && <button className="small" onClick={() => onCanvas(s.node)} title={t("rp.onCanvas")}>{t("rp.canvas")}</button>}
              {menu.length > 0 && <MoreMenu label={t("rp.stepMenu", { name: s.node })} items={menu} />}
            </div>
          </div>)}
      </li>);
  };

  return (
    <ol className="rp-steps" aria-label={t("rp.steps")}>
      {groups(steps, spec).map((g) => g.kind === "single" ? row(g.steps[0]) : (
        <li key={`g-${g.steps[0].node}`} className={`rp-par ${g.kind}`}>
          <span className="rp-par-label">{g.kind === "parallel" ? `∥ ${t("rp.parallel", { n: String(g.steps.length) })}` : `⑂ ${t("rp.choice", { n: String(g.steps.length) })}`}</span>
          <ol className="rp-steps">{g.steps.map(row)}</ol>
        </li>))}
      {running && <li className="sr-only" aria-live="polite">{steps.filter((s) => s.state === "running").map((s) => s.node).join(", ")}</li>}
    </ol>);
}

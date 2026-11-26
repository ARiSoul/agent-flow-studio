// What a run needs from the person, at the top of the Current view: sign in again, approve a
// write, a step that runs again after a temporary error (now, or cancel), decide at a human gate
// (what you approve, your comment, Approve · Answer · Reject, and "Stop without deciding" apart),
// continue a paused run, update the workspace context. Each card holds its own buttons.
import { useState, type RefObject } from "react";
import { GrowingTextarea } from "./fields";
import { useI18n, type I18n } from "./i18n";
import { Markdown } from "./Markdown";
import { Clamp, fileUrl, mediaOf, oneLine, pretty } from "./RunParts";
import { shortCause } from "./runLines";

import type { Shown } from "./RunResults";
import { duration, type Ev, type Step } from "./runSteps";

/** "writer: next; checker: score > 3" → "after writer, after checker (score > 3)". */
export function readReason(reason: string, t: I18n["t"]): string {
  return reason.split("; ").map((part) => {
    const i = part.indexOf(": ");
    if (i < 0) return part;
    const node = part.slice(0, i), why = part.slice(i + 2);
    return why === "next" || why === "loop" ? t("rp.after", { node }) : t("rp.afterIf", { node, cond: why });
  }).join(", ");
}

export type GateInfo = { gate: string; label: string; reason: string };

export function Attention({ refEl, authErr, pending, notes, setNotes, onAnswer, retrying, onRetry, retryPolicy, turn, onChange, changingFrom, onKeep, gate, waitedSince, now, gateOutputs, typeOf,
  note, setNote, onDecide, onStop, paused, pausedUntil, onContinue, ctxSuggest, onContext, onShow, changed, onChanges, runInput }: {
  refEl: RefObject<HTMLElement>;
  authErr?: Ev; pending: Ev[]; notes: Record<string, string>; setNotes: (f: (n: Record<string, string>) => Record<string, string>) => void;
  onAnswer: (id: string, ok: boolean) => void;
  /** Steps that failed with a temporary error and run again by themselves soon. */
  retrying: Step[]; onRetry: (node: string, action: "now" | "cancel") => void;
  /** The flow's retry setting (to say where it comes from). */
  retryPolicy: { transient: number; wait: number };
  gate: GateInfo | null; waitedSince?: number; now: number;
  /** The outputs of the steps just before the gate: what the person approves. */
  gateOutputs: { node: string; name: string; value: unknown }[];
  typeOf: (agent: string, name: string) => string;
  note: string; setNote: (s: string) => void;
  onDecide: (d: "approve" | "reject" | "answer") => void; onStop: () => void;
  paused: boolean; pausedUntil?: string | null; onContinue: (how: "step" | "full") => void;
  /** Decided at its gate while another run went: it goes on when that run ends. */
  turn?: { after: string; decision: string } | null;
  /** Change the decision before the run goes on. */
  onChange: () => void;
  /** The decision being changed (the run is held meanwhile), and keeping it. */
  changingFrom?: { decision: string; comment: string; key?: string; until?: number } | null; onKeep: () => void;
  ctxSuggest: boolean; onContext: () => void;
  /** How many files the run changed in the workspace (null: no git roots), and opening them. */
  changed?: number | null; onChanges?: () => void;
  /** The run folder (image outputs are shown from it). */
  runInput?: string;
  onShow: (s: Shown) => void;
}) {
  const i18n = useI18n();
  const { t } = i18n;
  const [help, setHelp] = useState(false);
  if (!authErr && !pending.length && !retrying.length && !turn && !gate && !paused && !ctxSuggest) return null;
  return (
    <section className="rp-attention" ref={refEl} tabIndex={-1} aria-label={t("rp.needsYou")}>
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
      {pending.map((p) => {
        const id = p.id as string;
        const title = t("run.writeBy", { agent: String(p.agent), env: String(p.environment) });
        return (
          <div key={id} className="gate-card write" role="group" aria-label={title}>
            <b>{title}</b>
            <p className="small mono">{String(p.summary)}</p>
            {p.detail && typeof p.detail === "object" ? (
              <ul className="rp-kv small">{Object.entries(p.detail as Record<string, unknown>).map(([k, v]) => (
                <li key={k}><code>{k}</code><span className="mono">{oneLine(v, 300)}</span></li>))}</ul>
            ) : <pre className="mono small">{String(p.detail ?? "")}</pre>}
            <GrowingTextarea minRows={2} placeholder={t("run.writeComment")} value={notes[id] ?? ""}
              aria-label={t("run.writeComment")} onChange={(e) => { const v = e.target.value; setNotes((n) => ({ ...n, [id]: v })); }} />
            <div className="rp-card-actions">
              <button className="primary small" onClick={() => onAnswer(id, true)}>{t("run.approveWrite")}</button>
              <button className="danger-text small" onClick={() => onAnswer(id, false)}>{t("run.reject")}</button>
            </div>
          </div>);
      })}
      {retrying.map((s) => s.retry && (
        <div key={s.node} className="gate-card retry" role="group" aria-label={s.node}>
          <b>⟳ {t("rp.retryCard", { node: s.node, t: duration(Math.max(0, s.retry.at - now / 1000)) || "0 s",
            n: String(s.retry.attempt + 1), of: String(s.retry.max + 1) })}</b>
          {s.retry.reason && <p className="small mono" title={s.retry.reason}>{shortCause(s.retry.reason)}</p>}
          <p className="small muted">{t("rp.retrySetting", { n: String(retryPolicy.transient), wait: String(retryPolicy.wait) })}</p>
          <div className="rp-card-actions">
            <button className="primary small" onClick={() => onRetry(s.node, "now")}>{t("rp.retryNow")}</button>
            <button className="small" onClick={() => onRetry(s.node, "cancel")}>{t("rp.retryCancel")}</button>
          </div>
        </div>))}
      {turn && (
        <div className="gate-card step" role="status">
          <b>⧗ {t("rp.turnTitle", { decision: turn.decision })}</b>
          <p className="small muted">{t("rp.turnHelp", { after: turn.after })}</p>
          <div className="rp-card-actions"><button className="small" onClick={onChange}>{t("rp.changeDecision")}</button>
            <span className="grow" /><button className="link small" onClick={onStop}>{t("run.stop")}</button></div>
        </div>)}
      {gate && (
        <div className="gate-card" role="group" aria-label={t("run.humanApproval", { label: gate.label })}>
          <div className="row"><b className="grow">⏸ {t("run.humanApproval", { label: gate.label })}</b>
            {waitedSince && <span className="muted small" title={t("rp.waitingTitle")}>{t("rp.waited", { t: duration(now / 1000 - waitedSince) })}</span>}
            <button className="icon small" aria-expanded={help} aria-label={t("rp.help")} title={t("rp.help")} onClick={() => setHelp(!help)}>ⓘ</button></div>
          {changingFrom && <div className="small rp-recorded">
            <div>{t("rp.recorded", { decision: changingFrom.decision })}
              {changingFrom.comment && <span className="muted"> — “{changingFrom.comment}”</span>}</div>
            <div className="muted">{changingFrom.until
              ? t("rp.heldUntil", { decision: changingFrom.decision, t: duration(Math.max(0, changingFrom.until - now / 1000)) || "0 s" })
              : t("rp.heldWhileChanging")}</div>
            <button className="primary small" onClick={onKeep}>{t("rp.keepDecision")}</button>
          </div>}
          {gate.reason && <p className="small muted">{t("rp.gateReason", { reason: readReason(gate.reason, t) })}</p>}
          {changed !== null && changed !== undefined && (changed > 0
            ? <button className="link small rp-gate-changes" onClick={onChanges}>{i18n.tn("rp.chGate", changed)} ▸</button>
            : <p className="small muted">{t("rp.chGateNone")}</p>)}
          {help && <p className="small muted">{t("rp.gateHelp")}</p>}
          {gateOutputs.map(({ node, name, value }, n) => (
            <div key={`${node}.${name}`} className="rp-gate-out">
              <div className="row small"><span className="mono"><b>{name}</b> · {node}</span><span className="grow" />
                <button className="small" onClick={() => onShow({ title: `${node}.${name}`, value, md: ["markdown", "text"].includes(typeOf(node, name)) })}>{t("run.outOpen")}</button></div>
              {mediaOf(value)?.kind === "image" && runInput
                ? <img className="rp-gate-img checker" src={fileUrl(runInput, mediaOf(value)!.path)} alt={name} />
                : n === 0 && !changingFrom ? <Clamp lines={5}>{typeof value === "string" ? <Markdown text={value} /> : <pre className="mono small">{pretty(value)}</pre>}</Clamp>
                : <div className="small rp-gate-line">{oneLine(value, 140)}</div>}
            </div>))}
          <GrowingTextarea minRows={2} placeholder={t("rp.gateComment")} value={note} aria-label={t("rp.gateComment")}
            onChange={(e) => setNote(e.target.value)}
            onKeyDown={(e) => { if (e.key === "Enter" && (e.ctrlKey || e.metaKey) && note.trim()) { e.preventDefault(); onDecide("answer"); } }} />
          <div className="rp-card-actions rp-sticky">
            {changingFrom && <span className="small muted">{t("rp.changeTo")}</span>}
            {changingFrom?.key !== "approve" && <button className={`${changingFrom ? "" : "primary "}small`} onClick={() => onDecide("approve")}>{t("run.approve")}</button>}
            <button className="small" aria-disabled={!note.trim()} onClick={() => note.trim() && onDecide("answer")}
              title={note.trim() ? t("rp.answerTitle") : t("rp.answerNeedsText")}>{t("rp.answer")}</button>
            {changingFrom?.key !== "reject" && <button className="danger-text small" onClick={() => onDecide("reject")} title={t("rp.rejectTitle")}>{t("rp.rejectEnds")}</button>}
          </div>
          {!note.trim() && <span className="sr-only">{t("rp.answerNeedsText")}</span>}
          <div className="row small rp-gate-foot">
            {changingFrom ? <button className="link small" onClick={onStop} title={t("rp.stopDropsTitle")}>{t("rp.stopDrops")}</button> : <button className="link small" onClick={onStop} title={`${t("rp.gateWaits")} ${t("rp.stopGateTitle")}`}>{t("rp.stopLater")}</button>}
          </div>
        </div>)}
      {paused && (
        <div className="gate-card step">
          <b>{pausedUntil ? t("run.pausedUntil", { target: pausedUntil }) : t("run.pausedAfter")}</b>
          <p className="small muted">{t("run.pausedHelp")}</p>
          <div className="rp-card-actions">
            <button className="primary small" onClick={() => onContinue("step")}>{t("run.nextStep")}</button>
            <button className="small" onClick={() => onContinue("full")}>{t("run.continueToEnd")}</button>
            <span className="grow" />
            <button className="link small" onClick={onStop}>{t("run.stop")}</button>
          </div>
        </div>)}
      {ctxSuggest && (
        <div className="gate-card ctx-card">
          <b>{t("run.ctxTitle")}</b>
          <p className="small muted">{t("run.ctxHelp")}</p>
          <button className="primary small" onClick={onContext}>{t("run.ctxUpdate")}</button>
        </div>)}
    </section>);
}

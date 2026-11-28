// What a run needs from the person, at the top of the Current view: sign in again, approve a
// write, a step that runs again after a temporary error (now, or cancel), decide at a human gate
// (what you approve, your comment, Approve · Send answer · Reject, Stop in the card's ⋯), continue
// a paused run, update the workspace context. Every card has one shell (`Card`): a left border
// whose color says what it is (--human: your decision; --bad: something failed; --accent: paused),
// one header row (icon, title, meta), one line of what is asked, then its buttons.
import { useState, type ReactNode, type RefObject } from "react";
import { GrowingTextarea } from "./fields";
import { useI18n, type I18n } from "./i18n";
import { Markdown } from "./Markdown";
import { fileUrl, mediaOf, MoreMenu, oneLine, pretty } from "./RunParts";
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

/** A temporary API error in plain words (the raw text stays one click away). */
export function plainCause(text: string, t: I18n["t"]): string {
  if (/\b529\b|overloaded/i.test(text)) return t("rp.cause.overloaded");
  if (/\b429\b|rate_limit/i.test(text)) return t("rp.cause.rateLimit");
  const code = text.match(/api error:?\s*\(?(5\d\d)\b/i)?.[1];
  if (code) return t("rp.cause.server", { code });
  if (/connection/i.test(text)) return t("rp.cause.connection");
  return shortCause(text);
}

const DRAFT = /^>\s*\*\*DRAFT[^\n]*\n+/;

/** A write summary ("Create/replace hello.py (29 characters)"): the file in mono, the size muted. */
function summaryParts(summary: string, file: unknown): ReactNode {
  const size = summary.match(/\s*\((\d+ [^)]*)\)\s*$/);
  const head = size ? summary.slice(0, size.index) : summary;
  const f = typeof file === "string" && file && head.includes(file) ? file : null;
  const parts = f ? head.split(f) : [head];
  return <>{parts.map((x, i) => <span key={i}>{x}{f && i < parts.length - 1 && <b className="mono">{f}</b>}</span>)}
    {size && <span className="muted"> · {size[1]}</span>}</>;
}  // artifacts.DRAFT_MARKER, as a quote at the top of a draft

export type GateInfo = { gate: string; label: string; reason: string };
type Tone = "decide" | "failed" | "paused";

function Card({ tone, icon, title, meta, metaTitle, ask, label, menu, alert, children }: {
  tone: Tone; icon: ReactNode; title: ReactNode; meta?: ReactNode; metaTitle?: string; ask?: ReactNode;
  label?: string; menu?: ReactNode; alert?: boolean; children?: ReactNode;
}) {
  return (
    <div className={`rp-card ${tone}`} role={alert ? "alert" : "group"} aria-label={label}>
      <div className="rp-card-head">
        <span className="rp-card-icon" aria-hidden="true">{icon}</span>
        <b className="rp-card-title">{title}</b>
        {meta && <span className="rp-card-meta" title={metaTitle} aria-live="off">{meta}</span>}
        {menu}
      </div>
      {ask && <div className="rp-card-ask">{ask}</div>}
      {children}
    </div>
  );
}

/** A long value, cut at a few lines with a fade (the full value opens in the viewer). */
function Fade({ children }: { children: ReactNode }) {
  return <div className="rp-fade">{children}</div>;
}

export function Attention({ refEl, authErr, pending, notes, setNotes, onAnswer, retrying, onRetry, onRetrySetting, retryPolicy, turn, onChange, changingFrom, onKeep, gate, waitedSince, now, gateOutputs, typeOf,
  note, setNote, onDecide, onStop, paused, pausedUntil, pausedAfter, pausedAgents, onContinue, ctxSuggest, onContext, onShow, changed, onChanges, runInput }: {
  refEl: RefObject<HTMLElement>;
  authErr?: Ev; pending: Ev[]; notes: Record<string, string>; setNotes: (f: (n: Record<string, string>) => Record<string, string>) => void;
  onAnswer: (id: string, ok: boolean) => void;
  /** Steps that failed with a temporary error and run again by themselves soon. */
  retrying: Step[]; onRetry: (node: string, action: "now" | "cancel") => void;
  onRetrySetting?: () => void;
  /** The flow's retry setting (to say where it comes from). */
  retryPolicy: { transient: number; wait: number };
  gate: GateInfo | null; waitedSince?: number; now: number;
  /** The outputs of the steps just before the gate: what the person approves. */
  gateOutputs: { node: string; name: string; value: unknown }[];
  typeOf: (agent: string, name: string) => string;
  note: string; setNote: (s: string) => void;
  onDecide: (d: "approve" | "reject" | "answer") => void; onStop: () => void;
  paused: boolean; pausedUntil?: string | null; onContinue: (how: "step" | "full") => void;
  /** Step-by-step: the step that just finished. */
  pausedAfter?: string | null;
  /** The agents the person paused (they go on in their own sessions). */
  pausedAgents?: string[];
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
  const count = (authErr ? 1 : 0) + pending.length + retrying.length + (turn ? 1 : 0) + (gate ? 1 : 0)
    + (paused ? 1 : 0) + (ctxSuggest ? 1 : 0);
  if (!count) return null;
  const stopRun = <button className="danger-text small" onClick={onStop}>{t("rp.stopRun")}</button>;
  const answerTo = gateOutputs[0]?.node;
  return (
    <section className="rp-attention" ref={refEl} tabIndex={-1} aria-labelledby="rp-needs">
      <h3 id="rp-needs" className="rp-needs">{t("rp.needsYouN", { n: String(count) })}</h3>
      {authErr && (
        <Card tone="failed" icon="!" title={t("run.auth.title")} ask={t("run.auth.why")} alert>
          <ol className="rp-card-ol">
            <li>{t("run.auth.step1")}</li>
            <li>{t("run.auth.step2")} <code>claude</code></li>
            <li>{t("run.auth.step3")} <code>/login</code></li>
            <li>{t("run.auth.step4")}</li>
          </ol>
          <p className="rp-card-note">{t("run.auth.apiKey")} <code>ANTHROPIC_API_KEY</code></p>
          <details className="rp-card-details"><summary>{t("run.auth.detail")}</summary>
            <pre className="mono art-view">{String(authErr.message ?? authErr.text ?? "")}</pre></details>
        </Card>)}

      {pending.map((p) => {
        const id = p.id as string;
        const agent = String(p.agent), env = String(p.environment);
        const detail = p.detail && typeof p.detail === "object" ? p.detail as Record<string, unknown> : null;
        const body = detail ? (detail.content ?? detail.new_string ?? detail.command ?? detail.body) : p.detail;
        const hasBody = body !== undefined && body !== null && body !== "";
        const rest = detail ? Object.entries(detail).filter(([k]) =>
          !["file_path", "content", "new_string", "command", "body"].includes(k)) : [];
        return (
          <Card key={id} tone="decide" icon="✎" label={t("run.writeBy", { agent, env })}
            title={t("rp.writeTitle", { agent })} meta={env} metaTitle={t("rp.envTitle", { env })}
            ask={t("rp.writeAsk", { agent })}>
            <div className="rp-card-section">
              <div className="rp-gate-out-head"><span className="rp-sum" title={String(p.summary)}>{summaryParts(String(p.summary), detail?.file_path)}</span><span className="grow" />
                {hasBody && <button className="small" aria-label={t("rp.openFull")} title={t("rp.openFull")}
                  onClick={() => onShow({ title: String(p.summary), value: body, md: false })}>{t("rp.open")}</button>}</div>
              {rest.length > 0 && <ul className="rp-kv small">{rest.map(([k, v]) => (
                <li key={k}><code>{k}</code><span className="mono">{oneLine(v, 300)}</span></li>))}</ul>}
              {hasBody && <pre className="mono rp-write-body">{typeof body === "string" ? body : pretty(body)}</pre>}
            </div>
            <GrowingTextarea minRows={2} placeholder={t("run.writeComment")} value={notes[id] ?? ""}
              aria-label={t("run.writeComment")} onChange={(e) => { const v = e.target.value; setNotes((n) => ({ ...n, [id]: v })); }} />
            <div className="rp-card-actions">
              <button className="primary small" onClick={() => onAnswer(id, true)}>{t("run.approveWrite")}</button>
              <span className="grow" />
              <button className="danger-text small" onClick={() => onAnswer(id, false)}>{t("run.reject")}</button>
            </div>
          </Card>);
      })}

      {retrying.map((s) => s.retry && (
        <Card key={s.node} tone="failed" icon="⟳" label={t("rp.retryTitle", { node: s.node })} title={t("rp.retryTitle", { node: s.node })}
          meta={t("rp.retryMeta", { t: duration(Math.max(0, s.retry.at - now / 1000)) || "0 s",
            n: String(s.retry.attempt + 1), of: String(s.retry.max + 1) })}
          ask={<>{s.retry.reason ? `${plainCause(s.retry.reason, t)} ` : ""}{t("rp.retryNoAction")}</>}>
          <div className="rp-card-section">
            {s.retry.reason && <details className="rp-card-details"><summary>{t("rp.rawError")}</summary>
              <pre className="mono art-view">{s.retry.reason}</pre></details>}
            <p className="rp-card-note">{t("rp.retrySetting", { n: String(retryPolicy.transient) })}
              {onRetrySetting && <> · <button className="link small rp-inline-link" onClick={onRetrySetting}>{t("rp.retrySettingLink")}</button></>}</p>
          </div>
          <div className="rp-card-actions">
            <button className="small" onClick={() => onRetry(s.node, "now")}>{t("rp.retryNow")}</button>
            <button className="small" title={t("rp.retryCancelTitle")} onClick={() => onRetry(s.node, "cancel")}>{t("rp.retryCancel")}</button>
          </div>
        </Card>))}

      {turn && (
        <Card tone="paused" icon="⧗" title={t("rp.turnTitle", { decision: turn.decision })} ask={t("rp.turnHelp", { after: turn.after })}>
          <div className="rp-card-actions"><button className="small" onClick={onChange}>{t("rp.changeDecision")}</button>
            <span className="grow" />{stopRun}</div>
        </Card>)}

      {gate && (
        <Card tone="decide" icon={<span className="rp-gate-icon">?</span>} label={t("run.humanApproval", { label: gate.label })} title={gate.label}
          meta={[gate.reason ? readReason(gate.reason, t) : "", waitedSince ? duration(now / 1000 - waitedSince) : ""].filter(Boolean).join(" · ") || undefined}
          metaTitle={t("rp.waitingTitle")} ask={t("rp.gateAsk")}
          menu={<>
            <button className="icon small" aria-expanded={help} aria-label={t("rp.help")} title={t("rp.help")} onClick={() => setHelp(!help)}>ⓘ</button>
            <MoreMenu label={t("rp.moreShort")} items={[{ label: changingFrom ? t("rp.stopDrops") : t("rp.stopRunNoDecision"),
              title: changingFrom ? t("rp.stopDropsTitle") : `${t("rp.gateWaits")} ${t("rp.stopGateTitle")}`, danger: true, onSelect: onStop }]} />
          </>}>
          {help && <p className="rp-card-note">{t("rp.gateHelp")}</p>}
          {changingFrom && <div className="small rp-recorded">
            <div>{t("rp.recorded", { decision: changingFrom.decision })}
              {changingFrom.comment && <span className="muted"> — “{changingFrom.comment}”</span>}</div>
            <div className="muted">{changingFrom.until
              ? t("rp.heldUntil", { decision: changingFrom.decision, t: duration(Math.max(0, changingFrom.until - now / 1000)) || "0 s" })
              : t("rp.heldWhileChanging")}</div>
            <button className="primary small" onClick={onKeep}>{t("rp.keepDecision")}</button>
          </div>}
          {changed !== null && changed !== undefined && (changed > 0
            ? <button className="link small rp-gate-changes" onClick={onChanges}>{i18n.tn("rp.chGate", changed)} ▸</button>
            : <p className="rp-card-note">{t("rp.chGateNone")}</p>)}
          {gateOutputs.map(({ node, name, value }, n) => {
            const md = ["markdown", "text"].includes(typeOf(node, name));
            const draft = typeof value === "string" && DRAFT.test(value);
            const text = typeof value === "string" ? value.replace(DRAFT, "") : value;
            return (
              <div key={`${node}.${name}`} className="rp-gate-out">
                <div className="rp-gate-out-head"><span className="mono"><b>{name}</b> · {node}</span>
                  {draft && <span className="rp-tag">{t("rp.draftTag")}</span>}<span className="grow" />
                  <button className="small" onClick={() => onShow({ title: `${node}.${name}`, value, md })}>{t("rp.openOutput", { name })}</button></div>
                {mediaOf(value)?.kind === "image" && runInput
                  ? <img className="rp-gate-img checker" src={fileUrl(runInput, mediaOf(value)!.path)} alt={name} />
                  : n === 0 && !changingFrom ? <Fade>{typeof text === "string" ? <Markdown text={text} /> : <pre className="mono small">{pretty(text)}</pre>}</Fade>
                  : <div className="small rp-gate-line">{oneLine(text, 140)}</div>}
              </div>);
          })}
          <GrowingTextarea minRows={2} value={note} aria-label={t("rp.gateCommentLabel")}
            placeholder={answerTo ? t("rp.gateCommentTo", { node: answerTo }) : t("rp.gateCommentAny")}
            onChange={(e) => setNote(e.target.value)}
            onKeyDown={(e) => { if (e.key === "Enter" && (e.ctrlKey || e.metaKey) && note.trim()) { e.preventDefault(); onDecide("answer"); } }} />
          <div className="rp-card-actions rp-sticky">
            {changingFrom && <span className="small muted">{t("rp.changeTo")}</span>}
            {changingFrom?.key !== "approve" && <button className={`${changingFrom ? "" : "primary "}small`} onClick={() => onDecide("approve")}>{t("run.approve")}</button>}
            <button className="small" aria-disabled={!note.trim()} onClick={() => note.trim() && onDecide("answer")}
              title={note.trim() ? t("rp.answerTitle") : t("rp.answerNeedsText")}>{t("rp.sendAnswer")}</button>
            <span className="grow" />
            {changingFrom?.key !== "reject" && <button className="danger-text small" onClick={() => onDecide("reject")} title={t("rp.rejectTitle")}>{t("rp.reject")}</button>}
          </div>
          <p className="rp-card-note">{t("rp.gateEnds")}</p>
          {!note.trim() && <span className="sr-only">{t("rp.answerNeedsText")}</span>}
        </Card>)}

      {paused && pausedAgents && pausedAgents.length > 0 ? (
        <Card tone="paused" icon="⏸" label={t("rp.pausedBy", { names: pausedAgents.join(", ") })}
          title={t("rp.youPaused", { names: pausedAgents.join(", ") })} ask={t("rp.pausedByHelp")}>
          <div className="rp-card-actions">
            <button className="primary small" onClick={() => onContinue("full")}>{t("rp.goOn")}</button>
            <span className="grow" />{stopRun}
          </div>
        </Card>
      ) : paused && (
        <Card tone="paused" icon="⏸" ask={t("run.pausedHelp")}
          title={pausedUntil ? t("run.pausedUntil", { target: pausedUntil })
            : pausedAfter ? t("rp.pausedAfterStep", { node: pausedAfter }) : t("run.pausedAfter")}>
          <div className="rp-card-actions">
            <button className="primary small" onClick={() => onContinue("step")}>{t("run.nextStep")}</button>
            <button className="small" onClick={() => onContinue("full")}>{t("run.continueToEnd")}</button>
            <span className="grow" />{stopRun}
          </div>
        </Card>)}

      {ctxSuggest && (
        <Card tone="paused" icon="✦" title={t("run.ctxTitle")} ask={t("run.ctxHelp")}>
          <div className="rp-card-actions"><button className="primary small" onClick={onContext}>{t("run.ctxUpdate")}</button></div>
        </Card>)}
    </section>);
}

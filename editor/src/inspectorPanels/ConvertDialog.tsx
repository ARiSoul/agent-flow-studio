/** The window of "Convert to the graph engine": the verdict and what the person gains, then what
 * to check (problems, agents, conditions in words, removed conditions, what is no longer used),
 * before the draft changes. Nothing is saved here. */
import { useEffect, useLayoutEffect, useRef, useState, type ReactNode } from "react";
import { trapFocus } from "../Dialog";
import { useI18n } from "../i18n";
import { localizeServer } from "../i18n/server";
import type { FlowError } from "../types";

export type Note = FlowError & { level: "warn" | "info" };
export interface ConvertPlan { errors: FlowError[]; notes: Note[] }

const FEW = 3;  // rows a long list shows before "Show all"
const p = (n: Note, k: string) => String((n.params as Record<string, unknown> | undefined)?.[k] ?? "");

/** `code` spans of a translated text as <code> (the texts name `if` expressions). */
function withCode(text: string): ReactNode {
  return text.split("`").map((part, i) => (i % 2 ? <code key={i}>{part}</code> : part));
}

function Section({ id, title, count, tone, hint, children }: {
  id: string; title: string; count?: number; tone?: "bad" | "warn"; hint?: ReactNode; children: ReactNode;
}) {
  return (
    <section className={`cv-section${tone ? ` ${tone}` : ""}`} aria-labelledby={id}>
      <h3 id={id}>{title}{count !== undefined && <span className="cv-count">{count}</span>}</h3>
      {hint && <p className="cv-hint">{hint}</p>}
      {children}
    </section>
  );
}

function Link({ src, dst }: { src: string; dst: string }) {
  const { t } = useI18n();
  return <span className="cv-link"><code>{src}</code><span aria-hidden="true">→</span>
    <span className="sr-only">{t("conv.to")}</span><code>{dst}</code></span>;
}

/** Links with a condition text: a compact two-column list, the first rows and "Show all". */
function Conditions({ id, notes, was }: { id: string; notes: Note[]; was?: boolean }) {
  const { t } = useI18n();
  const [all, setAll] = useState(false);
  const opened = useRef(false);  // "Show all" was pressed (not the first render)
  const firstNew = useRef<HTMLSpanElement>(null);
  const shown = all ? notes : notes.slice(0, FEW);
  useEffect(() => {  // keyboard: after "Show all", on the first new row (once React has drawn it)
    if (all && opened.current) firstNew.current?.focus();
  }, [all]);
  const toggle = () => { opened.current = true; setAll(!all); };
  return <>
    <ul className="cv-conds">{shown.map((n, i) => (
      <li key={i}>
        <span className="cv-cell" ref={i === FEW ? firstNew : undefined} tabIndex={i === FEW ? -1 : undefined}
          aria-describedby={i === FEW ? `${id}-text-${i}` : undefined}>
          <Link src={p(n, "src")} dst={p(n, "dst")} /></span>
        <span className="cv-cell cv-text" id={`${id}-text-${i}`}>{was ? t("conv.was", { text: p(n, "when") })
          : t("conv.quoted", { text: p(n, "when") })}</span>
      </li>))}
    </ul>
    {notes.length > FEW && (
      <button className="link small cv-more" aria-expanded={all} onClick={toggle}>
        {all ? t("conv.showFewer") : t("conv.showAll", { n: String(notes.length) })}</button>)}
  </>;
}

export function ConvertDialog({ plan, onCancel, onConvert }: {
  plan: ConvertPlan; onCancel: () => void; onConvert: () => void;
}) {
  const { t, tn, lang } = useI18n();
  const box = useRef<HTMLDivElement>(null);
  const body = useRef<HTMLDivElement>(null);
  const content = useRef<HTMLDivElement>(null);  // grows when a list opens: observed too
  const [more, setMore] = useState(false);  // the body can still scroll down: a shadow on the footer
  useEffect(() => {
    const back = document.activeElement;
    box.current?.querySelector<HTMLElement>("button.primary")?.focus();
    return () => { if (back instanceof HTMLElement) back.focus(); };
  }, []);
  const measure = () => {
    const b = body.current;
    if (b) setMore(b.scrollTop + b.clientHeight < b.scrollHeight - 2);
  };
  useLayoutEffect(measure);
  useEffect(() => {  // the window or the content changes size: the footer shadow follows
    const b = body.current;
    if (!b || typeof ResizeObserver === "undefined") return;
    const ro = new ResizeObserver(() => measure());
    ro.observe(b);
    if (content.current) ro.observe(content.current);
    return () => ro.disconnect();
  }, []);  // eslint-disable-line react-hooks/exhaustive-deps -- measure only reads refs

  const by = (code: string) => plan.notes.filter((n) => n.code === code);
  const noInputs = by("convert.no_inputs"), noOutputs = by("convert.no_outputs");
  const when = by("convert.when"), cleared = by("convert.when_cleared"), orch = by("convert.orch_prompt");
  const known = new Set(["convert.no_inputs", "convert.no_outputs", "convert.when", "convert.when_cleared", "convert.orch_prompt"]);
  const other = plan.notes.filter((n) => !known.has(n.code ?? ""));
  // in flow order (the notes come agent by agent)
  const agents = [...new Set(plan.notes.filter((n) => n.code === "convert.no_inputs" || n.code === "convert.no_outputs")
    .map((n) => p(n, "agent")))];
  const status = plan.errors.length ? "bad" : agents.length ? "warn" : "ok";
  const verdict = status === "bad" ? tn("conv.statusBad", plan.errors.length)
    : status === "warn" ? tn("conv.statusWarn", agents.length) : t("conv.statusOk");
  const gains = ["conv.gain.resume", "conv.gain.from", "conv.gain.retry", "conv.gain.pause", "conv.gain.provider", "conv.gain.nodes"] as const;

  return (
    <div className="modal dialog-layer" onMouseDown={(e) => e.target === e.currentTarget && onCancel()}>
      <div ref={box} className="modal-box convert" role="dialog" aria-modal="true" aria-labelledby="cv-title"
        aria-describedby="cv-verdict"
        onKeyDown={(e) => {
          e.stopPropagation();
          if (e.key === "Escape") { e.preventDefault(); onCancel(); }
          if (e.key === "Tab") trapFocus(e, box.current);
        }}>
        <header className="cv-head">
          <div className="cv-title-row">
            <h2 id="cv-title">{t("insp.convertTitle")}</h2>
            <button className="icon-btn cv-close" aria-label={t("conv.close")} title={t("conv.close")} onClick={onCancel}>×</button>
          </div>
          <p className="cv-sub">{t("conv.subtitle")}</p>
          <p id="cv-verdict" className={`cv-verdict ${status}`}>{verdict}</p>
          <div className="cv-gains">
            <span id="cv-gains-label">{t("conv.gains")}</span>
            <ul aria-labelledby="cv-gains-label">{gains.map((g) => <li key={g}>{t(g)}</li>)}</ul>
          </div>
        </header>

        <div ref={body} className="cv-body" onScroll={measure}>
          <div ref={content} className="cv-content">
          {plan.errors.length > 0 && (
            <Section id="cv-errors" title={t("conv.errors")} count={plan.errors.length} tone="bad" hint={t("conv.errorsHint")}>
              <ul className="cv-list">{plan.errors.map((x, i) => (
                <li key={i}>{withCode(localizeServer(x, x.msg, lang))}</li>))}</ul>
            </Section>)}

          {agents.length > 0 && (
            <Section id="cv-agents" title={t("conv.agents")} count={agents.length} tone="warn" hint={t("conv.agentsHint")}>
              <ul className="cv-agents">{agents.map((a) => {
                const ni = noInputs.find((n) => p(n, "agent") === a), no = noOutputs.find((n) => p(n, "agent") === a);
                return (
                  <li key={a}>
                    <code className="cv-name">{a}</code>
                    {ni && <p><b>{t("conv.noInputs")}</b> {t("conv.noInputsHint")}</p>}
                    {no && <p><b>{t("conv.noOutputs", { next: p(no, "next") })}</b> {t("conv.noOutputsHint")}</p>}
                  </li>);
              })}</ul>
            </Section>)}

          {when.length > 0 && (
            <Section id="cv-when" title={t("conv.when")} count={when.length} hint={withCode(t("conv.whenHint"))}>
              <Conditions id="cv-when" notes={when} />
            </Section>)}

          {cleared.length > 0 && (
            <Section id="cv-cleared" title={t("conv.cleared")} count={cleared.length} hint={t("conv.clearedHint")}>
              <Conditions id="cv-cleared" notes={cleared} was />
            </Section>)}

          {(orch.length > 0 || other.length > 0) && (
            <Section id="cv-unused" title={t("conv.unused")} tone={orch.length ? "warn" : undefined}>
              <ul className="cv-list">
                {orch.length > 0 && <li>{t("conv.orchPrompt")}</li>}
                {other.map((n, i) => <li key={i}>{withCode(localizeServer(n, n.msg, lang))}</li>)}
              </ul>
            </Section>)}
          </div>
        </div>

        <footer className={`cv-foot${more ? " more" : ""}`}>
          <span>{t("conv.footer")}</span>
          <button className="dialog-cancel" onClick={onCancel}>{t("conv.cancel")}</button>
          <button className="primary" onClick={onConvert}>{t("conv.ok")}</button>
        </footer>
      </div>
    </div>
  );
}

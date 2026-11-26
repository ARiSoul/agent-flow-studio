// The log of a run: one line per event (time since the start, the step it belongs to, the text),
// with filters by kind and by step, search, copy and download, in time order or grouped by step.
// Tool calls show their outcome on their own line; the links a step follows share one line.
import { useMemo, useRef, useState } from "react";
import { useI18n } from "./i18n";
import { foldLinks, line, outcomeText, retriedFailures, type LogKind } from "./runLines";
import { download, fullTime, MoreMenu, useCopy } from "./RunParts";
import { duration, nodeOf, toolOutcome, type Ev } from "./runSteps";

export { line, outcomeText, type LogKind } from "./runLines";

const stored = (k: string, d: boolean) => { try { const v = localStorage.getItem(k); return v === null ? d : v === "1"; } catch { return d; } };
const keep = (k: string, v: boolean) => { try { localStorage.setItem(k, v ? "1" : "0"); } catch { /* no storage */ } };

export function LogView({ events, t0, steps, fileName, onStep }: {
  events: Ev[]; t0?: number; steps: string[]; fileName: string; onStep: (node: string) => void;
}) {
  const i18n = useI18n();
  const { t, lang } = i18n;
  const [filter, setFilter] = useState<LogKind>("all");
  const [step, setStep] = useState("");
  const [query, setQuery] = useState("");
  const [clockTime, setClockTime] = useState(false);
  const [byStep, setByStep] = useState(() => stored("afs.log.byStep", false));
  const [showText, setShowText] = useState(() => stored("afs.log.text", true));  // the agents' own words
  const [opened, setOpened] = useState<Set<number>>(new Set());  // text lines shown whole
  const [copied, copy] = useCopy();
  const list = useRef<HTMLOListElement>(null);
  const nextErr = useRef(0);
  const outcome = useMemo(() => toolOutcome(events), [events]);
  const retried = useMemo(() => retriedFailures(events), [events]);
  const lines = useMemo(() => foldLinks(events.map((e) => {
    const raw = line(e, i18n);
    // a failure that ran again by itself is a warning, not an error
    const l = raw && retried.has(e.i) ? { ...raw, kind: "flow" as LogKind, cls: e.type === "result" ? "info" : "gate",
      text: raw.text.replace(/^✗/, "↻") } : raw;
    // a tool call that failed counts as an error (its error is on its tool's line)
    const bad = l && e.type === "tool" && typeof e.id === "string" && outcome[e.id]?.type === "tool_error";
    return { e, l: bad ? { ...l, kind: "errors" as LogKind } : l, node: nodeOf(e) };
  }).filter((x): x is { e: Ev; l: NonNullable<ReturnType<typeof line>>; node: string | undefined } => !!x.l)), [events, i18n, outcome, retried]);
  const filtered = lines.filter(({ l, node }) => (filter === "all" || l.kind === filter) && (!step || node === step)
    && (showText || l.kind !== "text" || filter === "text")
    && (!query.trim() || `${node ?? ""} ${l.text}`.toLowerCase().includes(query.trim().toLowerCase())));
  // grouped by step: the run's own lines first, then each step in the flow's order (time order inside)
  const shown = byStep ? [...filtered].sort((a, b) => (a.node ? steps.indexOf(a.node) + 1 : 0) - (b.node ? steps.indexOf(b.node) + 1 : 0)) : filtered;
  const count = (k: LogKind) => lines.filter((x) => x.l.kind === k).length;
  const errors = count("errors");
  const when = (ts?: number) => !ts ? "" : clockTime
    ? new Date(ts * 1000).toLocaleTimeString(lang, { hour: "2-digit", minute: "2-digit", second: "2-digit" })
    : t0 ? `+${duration(ts - t0) || "0.0 s"}` : "";
  const asText = () => lines.map(({ e, l, node }) => `${when(e.ts)}\t${node ?? "-"}\t${l.text}`).join("\n");
  const toNextError = () => {
    const els = list.current?.querySelectorAll<HTMLElement>("li.err") ?? [];
    if (!els.length) return;
    const el = els[nextErr.current % els.length];
    nextErr.current += 1;
    el.scrollIntoView({ block: "center", behavior: "smooth" });
    el.classList.add("flash");
    setTimeout(() => el.classList.remove("flash"), 1200);
  };
  let lastNode: string | undefined = "\u0000";
  return (
    <div className="rp-log">
      <div className="rp-log-tools">
        <select value={filter} onChange={(e) => setFilter(e.target.value as LogKind)} aria-label={t("rp.filter")}>
          {(["all", "errors", "text", "tools", "flow"] as LogKind[]).map((f) => (
            <option key={f} value={f}>{t(`rp.filter.${f}`)} ({f === "all" ? lines.length : count(f)})</option>))}
        </select>
        <select value={step} onChange={(e) => setStep(e.target.value)} aria-label={t("rp.filterStep")}>
          <option value="">{t("rp.allSteps")}</option>
          {steps.map((s) => <option key={s} value={s}>{s}</option>)}
        </select>
        <MoreMenu label={t("rp.logMenu")} items={[
          { label: t(copied === "log" ? "run.outCopied" : "rp.copyLog"), onSelect: () => copy(asText(), "log") },
          { label: t("rp.downloadLogTitle"), onSelect: () => download(`${fileName}-events.jsonl`, events.map((e) => JSON.stringify(e)).join("\n"), "application/x-ndjson") },
          { label: t(clockTime ? "rp.timeRelative" : "rp.timeClock"), onSelect: () => setClockTime(!clockTime) },
        ]} />
        <input type="search" className="rp-log-search" placeholder={t("rp.search")} aria-label={t("rp.search")} value={query} onChange={(e) => setQuery(e.target.value)} />
      </div>
      <div className="rp-log-opts small">
        <label className="rp-toggle"><input type="checkbox" checked={showText} onChange={(e) => { setShowText(e.target.checked); keep("afs.log.text", e.target.checked); }} /> {t("rp.agentText")}</label>
        <label className="rp-toggle"><input type="checkbox" checked={byStep} onChange={(e) => { setByStep(e.target.checked); keep("afs.log.byStep", e.target.checked); }} /> {t("rp.byStep")}</label>
      </div>
      <div className="rp-log-sum small muted">
        <span>{t("rp.linesShown", { n: String(shown.length), of: String(lines.length) })}</span>
        {errors > 0 && <button className="link small rp-err-jump" onClick={toNextError}>✗ {i18n.tn("rp.nextError", errors)}</button>}
      </div>
      <ol className="rp-lines" role="log" aria-label={t("rp.log")} ref={list}>
        {shown.map(({ e, l, node }) => {
          const o = e.type === "tool" && typeof e.id === "string" ? outcomeText(outcome[e.id], t) : null;
          const head = node !== lastNode && node ? node : null;  // the step's name only when it changes
          lastNode = node;
          return (
            <li key={e.i} className={`ev ${l.cls}${o?.bad ? " err" : ""}${byStep && head ? " rp-group" : ""}${e.type === "result" || e.type === "edge" ? " rp-row1" : ""}`}
              title={e.type === "result" || e.type === "edge" ? l.text : undefined}>
              <span className="rp-t" title={fullTime(e.ts, lang)}>{when(e.ts)}</span>
              <span className="rp-txt">
                {head && <button className="rp-lnode" title={t("rp.showStep")} onClick={() => onStep(head)}>{head}</button>}
                {e.type === "text"
                  ? <span className={`rp-said${opened.has(e.i) ? " open" : ""}`} title={opened.has(e.i) ? undefined : t("rp.clickWhole")}
                    onClick={() => setOpened((s) => { const n = new Set(s); if (n.has(e.i)) n.delete(e.i); else n.add(e.i); return n; })}>{l.text}</span>
                  : <span>{l.text}{o && <span className={o.bad ? "" : "muted"}> · {o.text}</span>}</span>}
              </span>
            </li>);
        })}
        {shown.length === 0 && <li className="muted small">{t("rp.noLines")}</li>}
      </ol>
    </div>);
}

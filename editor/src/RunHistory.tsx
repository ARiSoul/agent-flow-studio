// The saved runs of a flow: totals of a period (7 days, 30 days, all) with the period before,
// search and a state filter (on the server), a page at a time, grouped by day; each row opens its
// run, and its menu re-runs it, edits and runs it, runs it from a step, compares it with another run
// (Compare), renames or deletes it (to the trash, where it can be restored).
import { useCallback, useEffect, useState } from "react";
import { api } from "./api";
import { useDialog } from "./Dialog";
import { useI18n } from "./i18n";
import { fullTime, MoreMenu, oneLine, RUN_ICON, statusText, when } from "./RunParts";
import { count, duration, parseLines, shortModel, stepsOf, usd, wordDiff, type Step } from "./runSteps";
import type { FlowRaw, RunIO } from "./types";

export type PastRun = { input: string; run: string; started: number; ended?: number | null; status: string; gate?: string | null;
  failed?: string | null; message?: string | null; cost: number; mode?: string; target?: string | null; execution?: string;
  title?: string; open?: boolean; attempts?: number;
  /** Seconds it waited for the person at human gates (part of its time). */
  waited?: number;
  /** It started from the queue (when it was queued). */
  from_queue?: number;
  /** A decided run: the decision that waits for its turn. */
  decision?: string | null };
type Trashed = { name: string; input: string; title?: string; started?: number; deleted: number };
type Totals = { runs: number; failed: number; lost?: number; median: number | null; cost: number };
type Stats = Totals & { days?: number; prev?: Totals };
const WINDOWS = [7, 30, 0];  // days (0: all)

const MUTATE = { "Content-Type": "application/json", "X-Flow-Editor": "1" };
const PAGE = 30;
const STATES = ["running", "gate", "paused", "decided", "error", "stopped", "lost", "done"];

async function send(url: string, method: string, body?: unknown) {
  const r = await fetch(url, { method, headers: MUTATE, body: body === undefined ? undefined : JSON.stringify(body) });
  const d = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(typeof d.detail === "string" ? d.detail : r.statusText);
  return d;
}

export function HistoryView({ flow, spec, current, refresh, graph, onOpen, onOpenStep, onRerun, onEdit, onFromStep, onChanged }: {
  flow: string; spec: FlowRaw; current?: string; refresh: unknown; graph: boolean;
  onOpen: (input: string) => void; onOpenStep: (input: string, node: string) => void;
  onRerun: (p: PastRun) => void; onEdit: (input: string) => void;
  onFromStep: (input: string) => void;
  /** A run was renamed, deleted or restored (`gone`: the folder no longer exists). */
  onChanged: (input: string, gone: boolean) => void;
}) {
  const i18n = useI18n();
  const { t, lang } = i18n;
  const dialog = useDialog();
  const [items, setItems] = useState<PastRun[]>([]);
  const [total, setTotal] = useState(0);
  const [stats, setStats] = useState<Stats | null>(null);
  const [query, setQuery] = useState("");
  const [status, setStatus] = useState("");
  const [why, setWhy] = useState<Set<string>>(new Set());
  const [err, setErr] = useState<string | null>(null);
  const [trash, setTrash] = useState<Trashed[] | null>(null);
  const [compare, setCompare] = useState<PastRun | null>(null);
  const [days, setDays] = useState(() => { try { return Number(localStorage.getItem("afs.run.days") ?? 30); } catch { return 30; } });
  const pickDays = (d: number) => { setDays(d); try { localStorage.setItem("afs.run.days", String(d)); } catch { /* no storage */ } };

  const url = (n: number, offset: number) =>
    `/api/flows/${encodeURIComponent(flow)}/runs?limit=${n}&offset=${offset}&q=${encodeURIComponent(query.trim())}&status=${status}&days=${days}`;
  const load = useCallback((n = PAGE) => {
    fetch(url(n, 0)).then((r) => (r.ok ? r.json() : { runs: [], total: 0 }))
      .then((d) => { setItems(d.runs ?? []); setTotal(d.total ?? 0); setStats(d.stats ?? null); }).catch(() => undefined);
  }, [flow, query, status, days]);  // eslint-disable-line react-hooks/exhaustive-deps -- url uses these
  useEffect(() => {  // the server searches (after a short pause while typing)
    const h = setTimeout(() => load(Math.max(PAGE, items.length)), query ? 250 : 0);
    return () => clearTimeout(h);
  }, [load, refresh]);  // eslint-disable-line react-hooks/exhaustive-deps -- keep what is loaded
  const more = () => fetch(url(PAGE, items.length)).then((r) => r.json())
    .then((d) => { setItems((x) => [...x, ...(d.runs ?? [])]); setTotal(d.total ?? 0); }).catch(() => undefined);
  const loadTrash = () => fetch(`/api/runs-trash?flow=${encodeURIComponent(flow)}`).then((r) => r.json())
    .then((d) => setTrash(d.runs ?? [])).catch(() => setTrash([]));

  const rename = async (p: PastRun) => {
    const r = await dialog.form({ title: t("run.renameTitle"), message: t("run.renameHelp", { id: p.input }), ok: t("cmd.saveOk"),
      fields: [{ key: "title", label: t("run.renameLabel"), value: p.title ?? "" }] });
    if (!r) return;
    try { await send(`/api/runs/${encodeURIComponent(p.input)}/title`, "PUT", { title: r.title }); load(items.length); onChanged(p.input, false); }
    catch (e) { setErr(String((e as Error).message)); }
  };
  const remove = async (p: PastRun) => {
    if (!(await dialog.confirm({ title: t("rp.deleteTitle", { name: p.title || p.input }), message: t("rp.deleteMessage", { id: p.input }),
      ok: t("app.delete"), danger: true }))) return;
    try { await send(`/api/runs/${encodeURIComponent(p.input)}`, "DELETE"); load(items.length); onChanged(p.input, true); if (trash) loadTrash(); }
    catch (e) { setErr(String((e as Error).message)); }
  };
  const restore = async (x: Trashed) => {
    try { const d = await send(`/api/runs-trash/${encodeURIComponent(x.name)}/restore`, "POST"); loadTrash(); load(items.length); onChanged(d.input, false); }
    catch (e) { setErr(String((e as Error).message)); }
  };
  const empty = async () => {
    if (!(await dialog.confirm({ title: t("rp.emptyTrashTitle"), message: i18n.tn("rp.emptyTrashMessage", trash?.length ?? 0),
      ok: t("rp.emptyTrash"), danger: true }))) return;
    try {
      const d = await send(`/api/runs-trash?flow=${encodeURIComponent(flow)}`, "DELETE");
      if (d.failed?.length) setErr(t("rp.emptyFailed", { names: d.failed.join(", ") }));
      loadTrash();
    } catch (e) { setErr(String((e as Error).message)); }
  };
  const pct = (x?: Totals) => (x && x.runs ? Math.round((x.failed / x.runs) * 100) : 0);

  const day = (ts: number) => {
    const d = new Date(ts * 1000), today = new Date();
    const y = new Date(); y.setDate(today.getDate() - 1);
    return d.toDateString() === today.toDateString() ? t("rp.today") : d.toDateString() === y.toDateString() ? t("rp.yesterday")
      : d.toLocaleDateString(lang, { weekday: "short", day: "2-digit", month: "short", year: "numeric" });
  };
  let lastDay = "";
  return (
    <>
      {stats && (
        <div className="rp-stats small" aria-label={t("rp.statsTitle")}>
          <select className="rp-stats-win" value={days} onChange={(e) => pickDays(Number(e.target.value))} aria-label={t("rp.statWindow")}>
            {WINDOWS.map((d) => <option key={d} value={d}>{t(`rp.win.${d}` as "rp.win.7")}</option>)}</select>
          <span><b>{stats.runs}</b> {t("rp.statRuns")}</span>
          {stats.runs > 0 && <>
            <span className={stats.failed ? "st-error rp-state" : ""}><b>{pct(stats)}%</b> {t("rp.statFailed")}
              {stats.prev && stats.prev.runs > 0 && <span className="muted" title={t("rp.trendTitle")}> {t("rp.before", { v: `${pct(stats.prev)}%` })}</span>}</span>
            {!!stats.lost && <span className="st-lost rp-state"><b>{stats.lost}</b> {t("rp.statLost")}</span>}
            {stats.median !== null && <span title={t("rp.statMedianTitle")}>{t("rp.statMedian")} <b>{duration(stats.median)}</b></span>}
            <span><b>{usd(stats.cost)}</b> {t("rp.statSpent")} · {t("rp.perRun", { cost: usd(stats.cost / stats.runs) })}
              {stats.prev && stats.prev.runs > 0 && <span className="muted" title={t("rp.trendTitle")}> {t("rp.before", { v: usd(stats.prev.cost / stats.prev.runs) })}</span>}</span>
          </>}
        </div>)}
      <div className="row">
        <input type="search" className="grow" placeholder={t("rp.searchRuns")} aria-label={t("rp.searchRuns")} value={query} onChange={(e) => setQuery(e.target.value)} />
        <select value={status} onChange={(e) => setStatus(e.target.value)} aria-label={t("rp.filterStatus")}>
          <option value="">{t("rp.allStatus")}</option>
          {STATES.map((s) => <option key={s} value={s}>{statusText(i18n, s)}</option>)}
        </select>
      </div>
      {err && <p className="field-err" role="alert">{err}</p>}
      {total === 0 && <p className="muted small">{query || status ? t("rp.noMatch") : t("rp.noHistory")}</p>}
      <ul className="rp-history">
        {items.map((p) => {
          const d = day(p.started);
          const head = d !== lastDay ? (lastDay = d) : null;
          const reason = p.status === "error" ? `${p.failed ? `${p.failed}: ` : ""}${(p.message ?? "").replace(/^agent [^:]+: /, "")}`
            : p.status === "gate" && p.gate ? p.gate : p.status === "lost" ? t("rp.lostWhy")
            : p.status === "decided" ? t("rp.decidedRow", { decision: i18n.tOr(`run.decision.${p.decision ?? ""}`, p.decision ?? "") }) : "";
          const open = why.has(p.input);
          return [
            head && <li key={`d-${p.input}`} className="rp-day" aria-hidden="true">{head}</li>,
            <li key={p.input} className={current === p.input ? "on" : undefined}>
              <button className="rp-h-main" onClick={() => onOpen(p.input)} title={t("run.pastOpenTitle")}>
                <span className={`rp-h-ico st-${p.status}`} aria-hidden="true">{RUN_ICON[p.status] ?? "•"}</span>
                <span className="rp-h-text">
                  <span className="rp-h-title">{p.title || p.input}</span>
                  <span className="rp-h-sub">
                    <span className={`rp-state st-${p.status}`}>{statusText(i18n, p.status)}</span>
                    {p.title && <span className="mono">{p.input}</span>}
                    <span>{when(p.started, lang)}</span>
                    {p.ended && p.started ? <span>⏱ {duration(p.ended - p.started)}{(p.waited ?? 0) >= 1
                      ? <span className="muted"> ({t("rp.waitedYou", { t: duration(p.waited) })})</span> : null}</span> : null}
                    {p.cost > 0 && <span>{usd(p.cost)}</span>}
                    {p.mode && p.mode !== "full" && <span>{i18n.tOr(`run.mode.${p.mode}`, p.mode)}{p.target ? ` ${p.target}` : ""}</span>}
                    {p.from_queue ? <span title={t("rp.fromQueueTitle", { t: when(p.from_queue, lang) })}>⧗ {t("rp.fromQueue")}</span> : null}
                    {(p.attempts ?? 1) > 1 && <span title={t("rp.attemptsRunTitle")}>{i18n.tn("rp.attempts", p.attempts ?? 1)}</span>}
                  </span>
                </span>
              </button>
              <MoreMenu label={t("rp.runActions", { name: p.title || p.input })} items={[
                { label: t("rp.open"), onSelect: () => onOpen(p.input) },
                { label: t("rp.rerun"), title: t("rp.rerunTitle"), disabled: p.status === "running", onSelect: () => onRerun(p) },
                { label: t("rp.editRun"), title: t("rp.editRunTitle"), onSelect: () => onEdit(p.input) },
                ...(graph ? [{ label: t("rp.runFromStepDots"), title: t("rp.fromStepTitle"), disabled: p.status === "running", onSelect: () => onFromStep(p.input) }] : []),
                { label: t("rp.compareDots"), onSelect: () => setCompare(p) },
                { kind: "sep" as const },
                { label: t("rp.renameDots"), onSelect: () => rename(p) },
                { label: t("app.delete"), danger: true, disabled: p.status === "running", onSelect: () => remove(p) },
              ]} />
              {reason && (
                <button className={`rp-h-why${p.status === "gate" ? " wait" : p.status === "lost" ? " lost" : ""}${open ? " open" : ""}`} aria-expanded={open}
                  title={reason}
                  onClick={() => setWhy((s) => { const n = new Set(s); if (n.has(p.input)) n.delete(p.input); else n.add(p.input); return n; })}>
                  {p.status === "gate" ? "⏸ " : p.status === "lost" ? "? " : "✗ "}{reason}</button>)}
            </li>];
        })}
      </ul>
      {items.length < total && <button className="small" onClick={more}>{t("rp.loadMore", { n: String(total - items.length) })}</button>}
      <details className="rp-trash" onToggle={(e) => { if ((e.target as HTMLDetailsElement).open) loadTrash(); }}>
        <summary className="small">{t("rp.trash")}{trash ? ` (${trash.length})` : ""}</summary>
        {trash && trash.length === 0 && <p className="muted small">{t("rp.trashEmpty")}</p>}
        <ul className="rp-files">{(trash ?? []).map((x) => (
          <li key={x.name}><span className="small grow" title={x.input}>{x.title || x.input}</span>
            <span className="muted small" title={t("rp.deletedAt")}>{when(x.deleted, lang)}</span>
            <button className="small" onClick={() => restore(x)}>{t("rp.restore")}</button></li>))}</ul>
        {trash && trash.length > 0 && <button className="small danger" onClick={empty}>{t("rp.emptyTrash")}</button>}
      </details>
      {compare && <Compare flow={flow} spec={spec} base={compare} onClose={() => setCompare(null)}
        onOpenStep={(inp, node) => { setCompare(null); onOpenStep(inp, node); }} />}
    </>);
}

/** Two runs side by side: the other run is picked in the window (by default the finished run just
 * before this one; a search finds any). State, start, time and cost are information (time and cost
 * with the difference); then each step (state, time, cost, tokens, model: a click opens the step in
 * that run), then every input and output. Rows that differ are marked (≠); long texts open on click
 * and show a word diff. */
export function Compare({ flow, spec, base, onClose, onOpenStep }: {
  flow: string; spec: FlowRaw; base: PastRun; onClose: () => void; onOpenStep: (input: string, node: string) => void;
}) {
  const i18n = useI18n();
  const { t, lang } = i18n;
  const [all, setAll] = useState<PastRun[] | null>(null);
  const [other, setOther] = useState("");
  const [find, setFind] = useState("");
  const [io, setIo] = useState<[RunIO | null, RunIO | null]>([null, null]);
  const [steps, setSteps] = useState<[Step[], Step[]]>([[], []]);
  const [only, setOnly] = useState(false);
  useEffect(() => {
    fetch(`/api/flows/${encodeURIComponent(flow)}/runs?limit=200`).then((r) => r.json()).then((d) => {
      const runs: PastRun[] = (d.runs ?? []).filter((x: PastRun) => x.input !== base.input);
      setAll(runs);
      const before = runs.filter((x) => x.started < base.started);
      const pick = before.find((x) => x.status === "done") ?? runs.find((x) => x.status === "done") ?? before[0] ?? runs[0];
      setOther(pick?.input ?? "");
    }).catch(() => setAll([]));
  }, [flow, base]);
  const b = all?.find((x) => x.input === other);
  const a = base;
  useEffect(() => {
    if (!b) return;
    const runs = [a, b];
    Promise.all(runs.map((r) => api.io(r.input, flow).catch(() => null))).then((x) => setIo([x[0], x[1]]));
    Promise.all(runs.map((r) => fetch(`/api/artifacts/${encodeURIComponent(r.input)}/events.jsonl`)
      .then((x) => (x.ok ? x.json() : { text: "" })).then((d: { text: string }) => stepsOf(spec, parseLines(`${d.text}\n`).events))
      .catch(() => [] as Step[]))).then((x) => setSteps([x[0], x[1]]));
  }, [flow, spec, a, b]);
  useEffect(() => {
    const k = (e: KeyboardEvent) => e.key === "Escape" && onClose();
    window.addEventListener("keydown", k);
    return () => window.removeEventListener("keydown", k);
  }, [onClose]);
  const took = (r: PastRun) => (r.ended ? r.ended - r.started : undefined);
  const delta = (x: number | undefined, y: number | undefined, f: (n: number) => string) => {
    if (x === undefined || y === undefined) return "";
    const d = f(Math.abs(y - x));
    return /^\$?0(\.0+)?( s)?$/.test(d) ? "" : ` (${y > x ? "+" : "−"}${d})`;  // no "(−0.0 s)"
  };
  const stepText = (x?: Step) => !x ? "—" : [t(`rp.state.${x.state}`), x.start && x.end ? duration(x.end - x.start) : "",
    x.cost ? usd(x.cost) : "", x.tokens.in || x.tokens.out ? t("rp.tokens", { in: count(x.tokens.in), out: count(x.tokens.out) }) : "",
    x.model ? shortModel(x.model) : "", x.attempts.length > 1 ? `×${x.attempts.length}` : "",
    x.decision ? i18n.tOr(`run.decision.${x.decision.decision}`, x.decision.decision) : ""].filter(Boolean).join(" · ");
  const values: [string, unknown, unknown][] = [];
  const keys = (x: Record<string, unknown> | undefined, y: Record<string, unknown> | undefined) => [...new Set([...Object.keys(x ?? {}), ...Object.keys(y ?? {})])];
  for (const k of keys(io[0]?.inputs, io[1]?.inputs)) values.push([`input.${k}`, io[0]?.inputs[k], io[1]?.inputs[k]]);
  for (const ag of keys(io[0]?.outputs, io[1]?.outputs)) {
    for (const k of keys(io[0]?.outputs[ag], io[1]?.outputs[ag])) values.push([`${ag}.${k}`, io[0]?.outputs[ag]?.[k], io[1]?.outputs[ag]?.[k]]);
  }
  const differs = (x: unknown, y: unknown) => JSON.stringify(x) !== JSON.stringify(y);
  const cell = (v: unknown, o: unknown, side: 0 | 1) => {
    if (v === undefined) return <span className="muted">—</span>;
    const text = typeof v === "string" ? v : JSON.stringify(v, null, 2);
    if (text.length <= 80) return <span>{text}</span>;
    const diff = side === 1 && typeof v === "string" && typeof o === "string" && v !== o ? wordDiff(o, v) : null;
    return <details><summary>{oneLine(v, 80)}</summary>
      <div className="rp-cmp-full">{diff ? diff.map((d, n) => <span key={n} className={`wd-${d.op}`}>{d.text}</span>) : text}</div></details>;
  };
  const row = (k: string, x: unknown, y: unknown, mark: boolean) => (
    <tr key={k} className={mark ? "diff" : undefined}>
      <th className="mono">{mark && <span className="rp-ne" aria-label={t("rp.cmpDiffers")}>≠ </span>}{k}</th>
      <td>{cell(x, y, 0)}</td><td>{cell(y, x, 1)}</td></tr>);
  const stepCell = (r: PastRun, x: Step | undefined) => x
    ? <button className="rp-cmp-step" title={t("rp.openStepIn", { run: r.title || r.input })} onClick={() => onOpenStep(r.input, x.node)}>{stepText(x)}</button>
    : <span className="muted">—</span>;
  const shownRuns = (all ?? []).filter((x) => !find.trim() || `${x.title ?? ""} ${x.input}`.toLowerCase().includes(find.trim().toLowerCase()));
  return (
    <div className="modal" onMouseDown={(e) => e.target === e.currentTarget && onClose()}>
      <div className="modal-box out-viewer" role="dialog" aria-label={t("rp.compare")}>
        <div className="panel-head"><h2>{t("rp.compare")}</h2><span className="grow" />
          <label className="small row"><input type="checkbox" checked={only} onChange={(e) => setOnly(e.target.checked)} /> {t("rp.cmpOnly")}</label>
          <button className="small" onClick={onClose}>{t("conn.close")}</button></div>
        <div className="row small rp-cmp-pick">
          <span className="muted">{t("rp.cmpWith", { name: a.title || a.input })}</span>
          <input type="search" placeholder={t("rp.searchRuns")} aria-label={t("rp.searchRuns")} value={find} onChange={(e) => setFind(e.target.value)} />
          <select value={other} onChange={(e) => setOther(e.target.value)} aria-label={t("rp.compareWith")}>
            {shownRuns.map((x) => <option key={x.input} value={x.input}>{`${x.title || x.input} · ${statusText(i18n, x.status)} · ${when(x.started, lang)}`}</option>)}
          </select>
        </div>
        <div className="out-viewer-body">
          {all && !all.length && <p className="muted">{t("rp.compareNone")}</p>}
          {b && <table className="rp-cmp">
            <thead><tr><th /><th>{a.title || a.input} <span className="muted small">({t("rp.cmpThis")})</span></th><th>{b.title || b.input}</th></tr></thead>
            <tbody>
              <tr className="info"><th>{t("rp.cmpState")}</th><td>{statusText(i18n, a.status)}</td><td>{statusText(i18n, b.status)}</td></tr>
              <tr className="info"><th>{t("rp.cmpStarted")}</th><td>{fullTime(a.started, lang)}</td><td>{fullTime(b.started, lang)}</td></tr>
              <tr className="info"><th>{t("rp.cmpTook")}</th><td>{took(a) !== undefined ? duration(took(a)) : "—"}</td>
                <td>{took(b) !== undefined ? duration(took(b)) : "—"}<span className="muted">{delta(took(a), took(b), duration)}</span></td></tr>
              <tr className="info"><th>{t("rp.cmpCost")}</th><td>{usd(a.cost)}</td><td>{usd(b.cost)}<span className="muted">{delta(a.cost, b.cost, usd)}</span></td></tr>
              {steps[0].length > 0 && <tr className="sep"><th colSpan={3}>{t("rp.steps")}</th></tr>}
              {steps[0].map((x, n) => [x, steps[1][n]] as const).filter(([x, y]) => !only || stepText(x) !== stepText(y)).map(([x, y]) => (
                <tr key={x.node} className={stepText(x) !== stepText(y) ? "diff" : undefined}>
                  <th className="mono">{stepText(x) !== stepText(y) && <span className="rp-ne" aria-label={t("rp.cmpDiffers")}>≠ </span>}{x.node}</th>
                  <td>{stepCell(a, x)}</td><td>{stepCell(b, y)}</td></tr>))}
              {values.length > 0 && <tr className="sep"><th colSpan={3}>{t("rp.cmpValues")}</th></tr>}
              {values.filter(([, x, y]) => !only || differs(x, y)).map(([k, x, y]) => row(k, x, y, differs(x, y)))}
            </tbody>
          </table>}
          <p className="muted small">{t("rp.cmpHint")}</p>
        </div>
      </div>
    </div>);
}

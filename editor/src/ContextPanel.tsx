// The workspace context in the board's side panel: the state of each root (never explored,
// explored, commits since), Explore / Update (the context keeper, a model run), the person's
// notes, and each section to read or correct by hand.
import { useCallback, useEffect, useRef, useState } from "react";
import { api } from "./api";
import { useI18n } from "./i18n";
import { Markdown } from "./Markdown";
import type { WorkspaceContext } from "./types";
import { wsErrorText } from "./Workspaces";

/** Waits until a run is no longer running (polls the runs list). */
export async function waitRun(id: string): Promise<string> {
  for (;;) {
    await new Promise((r) => setTimeout(r, 1200));
    const r = (await api.runs()).find((x) => x.id === id);
    if (!r || r.status !== "running") return r?.status ?? "done";
  }
}

type RootState = "waiting" | "running" | "done" | "error";
/** A context keeper run being followed: its roots and the last things it did. */
interface Activity { id: string; mode: string; started: number; roots: Record<string, RootState>;
  lines: string[]; status: string }

/** The last few things a run does, as short lines (tool and its detail, or what it says). */
function activityLine(e: Record<string, unknown>): string | null {
  const where = String(e.sub ?? "").replace(/^context:/, "");
  if (e.type === "tool") return `${where} · ${String(e.tool)}${e.detail ? ` — ${String(e.detail)}` : ""}`;
  if (e.type === "text" && typeof e.text === "string") return `${where} · ${e.text.replace(/\s+/g, " ").slice(0, 140)}`;
  if (e.type === "tool_error") return `${where} · ✗ ${String(e.tool)}: ${String(e.error ?? "").slice(0, 120)}`;
  return null;
}

export function ContextPanel({ flash }: { flash: (text: string, bad?: boolean) => void }) {
  const { t, lang } = useI18n();
  const [ctx, setCtx] = useState<WorkspaceContext | null>(null);
  const [act, setAct] = useState<Activity | null>(null);
  const [now, setNow] = useState(Date.now());
  const [notes, setNotes] = useState("");
  const [edit, setEdit] = useState<Record<string, string>>({});
  const alive = useRef(true);
  const load = useCallback(() => api.context().then((c) => { setCtx(c); setNotes(c.sections.notes ?? ""); })
    .catch((e) => flash(String(e), true)), [flash]);

  /** Follows a context keeper run live (its event stream) until it ends. */
  const follow = useCallback(async (id: string) => {
    setAct({ id, mode: "", started: Date.now(), roots: {}, lines: [], status: "running" });
    try {
      const r = await fetch(`/api/runs/${id}/events?after=0`);
      if (!r.body) return;
      const reader = r.body.getReader();
      const dec = new TextDecoder();
      let buf = "";
      for (;;) {
        const { value, done } = await reader.read();
        if (done || !alive.current) break;
        buf += dec.decode(value, { stream: true });
        let nl;
        while ((nl = buf.indexOf("\n")) >= 0) {
          const raw = buf.slice(0, nl).trim();
          buf = buf.slice(nl + 1);
          if (!raw) continue;
          const e = JSON.parse(raw) as Record<string, unknown>;
          setAct((a) => {
            if (!a) return a;
            const n = { ...a, roots: { ...a.roots }, lines: a.lines };
            if (e.type === "start") {
              n.mode = String(e.context ?? "");
              if (typeof e.ts === "number") n.started = e.ts * 1000;
              for (const root of (e.roots as string[] | undefined) ?? []) n.roots[root] = "waiting";
            } else if (e.type === "context" && typeof e.root === "string") {
              n.roots[e.root] = e.status === "running" ? "running" : e.status === "done" ? "done" : "error";
            } else if (e.type === "end") n.status = String(e.status);
            const line = activityLine(e);
            if (line) n.lines = [...a.lines, line].slice(-5);
            return n;
          });
        }
      }
    } catch { /* the stream closed: the state below says how it ended */ }
    if (!alive.current) return;
    const runs = await api.runs().catch(() => []);
    const status = runs.find((x) => x.id === id)?.status ?? "done";
    setAct(null);
    flash(t(status === "done" ? "ctx.done" : status === "stopped" ? "ctx.stopped" : "ctx.failed"),
      status !== "done" && status !== "stopped");
    load();
  }, [flash, load, t]);

  // a context run already going (started from the board menu, a new root, or another tab)
  useEffect(() => {
    alive.current = true;
    load();
    api.runs().then((rs) => {
      const r = rs.find((x) => x.flow === "workspace_context" && x.status === "running");
      if (r) follow(r.id);
    }).catch(() => undefined);
    return () => { alive.current = false; };
  }, [load, follow]);
  useEffect(() => {  // the elapsed time while it runs
    if (!act) return;
    const h = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(h);
  }, [act]);
  if (!ctx || ctx.studio) return null;
  const roots = Object.entries(ctx.roots);
  const busy = act !== null;

  const run = async (mode: "explore" | "update", only: string[] | null) => {
    try {
      const r = await api.updateContext(mode, only);
      await follow(r.id);
    } catch (e) { flash(wsErrorText(e, lang), true); }
  };
  const stop = async () => {
    if (!act) return;
    await fetch(`/api/runs/${act.id}/stop`, { method: "POST", headers: { "X-Flow-Editor": "1" } }).catch(() => undefined);
  };
  const secs = act ? Math.max(0, Math.round((now - act.started) / 1000)) : 0;
  const elapsed = `${Math.floor(secs / 60)}:${String(secs % 60).padStart(2, "0")}`;
  const mark: Record<RootState, string> = { waiting: "…", running: "⟳", done: "✓", error: "✗" };
  const save = async (name: string, text: string) => {
    try { const c = await api.setContext(name, text); setCtx(c); flash(t("ctx.saved")); }
    catch (e) { flash(wsErrorText(e, lang), true); }
  };
  const stateOf = (r: WorkspaceContext["roots"][string]) => !r.exists ? t("ctx.missing")
    : !r.explored ? t("ctx.never")
    : r.commits_since ? t("ctx.behind", { n: String(r.commits_since) })
    : t("ctx.upToDate", { date: r.updated ? new Date(r.updated * 1000).toLocaleDateString(lang) : "—" });

  return (
    <div className="ctx-panel">
      <h3>{t("ctx.title")}</h3>
      <p className="muted small">{t("ctx.hint")}</p>
      {roots.length === 0 && <p className="muted small">{t("ctx.noRoots")}</p>}
      {act && (
        <div className="ctx-progress" role="status" aria-live="polite">
          <div className="row">
            <span className="spinner" aria-hidden="true" />
            <b className="grow">{t(act.mode === "explore" ? "ctx.progressExplore" : "ctx.progressUpdate")} · {elapsed}</b>
            <button className="small danger" onClick={stop}>{t("run.stop")}</button>
          </div>
          <div className="ctx-progress-roots">
            {Object.entries(act.roots).map(([r, s]) => (
              <span key={r} className={`badge ctx-${s}`} title={t(`ctx.state.${s}`)}>{mark[s]} {r}</span>))}
          </div>
          {act.lines.length > 0
            ? <ul className="ctx-progress-lines">{act.lines.map((l, i) => <li key={i} className="mono">{l}</li>)}</ul>
            : <span className="muted small">{t("ctx.progressStarting")}</span>}
          <span className="muted small">{t("ctx.progressHint")}</span>
        </div>)}
      <table className="ws-table ctx-roots"><tbody>
        {roots.map(([name, r]) => (
          <tr key={name}>
            <td className="mono" title={r.path}>{name}<span className="muted small root-abs">{r.path}</span></td>
            <td className={act?.roots[name] === "running" ? "ctx-running" : r.explored && !r.commits_since ? "ok" : "muted"}>
              {act?.roots[name] === "running" ? t("ctx.state.running")
                : act?.roots[name] === "waiting" ? t("ctx.state.waiting") : stateOf(r)}</td>
            <td>
              <button className="small" disabled={!!busy || !r.exists} onClick={() => run(r.explored ? "update" : "explore", [name])}
                title={r.explored ? t("ctx.updateRootTitle") : t("ctx.exploreTitle")}>
                {r.explored ? t("ctx.updateRoot") : t("ctx.explore")}</button>
            </td>
          </tr>))}
      </tbody></table>
      <div className="row">
        <button className="primary small" disabled={!!busy || !roots.length} onClick={() => run("update", null)}
          title={t("ctx.costHint")}>{t("ctx.updateAll")}</button>
        <button className="small" disabled={!!busy || !roots.length} onClick={() => run("explore", null)}
          title={t("ctx.costHint")}>{t("ctx.exploreAll")}</button>
      </div>
      {ctx.chars !== undefined && ctx.limit !== undefined && (
        <p className="muted small">{t("ctx.size", { n: String(ctx.chars), limit: String(ctx.limit) })}</p>)}
      <label className="field"><span className="field-label">{t("ctx.notes")}</span>
        <textarea rows={3} value={notes} placeholder={t("ctx.notesPlaceholder")} onChange={(e) => setNotes(e.target.value)} />
        <span className="field-hint">{t("ctx.notesHint")}</span></label>
      <button className="small" disabled={notes === (ctx.sections.notes ?? "")} onClick={() => save("notes", notes)}>{t("app.save")}</button>
      {roots.filter(([n]) => ctx.sections[n]).map(([n]) => (
        <details key={n} className="ctx-section">
          <summary className="small"><b>{n}</b></summary>
          {edit[n] === undefined
            ? <><Markdown text={ctx.sections[n]} />
              <button className="small" onClick={() => setEdit((x) => ({ ...x, [n]: ctx.sections[n] }))}>{t("ctx.edit")}</button></>
            : <>
              <textarea className="mono" rows={12} value={edit[n]} onChange={(e) => setEdit((x) => ({ ...x, [n]: e.target.value }))} />
              <div className="row">
                <button className="small primary" onClick={() => save(n, edit[n]).then(() => setEdit(({ [n]: _, ...rest }) => rest))}>{t("app.save")}</button>
                <button className="small" onClick={() => setEdit(({ [n]: _, ...rest }) => rest)}>{t("dialog.cancel")}</button>
              </div>
            </>}
        </details>))}
    </div>
  );
}

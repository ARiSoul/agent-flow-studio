// Studio workspaces: the selector in the top bar (workspace + environment), new / open /
// duplicate / forget, the settings panel (roots, variables, environments) and "Copy to workspace…"
// for flows, agents, connectors and variables (with the dependency check of the server).
import { useCallback, useEffect, useState } from "react";
import { api, currentWorkspaceName, ServerError, setWorkspaceCookies } from "./api";
import { ContextMenu, type MenuState } from "./ContextMenu";
import { useDialog } from "./Dialog";
import { FolderBrowser } from "./FolderBrowser";
import { useI18n } from "./i18n";
import { localizeServer } from "./i18n/server";
import type { CopyItem, CopyPlan, WorkspaceItem, WorkspaceNow, WorkspaceSpecRaw } from "./types";

const WS_NAME = /^[a-z][a-z0-9_-]{0,39}$/;
const KEY = /^[a-z][a-z0-9_]{0,39}$/;
const KINDS = ["dev", "staging", "prod"] as const;

/** A long folder as its start and its last two parts ("C:\…\repos\game"); the full path goes in
 * a title. */
export function shortPath(p: string | null | undefined): string {
  if (!p) return "";
  const sep = p.includes("\\") ? "\\" : "/";
  const parts = p.split(/[\\/]+/).filter(Boolean);
  if (parts.length <= 3) return p;
  const head = p.startsWith("/") ? `/${parts[0]}` : parts[0];
  return [head, "…", ...parts.slice(-2)].join(sep);
}

/** An error in the UI language (workspace and copy errors come with a code). */
export function wsErrorText(e: unknown, lang: string): string {
  if (e instanceof ServerError && e.code) return localizeServer({ code: e.code, params: e.params }, e.message, lang);
  return String((e as Error)?.message ?? e);
}

/** Opens a workspace: every flow, agent, connector and run belongs to it, so the page reloads. */
export function switchWorkspace(name: string, env: string | null) {
  setWorkspaceCookies(name, env);
  window.location.reload();
}

/** The actions on workspaces and copies, with their dialogs. `flash` shows the result. */
export function useWorkspaceActions(current: WorkspaceNow | undefined, flash: (text: string, bad?: boolean) => void) {
  const { t, lang } = useI18n();
  const dialog = useDialog();
  const here = current?.name ?? currentWorkspaceName();

  const placeFields = (repoHint: string) => [
    { key: "where", label: t("ws.where"), value: "local", hint: t("ws.whereHint"),
      options: [{ value: "local", label: t("ws.whereLocal") }, { value: "repo", label: t("ws.whereRepo") }] },
    { key: "repo", label: t("ws.repo"), value: "", placeholder: "D:\\repos\\my-app", hint: repoHint, browse: "folder" as const,
      when: (v: Record<string, string>) => v.where === "repo",
      validate: (v: string) => (v.trim() ? null : t("app.required")) },
  ];
  const nameField = (taken: string[], value = "") => ({
    key: "name", label: t("ws.name"), value, hint: t("ws.nameHint"), placeholder: "my-project",
    validate: (v: string) => !WS_NAME.test(v.trim()) ? t("ws.nameHint")
      : taken.includes(v.trim()) || v.trim() === "studio" ? t("ws.nameTaken") : null,
  });

  const create = useCallback(async () => {
    const list = (await api.workspaces()).workspaces.map((w) => w.name);
    const r = await dialog.form({ title: t("ws.newTitle"), message: t("ws.newMessage"), ok: t("app.create"), fields: [
      nameField(list), { key: "title", label: t("ws.title"), value: "" }, ...placeFields(t("ws.repoHint")),
    ] });
    if (!r) return;
    try {
      const ws = await api.createWorkspace({ name: r.name.trim(), title: r.title, where: r.where as "repo" | "local",
        repo: r.where === "repo" ? r.repo.trim() : undefined });
      switchWorkspace(ws.name, null);
    } catch (e) { flash(wsErrorText(e, lang), true); }
  }, [dialog, t, lang, flash]);

  const open = useCallback(async () => {
    const r = await dialog.form({ title: t("ws.openTitle"), message: t("ws.openMessage"), ok: t("ws.openOk"), fields: [
      { key: "path", label: t("ws.folder"), placeholder: "D:\\repos\\my-app", browse: "folder" as const,
        validate: (v: string) => (v.trim() ? null : t("app.required")) },
    ] });
    if (!r) return;
    try {
      const ws = await api.openWorkspace(r.path.trim());
      switchWorkspace(ws.name, null);
    } catch (e) { flash(wsErrorText(e, lang), true); }
  }, [dialog, t, lang, flash]);

  const duplicate = useCallback(async (src: string) => {
    const list = (await api.workspaces()).workspaces.map((w) => w.name);
    const r = await dialog.form({ title: t("ws.duplicateTitle", { name: src }), message: t("ws.duplicateMessage"),
      ok: t("ws.duplicateOk"), fields: [
        nameField(list, `${src}_copy`.slice(0, 40)), { key: "title", label: t("ws.title"), value: "" },
        ...placeFields(t("ws.repoHint")),
        { key: "same", label: t("ws.sameSecrets"), checkbox: true, value: "", hint: t("ws.sameSecretsHint") },
      ] });
    if (!r) return;
    try {
      const res = await api.duplicateWorkspace(src, { name: r.name.trim(), title: r.title,
        where: r.where as "repo" | "local", repo: r.where === "repo" ? r.repo.trim() : undefined, same_secrets: r.same === "1" });
      if (res.missing_secrets.length) await dialog.confirm({ title: t("ws.secretsTitle"),
        message: t("ws.secretsMissing", { list: res.missing_secrets.join(", ") }), ok: t("dialog.close") });
      switchWorkspace(res.name, null);
    } catch (e) { flash(wsErrorText(e, lang), true); }
  }, [dialog, t, lang, flash]);

  const forget = useCallback(async (name: string) => {
    if (!(await dialog.confirm({ title: t("ws.forgetTitle", { name }), message: t("ws.forgetMessage"),
      ok: t("ws.forgetOk"), danger: true }))) return;
    await api.forgetWorkspace(name);
    switchWorkspace("studio", null);
  }, [dialog, t]);

  /** "Copy / Move to workspace…": target (and name), then the dependency check, then the copy
   * (a move also takes the original out of this workspace). */
  const copyTo = useCallback(async (item: CopyItem, label: string, how: "copy" | "move" = "copy") => {
    const moving = how === "move";
    const list = (await api.workspaces()).workspaces.filter((w) => w.ok && (!moving || w.name !== here));
    if (!list.length) return flash(t("copy.noOther"), true);
    const step1 = await dialog.form({ title: t(moving ? "copy.moveTitle" : "copy.title", { what: label }),
      message: t(moving ? (item.kind === "flow_agent" ? "copy.moveAgentMessage" : "copy.moveMessage") : "copy.message"),
      ok: t("app.next"), fields: [
        { key: "to", label: t("copy.to"), value: list.find((w) => w.name !== here)?.name ?? here,
          options: list.map((w) => {
            const title = w.name === "studio" ? t("ws.studio") : w.title;
            return { value: w.name, label: w.name === here ? t("copy.thisWorkspace", { name: title }) : title };
          }) },
        ...(item.kind === "vars" ? [] : [{ key: "name", label: t("copy.newName"), value: item.kind === "flow_agent" ? item.agent ?? "" : item.name ?? "",
          hint: t("copy.newNameHint") }]),
      ] });
    if (!step1) return;
    const to = step1.to;
    let into: string | null = null;
    if (item.kind === "flow_agent") {  // into the target's library, or into one of its flows
      const flows = await api.workspaceFlows(to);
      const r = await dialog.form({ title: t("copy.intoTitle"), ok: t("app.next"), fields: [
        { key: "into", label: t("copy.into"), value: "",
          options: [{ value: "", label: t("copy.intoLibrary") }, ...flows.map((f) => ({ value: f.name, label: t("copy.intoFlow", { name: f.name }) }))] },
      ] });
      if (!r) return;
      into = r.into || null;
    }
    const body = { ...item, to_flow: into, to, new_name: step1.name?.trim() || null };
    let plan: CopyPlan;
    try { plan = await api.copyPlan(here, body); } catch (e) { return flash(wsErrorText(e, lang), true); }
    const depLabel = (k: string) => t(`copy.dep.${k}` as "copy.dep.var");
    const fields = [
      ...(plan.clash ? [
        { key: "new_name", label: t("copy.newName"), value: plan.suggestion ?? "", hint: t("copy.clash", { name: plan.name ?? "" }) },
        { key: "replace", label: t("copy.replace"), checkbox: true, value: "", hint: t("copy.replaceHint") },
      ] : []),
      ...((plan.clash_vars?.length || plan.clash_environments?.length) ? [
        { key: "replace", label: t("copy.replaceVars", { list: [...(plan.clash_vars ?? []), ...(plan.clash_environments ?? [])].join(", ") }),
          checkbox: true, value: "" },
      ] : []),
      ...plan.deps.map((d) => ({
        key: `dep:${d.kind}:${d.name}`, label: `${depLabel(d.kind)} ${d.name}`, value: d.choice,
        hint: d.in_target ? t("copy.depInTarget") : d.in_source ? t("copy.depMissing") : t("copy.depNowhere"),
        options: [
          ...(d.copyable ? [{ value: "copy", label: t(d.in_target ? "copy.choiceReplace" : "copy.choiceCopy") }] : []),
          ...(d.in_target ? [{ value: "target", label: t("copy.choiceTarget") }] : []),
          { value: "leave", label: t("copy.choiceLeave") },
        ],
      })),
      ...(plan.secrets?.length || plan.deps.some((d) => d.kind === "connector" && d.copyable)
        ? [{ key: "same", label: t("ws.sameSecrets"), checkbox: true, value: "", hint: t("ws.sameSecretsHint") }] : []),
    ];
    let answers: Record<string, string> = {};
    if (fields.length) {
      const r = await dialog.form({ title: t(moving ? "copy.movePlanTitle" : "copy.planTitle", { what: label, to }),
        message: plan.deps.length ? t("copy.planMessage") : undefined, ok: t(moving ? "copy.moveOk" : "copy.ok"), fields });
      if (!r) return;
      answers = r;
    } else if (!(await dialog.confirm({ title: t(moving ? "copy.movePlanTitle" : "copy.planTitle", { what: label, to }),
      message: t("copy.nothingElse"), ok: t(moving ? "copy.moveOk" : "copy.ok") }))) return;
    const deps = Object.fromEntries(Object.entries(answers).filter(([k]) => k.startsWith("dep:")).map(([k, v]) => [k.slice(4), v]));
    try {
      const send = { ...body, new_name: answers.new_name?.trim() || body.new_name,
        replace: answers.replace === "1", deps, same_secrets: answers.same === "1" };
      const res = moving ? await api.move(here, send) : await api.copy(here, send);
      const what = res.copied.map((c) => (c.name === c.as ? c.as : `${c.name} → ${c.as}`)).join(", ");
      flash(t(moving ? "copy.movedDone" : "copy.done", { what: what || "—", to }) + (res.missing_secrets.length
        ? ` · ${t("copy.secretsToSet", { list: res.missing_secrets.join(", ") })}` : ""));
      return res;
    } catch (e) { flash(wsErrorText(e, lang), true); }
  }, [dialog, t, lang, flash, here]);

  return { create, open, duplicate, forget, copyTo };
}

/** The top bar part: workspace and environment selectors, and the workspace menu. */
export function WorkspaceBar({ current, beforeSwitch, onEnv, flash }: {
  current: WorkspaceNow | undefined;
  beforeSwitch: () => Promise<boolean>;
  onEnv: () => void;
  flash: (text: string, bad?: boolean) => void;
}) {
  const { t } = useI18n();
  const [list, setList] = useState<WorkspaceItem[]>([]);
  const [menu, setMenu] = useState<MenuState | null>(null);
  const acts = useWorkspaceActions(current, flash);
  useEffect(() => { api.workspaces().then((r) => setList(r.workspaces)).catch(() => undefined); }, []);
  const name = current?.name ?? "studio";
  const studio = name === "studio";
  const choose = async (v: string) => { if (v !== name && (await beforeSwitch())) switchWorkspace(v, null); };
  const openMenu = (x: number, y: number) => setMenu({ x, y, title: t("ws.menu"), items: [
    { label: t("ws.new"), onSelect: () => acts.create() },
    { label: t("ws.open"), onSelect: () => acts.open() },
    { label: t("ws.duplicate"), disabled: studio, title: studio ? t("ws.duplicateStudio") : undefined,
      onSelect: () => acts.duplicate(name) },
    { kind: "sep" },
    { label: t("ws.forget"), danger: true, disabled: studio, onSelect: () => acts.forget(name) },
  ] });
  return (
    <span className="ws-bar" role="group" aria-label={t("ws.label")}>
      <label className="bar-label" htmlFor="ws-select">{t("ws.label")}</label>
      <select id="ws-select" value={name} title={name === "studio" ? t("ws.studioTitle") : t("ws.selectTitle")} onChange={(e) => choose(e.target.value)}>
        {list.length === 0 && <option value={name}>{current?.title ?? name}</option>}
        {list.map((w) => <option key={w.name} value={w.name} disabled={!w.ok}>
          {w.name === "studio" ? t("ws.studioShort") : w.title}{w.ok ? "" : ` (${t("ws.missing")})`}</option>)}
      </select>
      {(current?.environments.length ?? 0) > 0 && (
        <select className={`env env-${current?.kind ?? "dev"}`} value={current?.environment ?? ""} title={t("ws.envTitle")}
          aria-label={t("ws.env")} onChange={(e) => { setWorkspaceCookies(name, e.target.value); onEnv(); }}>
          {current!.environments.map((e) => <option key={e} value={e}>{e}</option>)}
        </select>)}
      <button className="icon-btn" title={t("ws.menu")} aria-label={t("ws.menu")} aria-haspopup="menu"
        onClick={(e) => { const r = (e.currentTarget as HTMLElement).getBoundingClientRect(); openMenu(r.left, r.bottom + 2); }}>⋯</button>
      {menu && <ContextMenu menu={menu} onClose={() => setMenu(null)} />}
    </span>
  );
}

/** Roots, variables and environments of a workspace (the side panel of the board). */
export function WorkspaceSettings({ name, flash, onSaved }: { name: string; flash: (text: string, bad?: boolean) => void;
  onSaved: () => void }) {
  const { t, lang } = useI18n();
  const [spec, setSpec] = useState<WorkspaceSpecRaw | null>(null);
  const [saved, setSaved] = useState("");
  const [roots, setRoots] = useState<Record<string, string>>({});
  const [picked, setPicked] = useState<Set<string>>(new Set());
  const [err, setErr] = useState<string | null>(null);
  const [browse, setBrowse] = useState<string | null>(null);  // the root whose folder browser is open
  const [home, setHome] = useState("");
  const [place, setPlace] = useState<{ where: string; folder: string | null }>({ where: "", folder: null });
  const dialog = useDialog();
  const acts = useWorkspaceActions({ name, where: "local", environment: null, environments: [], kind: null }, flash);
  const load = useCallback(() => api.workspace(name).then((w) => {
    if (w.spec) { setSpec(w.spec); setSaved(JSON.stringify(w.spec)); }
    setRoots(w.roots ?? {});
    setHome(w.home ?? "");
    setPlace({ where: w.where, folder: w.folder });
  }).catch((e) => setErr(String(e))), [name]);
  useEffect(() => { load(); }, [load]);
  if (!spec) return <p className="muted small">{err ?? t("app.loading")}</p>;
  const dirty = JSON.stringify(spec) !== saved;
  const set = (fn: (s: WorkspaceSpecRaw) => void) => setSpec((s) => { const n = structuredClone(s!); fn(n); return n; });
  const envs = Object.keys(spec.environments);
  const allVars = [...new Set([...Object.keys(spec.vars), ...envs.flatMap((e) => Object.keys(spec.environments[e].vars ?? {}))])].sort();
  const toggle = (k: string) => setPicked((p) => { const n = new Set(p); if (n.has(k)) n.delete(k); else n.add(k); return n; });
  /** A folder inside the workspace home is kept relative to it (it travels with the repository). */
  const relative = (p: string) => {
    const h = home.replace(/[\\/]+$/, "");
    if (!h) return p;
    if (p.toLowerCase() === h.toLowerCase()) return ".";
    const low = p.toLowerCase(), hl = h.toLowerCase();
    if (low.startsWith(hl + "\\") || low.startsWith(hl + "/")) return p.slice(h.length + 1).replace(/\\/g, "/");
    return p;
  };
  const save = async () => {
    setErr(null);
    const before = Object.keys((JSON.parse(saved || "{}") as WorkspaceSpecRaw).roots ?? {});
    let added = Object.keys(spec.roots).filter((r) => !before.includes(r));
    try {
      const w = await api.updateWorkspace(name, { title: spec.title, description: spec.description, roots: spec.roots,
        vars: spec.vars, environments: spec.environments, default_environment: spec.default_environment ?? null,
        preview: spec.preview?.url ? spec.preview : null });
      if (w.spec) { setSpec(w.spec); setSaved(JSON.stringify(w.spec)); }
      setRoots(w.roots ?? {});
      // a root that only changed its name kept its explored context: nothing new to explore
      const renamed = Object.values(w.renamed_roots ?? {});
      added = added.filter((r) => !renamed.includes(r));
      flash(t("ws.saved"));
      onSaved();
    } catch (e) { return setErr(wsErrorText(e, lang)); }
    // a new folder of the workspace: explore it now (a model run), so the flows know it
    if (added.length && await dialog.confirm({ title: t("ctx.exploreNewTitle", { roots: added.join(", ") }),
      message: t("ctx.exploreNewMessage"), ok: t("ctx.explore") })) {
      try {
        await api.updateContext("explore", added);
        onSaved();  // the context block (above) finds the run and shows its progress
      } catch (e) { flash(wsErrorText(e, lang), true); }
    }
  };
  /** A name typed in a table (root, environment, variable): spaces and "-" become "_"; a name that
   * is still invalid or already used is refused with a message and the field shows the old name. */
  const commitName = (kind: "var" | "env" | "root", from: string, input: HTMLInputElement) => {
    const typed = input.value.trim();
    const to = typed.toLowerCase().replace(/[\s-]+/g, "_");
    if (to === from) { input.value = from; return; }
    const used = kind === "var" ? allVars : kind === "env" ? envs : Object.keys(spec.roots);
    if (!KEY.test(to) || used.includes(to)) {
      input.value = from;
      flash(t(!KEY.test(to) ? "ws.badName" : "ws.nameUsed", { name: typed }), true);
      return;
    }
    if (to !== typed) flash(t("ws.nameChanged", { from: typed, to }));
    input.value = to;
    rename(kind, from, to);
  };
  /** The same object with one key renamed, in the same place (the tables keep their order). */
  const rekey = <V,>(o: Record<string, V>, from: string, to: string): Record<string, V> =>
    Object.fromEntries(Object.entries(o).map(([k, v]) => [k === from ? to : k, v]));
  const rename = (kind: "var" | "env" | "root", from: string, to: string) => set((s) => {
    if (!KEY.test(to) || to === from) return;
    if (kind === "var") {
      if (from in s.vars) s.vars = rekey(s.vars, from, to);
      for (const e of Object.values(s.environments)) if (e.vars && from in e.vars) e.vars = rekey(e.vars, from, to);
    } else if (kind === "env") {
      s.environments = rekey(s.environments, from, to);
      if (s.default_environment === from) s.default_environment = to;
    } else { s.roots = rekey(s.roots, from, to); }
  });
  const pickedVars = allVars.filter((v) => picked.has(`var:${v}`));
  const pickedEnvs = envs.filter((e) => picked.has(`env:${e}`));
  return (
    <div className="ws-settings">
      <label className="field"><span className="field-label">{t("ws.title")}</span>
        <input value={spec.title ?? ""} onChange={(e) => set((s) => { s.title = e.target.value; })} /></label>
      <label className="field"><span className="field-label">{t("insp.description")}</span>
        <textarea rows={2} value={spec.description ?? ""} onChange={(e) => set((s) => { s.description = e.target.value; })} /></label>

      <h3>{t("ws.roots")}</h3>
      <p className="muted small">{t("ws.rootsHint")}</p>
      <table className="ws-table ws-roots"><tbody>
        {Object.entries(spec.roots).flatMap(([k, r]) => [
          <tr key={k}>
            <td><input className="mono" defaultValue={k} aria-label={t("ws.name")} title={t("ws.nameRule")}
              onBlur={(e) => commitName("root", k, e.target)} onKeyDown={(e) => { if (e.key === "Enter") e.currentTarget.blur(); }} /></td>
            <td><input className="mono" value={r.path} aria-label={t("ws.folder")} title={roots[k]}
              onChange={(e) => set((s) => { s.roots[k].path = e.target.value; })} />
              {roots[k] && <span className="muted small root-abs" title={roots[k]}>→ {shortPath(roots[k])}</span>}</td>
            <td className="nowrap">
              <button className="icon" title={t("fb.browse")} aria-expanded={browse === k}
                onClick={() => setBrowse(browse === k ? null : k)}>📁</button>
              <button className="icon" title={t("menu.delete")} onClick={() => set((s) => { delete s.roots[k]; })}>✕</button></td>
          </tr>,
          browse === k && <tr key={`${k}:browse`}><td colSpan={3}>
            <FolderBrowser start={roots[k] ?? home} onClose={() => setBrowse(null)}
              onPick={(p) => { set((s) => { s.roots[k].path = relative(p); }); setBrowse(null); }} />
          </td></tr>,
        ])}
      </tbody></table>
      <button className="small" onClick={() => set((s) => { s.roots[uniq(Object.keys(s.roots), "root")] = { path: "." }; })}>{t("ws.addRoot")}</button>

      <h3>{t("ws.environments")}</h3>
      <p className="muted small">{t("ws.environmentsHint")}</p>
      <table className="ws-table"><tbody>
        {envs.map((e) => (
          <tr key={e}>
            <td><input type="checkbox" checked={picked.has(`env:${e}`)} aria-label={t("ws.pick")} onChange={() => toggle(`env:${e}`)} /></td>
            <td><input className="mono" defaultValue={e} aria-label={t("ws.name")} title={t("ws.nameRule")}
              onBlur={(x) => commitName("env", e, x.target)} onKeyDown={(x) => { if (x.key === "Enter") x.currentTarget.blur(); }} /></td>
            <td><select value={spec.environments[e].kind} aria-label={t("ws.kind")}
              onChange={(x) => set((s) => { s.environments[e].kind = x.target.value as "dev"; })}>
              {KINDS.map((k) => <option key={k} value={k}>{t(`ws.kind.${k}`)}</option>)}</select></td>
            <td><label className="check small"><input type="radio" name="default-env" checked={(spec.default_environment ?? envs[0]) === e}
              onChange={() => set((s) => { s.default_environment = e; })} /> {t("ws.default")}</label></td>
            <td><button className="icon" title={t("menu.delete")} disabled={envs.length < 2}
              onClick={() => set((s) => { delete s.environments[e]; if (s.default_environment === e) s.default_environment = null; })}>✕</button></td>
          </tr>))}
      </tbody></table>
      <button className="small" onClick={() => set((s) => { s.environments[uniq(Object.keys(s.environments), "env")] = { kind: "dev", vars: {} }; })}>{t("ws.addEnv")}</button>

      <h3>{t("ws.preview")}</h3>
      <p className="muted small">{t("ws.previewHint")}</p>
      <div className="row ws-preview">
        <input className="mono grow" value={spec.preview?.url ?? ""} placeholder="http://localhost:5173/" aria-label={t("ws.previewUrl")}
          onChange={(e) => set((s) => { s.preview = { ...(s.preview ?? { url: "" }), url: e.target.value }; })} />
        <input className="mono" value={spec.preview?.start ?? ""} placeholder="npm run dev" aria-label={t("ws.previewStart")}
          title={t("ws.previewStartTitle")}
          onChange={(e) => set((s) => { s.preview = { ...(s.preview ?? { url: "" }), start: e.target.value }; })} />
      </div>

      <h3>{t("ws.vars")}</h3>
      <p className="muted small">{t("ws.varsHint")}</p>
      <div className="ws-table-wrap"><table className="ws-table vars">
        <thead><tr><th /><th>{t("ws.name")}</th><th>{t("ws.base")}</th>{envs.map((e) => <th key={e}>{e}</th>)}<th /></tr></thead>
        <tbody>{allVars.map((v) => (
          <tr key={v}>
            <td><input type="checkbox" checked={picked.has(`var:${v}`)} aria-label={t("ws.pick")} onChange={() => toggle(`var:${v}`)} /></td>
            <td><input className="mono" defaultValue={v} aria-label={t("ws.name")} title={t("ws.nameRule")}
              onBlur={(e) => commitName("var", v, e.target)} onKeyDown={(e) => { if (e.key === "Enter") e.currentTarget.blur(); }} /></td>
            <td><input value={spec.vars[v] ?? ""} placeholder="—" aria-label={`${v} · ${t("ws.base")}`}
              onChange={(e) => set((s) => { if (e.target.value) s.vars[v] = e.target.value; else delete s.vars[v]; })} /></td>
            {envs.map((en) => (
              <td key={en}><input value={spec.environments[en].vars?.[v] ?? ""} placeholder={spec.vars[v] ?? "—"} aria-label={`${v} · ${en}`}
                onChange={(e) => set((s) => { const m = (s.environments[en].vars = s.environments[en].vars ?? {});
                  if (e.target.value) m[v] = e.target.value; else delete m[v]; })} /></td>))}
            <td><button className="icon" title={t("menu.delete")} onClick={() => set((s) => {
              delete s.vars[v]; for (const e of Object.values(s.environments)) delete e.vars?.[v]; })}>✕</button></td>
          </tr>))}</tbody>
      </table></div>
      <button className="small" onClick={() => set((s) => { s.vars[uniq(allVars, "var")] = "value"; })}>{t("ws.addVar")}</button>
      <p className="muted small">{t("ws.noSecrets")}</p>
      <details className="ws-place">
        <summary className="small">{t("ws.keptIn")}</summary>
        <span className="field-label">{t(place.where === "repo" ? "ws.placeRepo" : "ws.placeLocal")}</span>
        <code className="path" title={(place.where === "repo" ? home : place.folder) ?? ""}>{shortPath(place.where === "repo" ? home : place.folder)}</code>
        <span className="muted small" title={place.folder ?? ""}>{t("ws.placeFiles", { folder: shortPath(place.folder) })}</span>
      </details>
      {err && <p className="field-err">{err}</p>}
      <div className="row">
        <button className="primary" disabled={!dirty} onClick={save}>{dirty ? t("app.save") : t("app.saved")}</button>
        <button disabled={dirty || (!pickedVars.length && !pickedEnvs.length)} title={dirty ? t("ws.saveFirst") : undefined}
          onClick={() => acts.copyTo({ kind: "vars", vars: pickedVars, environments: pickedEnvs },
            t("copy.whatVars", { n: String(pickedVars.length + pickedEnvs.length) }))}>{t("copy.button")}</button>
      </div>
    </div>
  );
}

function uniq(used: string[], base: string): string {
  let i = 1;
  while (used.includes(`${base}_${i}`)) i++;
  return `${base}_${i}`;
}

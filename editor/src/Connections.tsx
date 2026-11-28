import { useEffect, useState } from "react";
import { errorText, PRIVATE_TERMS } from "./api";
import { useDialog } from "./Dialog";
import { LANGS, useI18n } from "./i18n";
import { localizeServer, type ServerMsg } from "./i18n/server";
import { locLabel } from "./LibraryManager";
import { useWorkspaceActions } from "./Workspaces";

const MUTATE = { "Content-Type": "application/json", "X-Flow-Editor": "1" };
const SLUG = /^[a-z][a-z0-9_]{0,39}$/;
const ENTRA_TYPES = new Set(["azure", "azure_devops", "microsoft_graph"]);

interface Exchange { url: string; grant_type: string; token_param?: string }
interface Auth {
  method: string; header?: string; username?: string | null; tenant_id?: string | null; client_id?: string | null;
  token_url?: string | null; scope?: string | null; authorize_url?: string | null;
  authorize_params?: Record<string, string>; token_auth?: "body" | "basic"; token_in?: "header" | "query";
  exchange?: Exchange | null;
}
interface OAuthStatus { signed_in: boolean; expires_at: number | null; refreshable: boolean }
interface Conn {
  type: string; description?: string; instructions?: string; environment?: string; base_url?: string | null; organization?: string | null;
  test_path?: string | null; allowed_paths?: string[]; auth: Auth; secrets?: Record<string, string>; access?: string;
  actions?: Record<string, { description: string; access?: string; method?: string; path?: string;
    params?: Record<string, unknown> }>;
  actions_yaml?: string; actions_only?: boolean;
  web?: { login_url?: string | null; signed_in_selector?: string | null; signed_out_url?: string | null;
    browser?: string; timeout_s?: number };
  web_session?: { profile: boolean; signing_in: boolean } | null;
  location?: string;  // "local" (connections.yaml) or an extension (its connections.yaml)
  secret?: { field: string | null; source: string | null; set: boolean; optional?: boolean };
  oauth?: OAuthStatus | null;
}
// a website action being recorded in a visible window (connections/recorder.py)
interface Recorded {
  name: string;  // the connector
  recording: boolean; steps: Record<string, unknown>[]; params: Record<string, unknown>;
  typed?: Record<string, { label: string; value: string }>;  // param → what was typed (local only)
  posted?: boolean;  // the page sent POST requests while recording
  warnings: (ServerMsg & { msg: string })[]; problems: string[]; error: string | null; yaml?: string;
}
const stepLine = (s: Record<string, unknown>) => {
  const [k, v] = Object.entries(s)[0] ?? ["?", ""];
  return `${k}: ${typeof v === "string" ? v : JSON.stringify(v)}`;
};
// recorded_1, recorded_2…: the first name not used in the actions text
const freeActionName = (text: string) => {
  const used = new Set([...text.matchAll(/^([a-z][a-z0-9_]*):/gm)].map((m) => m[1]));
  let i = 1;
  while (used.has(`recorded_${i}`)) i++;
  return `recorded_${i}`;
};
interface Preset { label: string; base_url: string; test_path: string; auth: Partial<Auth> }
interface Meta {
  types: Record<string, { label: string; methods: string[]; base_url: string | null; test_path: string;
    oauth?: { authorize_url: string; token_url: string; scope: string } | null }>;
  methods: Record<string, { label: string; secret: string | null; optional?: boolean }>;
  environments: string[];
  file: string;
  oauth_redirect_uri: string;
  oauth_presets: Record<string, Preset>;
  connections: Record<string, Conn>;
  locations?: string[];
}

async function req<T>(url: string, method: string, body?: unknown): Promise<T> {
  const r = await fetch(url, { method, headers: MUTATE, body: body === undefined ? undefined : JSON.stringify(body) });
  const data = await r.json().catch(() => ({}));
  if (!r.ok) {
    const d = (data as { detail?: unknown }).detail;
    const terms = (d as { private_terms?: string[] } | undefined)?.private_terms;
    if (terms) throw new Error(PRIVATE_TERMS + terms.join(", "));  // public: names private words
    throw new Error(Array.isArray(d) ? d.map((x: { msg: string }) => x.msg).join("; ") : String(d ?? r.statusText));
  }
  return data as T;
}

// examples for the actions editor (YAML; the server validates them on save)
const API_ACTIONS_EXAMPLE = `get_ticket:
  description: Reads one ticket (summary, state, notes).
  path: /api/tickets/{number}
  query: {fields: "summary,state,notes"}
  params:
    number: {pattern: "INC[0-9]{7}", description: Ticket number}
add_note:
  description: Adds a work note to a ticket.
  access: write          # needs access: write; follows the flow autonomy (approval)
  method: POST
  path: /api/tickets/{number}/notes
  body: {text: "{text}"}
  params:
    number: {pattern: "INC[0-9]{7}"}
    text: {max_length: 2000}
`;
const WEB_ACTIONS_EXAMPLE = `open_ticket:
  description: Opens a ticket and reads its summary and notes.
  params:
    number: {pattern: "INC[0-9]{7}"}
  steps:
    - goto: /ticket/{number}
    - click: "text=Notes"            # a tab
    - extract: {summary: "#summary", notes: ".notes"}
search_user:
  description: Searches a user by email and reads the result table.
  params:
    email: {pattern: "[^@\\\\s]+@[^@\\\\s]+"}
  steps:
    - goto: /users
    - fill: {selector: "input[name=q]", value: "{email}"}
    - press: {selector: "input[name=q]", key: Enter}
    - wait_for: "table.results"
    - extract_table: "table.results"
`;

const blank = (type: string, meta: Meta): Conn => ({ type, environment: "dev", auth: { method: meta.types[type].methods[0] } });
const lines = (o?: Record<string, string>) => Object.entries(o ?? {}).map(([k, v]) => `${k}=${v}`).join("\n");
const unlines = (s: string) => Object.fromEntries(s.split("\n").map((l) => l.trim()).filter(Boolean).map((l) => {
  const i = l.indexOf("=");
  return i < 0 ? [l, ""] : [l.slice(0, i).trim(), l.slice(i + 1).trim()];
}));

export function Connections({ onClose }: { onClose: () => void }) {
  const { t, tOr, lang } = useI18n();
  const dialog = useDialog();
  const [meta, setMeta] = useState<Meta | null>(null);
  const [sel, setSel] = useState<string | null>(null); // name, or "" for a new one
  const [name, setName] = useState("");
  const [form, setForm] = useState<Conn | null>(null);
  const [secret, setSecret] = useState("");
  const [envVar, setEnvVar] = useState("");
  const [msg, setMsg] = useState<{ text: string; bad?: boolean } | null>(null);
  const acts = useWorkspaceActions(undefined, (text, bad) => setMsg({ text, bad }));
  const [busy, setBusy] = useState(false);
  const [rec, setRec] = useState<Recorded | null>(null);
  const [fixed, setFixed] = useState<Set<string>>(new Set());  // typed fields kept as fixed values
  const [tested, setTested] = useState<{ action: string; ok: boolean; text: string; blocked?: string[];
    params: Record<string, string>; ts?: number } | null>(null);
  // the last test of each action of the selected connector (kept on this machine)
  const [lastTests, setLastTests] = useState<Record<string, { ok: boolean; result?: unknown; error?: string;
    blocked?: string[]; params?: Record<string, string>; ts: number }>>({});
  useEffect(() => {
    setTested(null);
    if (!sel) { setLastTests({}); return; }
    fetch(`/api/connections/${sel}/tests`).then((r) => (r.ok ? r.json() : { tests: {} }))
      .then((d) => setLastTests(d.tests ?? {})).catch(() => setLastTests({}));
  }, [sel]);
  const showTest = (action: string, x: (typeof lastTests)[string]) => setTested({
    action, ok: x.ok, text: x.ok ? JSON.stringify(x.result, null, 2) : String(x.error),
    blocked: x.blocked, params: x.params ?? {}, ts: x.ts });
  const [allowPost, setAllowPost] = useState(false);  // the recorded draft lets the page POST

  const reload = async (pick?: string | null) => {
    const r = await fetch("/api/connections");
    const m = (await r.json()) as Meta;
    setMeta(m);
    const p = pick === undefined ? sel : pick;
    if (p && m.connections[p]) { setSel(p); setName(p); setForm(structuredClone(m.connections[p])); }
  };
  useEffect(() => { reload().catch((e) => setMsg({ text: String(e), bad: true })); }, []); // eslint-disable-line
  useEffect(() => {
    const k = (e: KeyboardEvent) => e.key === "Escape" && onClose();
    window.addEventListener("keydown", k);
    return () => window.removeEventListener("keydown", k);
  }, [onClose]);
  // back from the provider's sign-in tab: refresh only the sign-in state (keeps unsaved edits)
  useEffect(() => {
    const onFocus = () => {
      fetch("/api/connections").then((r) => r.json()).then((m: Meta) => setMeta((old) => old && {
        ...old, connections: Object.fromEntries(Object.entries(old.connections).map(([n, c]) =>
          [n, { ...c, oauth: m.connections[n]?.oauth ?? c.oauth }])),
      })).catch(() => undefined);
    };
    window.addEventListener("focus", onFocus);
    return () => window.removeEventListener("focus", onFocus);
  }, []);

  // while a recording window is open, read the steps it has so far
  useEffect(() => {
    if (!rec?.recording) return;
    const id = window.setInterval(() => {
      fetch(`/api/connections/${rec.name}/browser/record`).then((r) => (r.ok ? r.json() : null))
        .then((d: Omit<Recorded, "name"> | null) => d && setRec((old) => old && old.name === rec.name ? { ...d, name: rec.name } : old))
        .catch(() => undefined);
    }, 1500);
    return () => window.clearInterval(id);
  }, [rec?.recording, rec?.name]);

  if (!meta) return <div className="modal"><div className="modal-box">{t("conn.loading")}</div></div>;
  const isNew = sel === "";
  const set = (p: Partial<Conn>) => setForm((f) => (f ? { ...f, ...p } : f));
  const setAuth = (p: Partial<Auth>) => setForm((f) => (f ? { ...f, auth: { ...f.auth, ...p } } : f));
  const tp = form ? meta.types[form.type] : null;
  const m = form ? meta.methods[form.auth.method] : null;
  const saved = !isNew && sel ? meta.connections[sel] : undefined;
  const savedSecret = saved?.secret;
  const signIn = saved?.oauth ?? null;
  const method = form?.auth.method;
  const generic = form?.type === "http_api";
  const fmtDate = (s: number) => new Date(s * 1000).toLocaleString(LANGS[lang].html);

  const run = async (fn: () => Promise<void>) => {
    setBusy(true); setMsg(null);
    try { await fn(); } catch (e) { setMsg({ text: errorText(e, t), bad: true }); }
    finally { setBusy(false); }
  };
  const save = () => run(async () => {
    if (!form) return;
    if (!SLUG.test(name)) throw new Error(t("conn.slugError"));
    if (isNew && meta.connections[name]) throw new Error(t("conn.exists"));
    // the actions go as YAML text (the server parses and checks them)
    const body = { ...form, secrets: undefined, secret: undefined, oauth: undefined, actions: undefined,
      web_session: undefined, actions_yaml: form.actions_yaml ?? "",
      // a new connector goes where the field says (the open workspace first, when there is one)
      location: form.location ?? (isNew ? meta.locations?.[0] : undefined) };
    await req(`/api/connections/${name}`, "PUT", body);
    await reload(name);
    setMsg({ text: t("conn.saved") });
  });
  const saveSecret = (useEnv: boolean) => run(async () => {
    if (!sel) return;
    await req(`/api/connections/${sel}/secret`, "PUT", useEnv ? { env: envVar } : { value: secret });
    setSecret(""); setEnvVar("");
    await reload(sel);
    setMsg({ text: useEnv ? t("conn.secretEnv") : t("conn.secretStored") });
  });
  const test = () => run(async () => {
    if (!sel) return;
    const r = await req<{ ok: boolean; status?: number; elapsed_ms?: number; detail: string }>(`/api/connections/${sel}/test`, "POST");
    setMsg({ text: `${r.ok ? "✓" : "✗"} ${r.status ? `HTTP ${r.status} · ` : ""}${r.elapsed_ms ? `${r.elapsed_ms} ms · ` : ""}${r.detail}`, bad: !r.ok });
  });
  const remove = () => run(async () => {
    if (!sel || !(await dialog.confirm({ title: t("conn.deleteTitle"), message: t("conn.confirmDelete", { name: sel }),
      ok: t("conn.delete"), danger: true }))) return;
    await req(`/api/connections/${sel}`, "DELETE");
    setSel(null); setForm(null);
    await reload(null);
  });
  const startSignIn = () => run(async () => {
    if (!sel) return;
    const r = await req<{ url: string }>(`/api/connections/${sel}/oauth/start`, "POST");
    window.open(r.url, "_blank", "noopener");
    setMsg({ text: t("conn.signInOpened") });
  });
  const signOut = () => run(async () => {
    if (!sel) return;
    await req(`/api/connections/${sel}/oauth`, "DELETE");
    await reload(sel);
  });
  // website: a visible browser window where the person signs in (the studio never types it)
  const webSignIn = () => run(async () => {
    if (!sel) return;
    await req(`/api/connections/${sel}/browser/sign-in`, "POST");
    setMsg({ text: t("conn.webSignInOpened") });
    await reload(sel);
  });
  const webSignOut = () => run(async () => {
    if (!sel || !(await dialog.confirm({ title: t("conn.webSignOutTitle"), message: t("conn.webSignOutMessage"),
      ok: t("conn.signOut"), danger: true }))) return;
    await req(`/api/connections/${sel}/browser`, "DELETE");
    await reload(sel);
  });
  // website: record an action while the person clicks on the site (a draft, saved with the rest)
  const startRecord = () => run(async () => {
    if (!sel) return;
    const r = await req<Omit<Recorded, "name">>(`/api/connections/${sel}/browser/record`, "POST", { banner: t("conn.recordBanner") });
    setRec({ ...r, name: sel });
    setFixed(new Set());
    setAllowPost(false);
    setMsg({ text: t("conn.recordOpened") });
  });
  const stopRecord = () => run(async () => {
    if (rec) await req(`/api/connections/${rec.name}/browser/record`, "DELETE");
  });
  const discardRecord = () => run(async () => {
    if (rec && !rec.recording) await req(`/api/connections/${rec.name}/browser/record`, "DELETE");
    setRec(null);
  });
  const addRecorded = () => run(async () => {
    if (!rec || !form) return;
    const action = freeActionName(form.actions_yaml ?? "");
    const keep = [...fixed].map(encodeURIComponent).join(",");
    const r = await req<Omit<Recorded, "name">>(
      `/api/connections/${rec.name}/browser/record?action=${action}&fixed=${keep}&allow_post=${allowPost}`, "GET");
    if (!r.yaml) return;
    const old = (form.actions_yaml ?? "").trimEnd();
    set({ actions_yaml: (old ? `${old}
` : "") + r.yaml });
    await req(`/api/connections/${rec.name}/browser/record`, "DELETE");
    setRec(null);
    setMsg({ text: t("conn.recordAdded", { name: action }) });
  });
  const recHere = rec && rec.name === sel ? rec : null;
  // run one saved read action now (a website one in a visible window) and show what an agent gets
  const testAction = (action: string, again?: Record<string, string>) => run(async () => {
    if (!sel || !saved) return;
    const params = Object.entries(saved.actions?.[action]?.params ?? {}) as [string, { description?: string }][];
    let values: Record<string, string> = again ?? {};
    if (params.length && !again) {
      const got = await dialog.form({ title: t("conn.actionTestTitle", { name: action }), ok: t("conn.actionTest"),
        fields: params.map(([k, x]) => ({ key: k, label: k, hint: x?.description })) });
      if (!got) return;
      values = got;
    }
    setTested(null);
    setMsg({ text: t(saved.type === "website" ? "conn.actionTestingWeb" : "conn.actionTesting") });
    const r = await req<{ ok: boolean; result?: unknown; error?: string; blocked?: string[] }>(
      `/api/connections/${sel}/actions/${action}/test`, "POST", { params: values });
    setMsg(null);
    const now = Date.now() / 1000;
    setLastTests((old) => ({ ...old, [action]: { ...r, params: values, ts: now } }));
    setTested({ action, ok: r.ok, text: r.ok ? JSON.stringify(r.result, null, 2) : String(r.error),
      blocked: r.blocked, params: values, ts: now });
  });
  // the test failed and the read action blocked POST requests: allow them (the person decides) and
  // test again with the same values
  const allowPostAndRetest = () => run(async () => {
    if (!sel || !tested) return;
    await req(`/api/connections/${sel}/actions/${tested.action}/allow-post`, "POST", { allow: true });
    await reload(sel);
    testAction(tested.action, tested.params);
  });
  const setWeb = (p: Partial<NonNullable<Conn["web"]>>) => setForm((f) => (f ? { ...f, web: { ...f.web, ...p } } : f));
  const withPreset = (base: Conn, key: string): Conn => {
    const p = meta.oauth_presets[key];
    return !p ? base : {
      ...base, type: "http_api", base_url: p.base_url, test_path: p.test_path,
      auth: { method: "oauth_browser", client_id: base.auth.client_id ?? "", token_auth: "body", token_in: "header",
        authorize_params: {}, exchange: null, ...p.auth },
    };
  };
  const applyPreset = (key: string) => {
    if (!form) return;
    setForm(withPreset(form, key));
    if (isNew && !name) setName(key);
  };
  const newFromPreset = (key: string) => {
    setSel(""); setName(key); setForm(withPreset(blank("http_api", meta), key)); setMsg(null);
  };

  return (
    <div className="modal" onMouseDown={(e) => e.target === e.currentTarget && onClose()}>
      <div className="modal-box conns">
        <div className="panel-head">
          <h2>{t("conn.title")}</h2>
          <span className="muted small">{meta.file} · {t("conn.secretsWhere")}</span>
          <button className="icon-btn modal-close" onClick={onClose} aria-label={t("conn.close")} title={t("conn.close")}>×</button>
        </div>
        <div className="conns-body">
          <div className="conns-list">
            {Object.entries(meta.connections).map(([n, c]) => (
              <button key={n} className={`conn-item${sel === n ? " on" : ""}`}
                onClick={() => { setSel(n); setName(n); setForm(structuredClone(c)); setMsg(null); }}>
                <b>{n}</b>
                <span className="muted small">{meta.types[c.type]?.label} · {c.environment ?? "dev"}
                  {c.location && c.location !== "local" ? ` · ${locLabel(t, c.location)}` : ""}
                  {c.secret && !c.secret.set && !c.secret.optional ? t("conn.noSecret") : ""}
                  {c.oauth && !c.oauth.signed_in ? t("conn.notSignedInList") : ""}</span>
              </button>
            ))}
            <div className="new-conn">
              {Object.keys(meta.types).map((k) => (
                <button key={k} className="small" onClick={() => { setSel(""); setName(""); setForm(blank(k, meta)); setMsg(null); }}>
                  + {meta.types[k].label}</button>
              ))}
              <span className="muted small new-conn-group">{t("conn.appsSignIn")}</span>
              {Object.entries(meta.oauth_presets).map(([k, p]) => (
                <button key={k} className="small" onClick={() => newFromPreset(k)}>+ {p.label}</button>
              ))}
            </div>
          </div>

          {form && tp && m ? (
            <div className="conns-form">
              {generic && (
                <label className="field"><span className="field-label">{t("conn.preset")}</span>
                  <select value="" onChange={(e) => applyPreset(e.target.value)}>
                    <option value="">{t("conn.presetNone")}</option>
                    {Object.entries(meta.oauth_presets).map(([k, p]) => <option key={k} value={k}>{p.label}</option>)}
                  </select>
                  <span className="field-hint">{t("conn.presetHint")}</span></label>)}
              <div className="grid2">
                <label className="field"><span className="field-label">{t("conn.name")}</span>
                  <input value={name} disabled={!isNew} placeholder={t("conn.namePlaceholder")} onChange={(e) => setName(e.target.value)} /></label>
                <label className="field"><span className="field-label">{t("conn.environment")}</span>
                  <select value={form.environment ?? "dev"} onChange={(e) => set({ environment: e.target.value })}>
                    {meta.environments.map((x) => <option key={x}>{x}</option>)}</select></label>
              </div>
              {(meta.locations ?? ["local"]).length > 1 && (
                <label className="field"><span className="field-label">{t("loc.where")}</span>
                  <select value={form.location ?? meta.locations?.[0] ?? "local"} onChange={(e) => set({ location: e.target.value })}>
                    {meta.locations!.map((l) => <option key={l} value={l}>{locLabel(t, l)}</option>)}
                  </select>
                  <span className="field-hint">{t("loc.connWhereHint")}</span></label>)}
              <label className="field"><span className="field-label">{t("conn.access")}</span>
                <select value={form.access ?? "read"} onChange={(e) => set({ access: e.target.value })}>
                  <option value="read">{t("conn.accessRead")}</option>
                  <option value="write">{t("conn.accessWrite")}</option>
                </select>
                {form.access === "write" && <span className="field-hint">{t("conn.accessWriteHint")}</span>}</label>
              <label className="field"><span className="field-label">{t("conn.description")}</span>
                <input value={form.description ?? ""} placeholder={t("conn.descriptionPlaceholder")} onChange={(e) => set({ description: e.target.value })} /></label>
              {form.type === "azure_devops" && (
                <label className="field"><span className="field-label">{t("conn.organization")}</span>
                  <input value={form.organization ?? ""} placeholder={t("conn.organizationPlaceholder")} onChange={(e) => set({ organization: e.target.value || null })} /></label>
              )}
              {form.type === "atlassian" && (
                <label className="field"><span className="field-label">{t("conn.atlassianSite")}</span>
                  <input value={form.organization ?? ""} placeholder={t("conn.atlassianSitePlaceholder")} onChange={(e) => set({ organization: e.target.value || null })} /></label>
              )}
              <div className="grid2">
                <label className="field"><span className="field-label">{t("conn.baseUrl")}</span>
                  <input value={form.base_url ?? ""} placeholder={tp.base_url ?? "https://api.example.com"} onChange={(e) => set({ base_url: e.target.value || null })} /></label>
                {form.type !== "website" && (
                  <label className="field"><span className="field-label">{t("conn.testPath")}</span>
                    <input value={form.test_path ?? ""} placeholder={tp.test_path} onChange={(e) => set({ test_path: e.target.value || null })} /></label>)}
              </div>
              {form.type === "website" && <>
                <p className="muted small">{t("conn.webHint")}</p>
                <div className="grid2">
                  <label className="field"><span className="field-label">{t("conn.webLoginUrl")}</span>
                    <input value={form.web?.login_url ?? ""} placeholder="/login" onChange={(e) => setWeb({ login_url: e.target.value || null })} /></label>
                  <label className="field"><span className="field-label">{t("conn.webBrowser")}</span>
                    <select value={form.web?.browser ?? "msedge"} onChange={(e) => setWeb({ browser: e.target.value })}>
                      <option value="msedge">Microsoft Edge</option>
                      <option value="chrome">Google Chrome</option>
                      <option value="chromium">Chromium (Playwright)</option>
                    </select></label>
                </div>
                <div className="grid2">
                  <label className="field"><span className="field-label">{t("conn.webSignedIn")}</span>
                    <input className="mono" value={form.web?.signed_in_selector ?? ""} placeholder="#user-menu"
                      onChange={(e) => setWeb({ signed_in_selector: e.target.value || null })} />
                    <span className="field-hint">{t("conn.webSignedInHint")}</span></label>
                  <label className="field"><span className="field-label">{t("conn.webSignedOut")}</span>
                    <input className="mono" value={form.web?.signed_out_url ?? ""} placeholder="/login"
                      onChange={(e) => setWeb({ signed_out_url: e.target.value || null })} />
                    <span className="field-hint">{t("conn.webSignedOutHint")}</span></label>
                </div>
              </>}
              <label className="field"><span className="field-label">{t("conn.allowedPaths")}</span>
                <textarea className="mono" rows={2} defaultValue={(form.allowed_paths ?? []).join("\n")} key={`${sel}-paths`}
                  placeholder={t("conn.allowedPathsPlaceholder")}
                  onBlur={(e) => set({ allowed_paths: e.target.value.split("\n").map((s) => s.trim()).filter(Boolean) })} />
                <span className="field-hint">{t("conn.allowedPathsHint")}</span></label>

              <h3>{t("conn.instructions")}</h3>
              <p className="muted small">{t("conn.instructionsHint")}</p>
              <div className="field">
                <textarea className="conn-instructions" rows={10} value={form.instructions ?? ""}
                  aria-label={t("conn.instructions")} placeholder={t("conn.instructionsPlaceholder")}
                  onChange={(e) => set({ instructions: e.target.value })} />
                <div className="row">
                  {!form.instructions?.trim() && (
                    <button type="button" className="small" onClick={() => set({ instructions: t("conn.instructionsOutline") })}>
                      {t("conn.instructionsInsertOutline")}</button>)}
                  <span className="muted small">{t("conn.instructionsChars", { n: (form.instructions ?? "").length })}</span>
                </div>
              </div>

              <h3>{t("conn.actions")}</h3>
              <p className="muted small">{t("conn.actionsHint")}</p>
              {Object.keys(saved?.actions ?? {}).length > 0 && (
                <ul className="conn-actions">
                  {Object.entries(saved!.actions!).map(([n, a]) => (
                    <li key={n}><code>{n}</code>
                      <span className={`badge${a.access === "write" ? " warn" : ""}`}>{t(a.access === "write" ? "conn.accessWrite" : "conn.accessRead")}</span>
                      {a.path && <code className="muted">{a.method ?? "GET"} {a.path}</code>}
                      <span className="muted small">{a.description}</span>
                      {a.access !== "write" && (
                        <button className="small" disabled={busy} title={t("conn.actionTestHint")}
                          onClick={() => testAction(n)}>{t("conn.actionTest")}</button>)}
                      {lastTests[n] && tested?.action !== n && (
                        <button className="small link" onClick={() => showTest(n, lastTests[n])}>
                          {t(lastTests[n].ok ? "conn.lastTestOk" : "conn.lastTestFailed")}</button>)}</li>))}
                </ul>)}
              {tested && saved?.actions?.[tested.action] && (
                <div className={`conn-test-out${tested.ok ? "" : " bad"}`}>
                  <b>{t(tested.ok ? "conn.actionTestOk" : "conn.actionTestFailed", { name: tested.action })}</b>
                  {tested.ts && <span className="muted small"> · {new Date(tested.ts * 1000).toLocaleString(LANGS[lang].html)}
                    {Object.keys(tested.params).length > 0 && ` · ${Object.entries(tested.params).map(([k, v]) => `${k}=${v}`).join(", ")}`}</span>}
                  <button className="small link" onClick={() => setTested(null)}>{t("conn.close")}</button>
                  <pre>{tested.text}</pre>
                  {!tested.ok && (tested.blocked?.length ?? 0) > 0 && saved?.type === "website" && <>
                    <span className="field-hint">{t("conn.allowPostHint")}</span>
                    <div className="row"><button className="small primary" disabled={busy} onClick={allowPostAndRetest}>
                      {t("conn.allowPostRetest")}</button></div>
                  </>}
                </div>)}
              <label className="check">
                <input type="checkbox" checked={!!form.actions_only} onChange={(e) => set({ actions_only: e.target.checked })} />
                {t("conn.actionsOnly")}
              </label>
              <div className="field">
                <textarea className="conn-instructions" rows={10} value={form.actions_yaml ?? ""} spellCheck={false}
                  aria-label={t("conn.actions")} placeholder={t("conn.actionsPlaceholder")}
                  onChange={(e) => set({ actions_yaml: e.target.value })} />
                {!form.actions_yaml?.trim() && (
                  <div className="row"><button type="button" className="small"
                    onClick={() => set({ actions_yaml: form.type === "website" ? WEB_ACTIONS_EXAMPLE : API_ACTIONS_EXAMPLE })}>
                    {t("conn.actionsInsertExample")}</button></div>)}
              </div>
              {form.type === "website" && (
                <div className="secret-box">
                  <b>{t("conn.record")}</b>
                  <span className="field-hint">{t("conn.recordHint")}</span>
                  {isNew ? <span className="field-hint">{t("conn.signInSaveFirst")}</span>
                    : !recHere ? (
                      <div className="row">
                        <button className="small primary" disabled={busy || !!saved?.web_session?.signing_in} onClick={startRecord}>
                          {t("conn.recordBtn")}</button>
                      </div>)
                    : <>
                      <span className="badge">{t(recHere.recording ? "conn.recording" : "conn.recordDone", { n: recHere.steps.length })}</span>
                      {recHere.steps.length > 0 && (
                        <ol className="rec-steps">
                          {recHere.steps.map((s, i) => <li key={i}><code>{stepLine(s)}</code></li>)}
                        </ol>)}
                      {Object.keys(recHere.typed ?? {}).length > 0 && <>
                        <span className="field-label">{t("conn.recordTyped")}</span>
                        <span className="field-hint">{t("conn.recordTypedHint")}</span>
                        <ul className="rec-typed">
                          {Object.entries(recHere.typed ?? {}).map(([p, x]) => (
                            <li key={p}>
                              <code>{p}</code>
                              <span className="muted small">{x.label}: «{x.value}»</span>
                              <label className="check">
                                <input type="checkbox" checked={fixed.has(p)} onChange={(e) => setFixed((old) => {
                                  const n = new Set(old);
                                  if (e.target.checked) n.add(p); else n.delete(p);
                                  return n;
                                })} />
                                {t("conn.recordFixed")}
                              </label>
                            </li>))}
                        </ul>
                      </>}
                      {recHere.warnings.map((w, i) => <span key={i} className="field-hint warn-text">{localizeServer(w, w.msg, lang)}</span>)}
                      {recHere.posted && !recHere.recording && <>
                        <label className="check">
                          <input type="checkbox" checked={allowPost} onChange={(e) => setAllowPost(e.target.checked)} />
                          {t("conn.recordAllowPost")}
                        </label>
                        <span className="field-hint">{t("conn.allowPostHint")}</span>
                      </>}
                      {recHere.problems.map((p, i) => <span key={i} className="field-hint warn-text">{p}</span>)}
                      {recHere.error && <span className="msg bad">{recHere.error}</span>}
                      <div className="row">
                        {recHere.recording
                          ? <button className="small" disabled={busy} onClick={stopRecord}>{t("conn.recordStop")}</button>
                          : <>
                            {recHere.steps.length > 0 && (
                              <button className="small primary" disabled={busy} onClick={addRecorded}>{t("conn.recordAdd")}</button>)}
                            <button className="small" disabled={busy} onClick={discardRecord}>{t("conn.recordDiscard")}</button>
                          </>}
                      </div>
                    </>}
                </div>)}

              <h3>{t("conn.auth")}</h3>
              <label className="field"><span className="field-label">{t("conn.method")}</span>
                <select value={form.auth.method} onChange={(e) => setAuth({ method: e.target.value,
                  // these two only exist for sign-in on a page; the server refuses them otherwise
                  ...(e.target.value !== "oauth_browser" ? { token_in: "header", exchange: null } : {}) })}>
                  {tp.methods.map((x) => <option key={x} value={x}>{tOr(`conn.methodLabel.${x}`, meta.methods[x].label)}</option>)}</select></label>
              {method === "api_key" && (
                <label className="field"><span className="field-label">{t("conn.header")}</span>
                  <input value={form.auth.header ?? "X-API-Key"} onChange={(e) => setAuth({ header: e.target.value })} /></label>)}
              {method === "basic" && (
                <label className="field"><span className="field-label">{form.type === "atlassian" ? t("conn.email") : t("conn.username")}</span>
                  <input value={form.auth.username ?? ""} onChange={(e) => setAuth({ username: e.target.value })} />
                  {form.type === "atlassian" && <span className="field-hint">{t("conn.atlassianHint")}</span>}</label>)}
              {method === "client_credentials" && (
                <div className="grid2">
                  {generic
                    ? <label className="field"><span className="field-label">{t("conn.tokenUrl")}</span>
                      <input value={form.auth.token_url ?? ""} placeholder="https://…/oauth2/token" onChange={(e) => setAuth({ token_url: e.target.value })} /></label>
                    : <label className="field"><span className="field-label">Tenant ID</span>
                      <input value={form.auth.tenant_id ?? ""} onChange={(e) => setAuth({ tenant_id: e.target.value })} /></label>}
                  <label className="field"><span className="field-label">Client ID</span>
                    <input value={form.auth.client_id ?? ""} onChange={(e) => setAuth({ client_id: e.target.value })} /></label>
                </div>)}
              {(method === "client_credentials" || method === "azure_cli" || method === "managed_identity") && (
                <label className="field"><span className="field-label">Scope</span>
                  <input value={form.auth.scope ?? ""} placeholder={t("conn.scopeDefault")} onChange={(e) => setAuth({ scope: e.target.value || null })} /></label>)}
              {(method === "azure_cli" || method === "gh_cli") && (
                <p className="muted small">{t("conn.cliSession", { cmd: method === "azure_cli" ? "az login" : "gh auth login" })}</p>)}
              {method === "managed_identity" && <>
                <label className="field"><span className="field-label">{t("conn.miClientId")}</span>
                  <input value={form.auth.client_id ?? ""} onChange={(e) => setAuth({ client_id: e.target.value || null })} /></label>
                <p className="muted small">{t("conn.miHint")}</p>
              </>}

              {method === "oauth_browser" && <>
                <div className="grid2">
                  <label className="field"><span className="field-label">{t("conn.clientIdOauth")}</span>
                    <input value={form.auth.client_id ?? ""} onChange={(e) => setAuth({ client_id: e.target.value })} /></label>
                  {ENTRA_TYPES.has(form.type) && (
                    <label className="field"><span className="field-label">Tenant ID</span>
                      <input value={form.auth.tenant_id ?? ""} placeholder={t("conn.tenantOptional")} onChange={(e) => setAuth({ tenant_id: e.target.value || null })} /></label>)}
                </div>
                <div className="grid2">
                  <label className="field"><span className="field-label">{t("conn.authorizeUrl")}</span>
                    <input value={form.auth.authorize_url ?? ""} placeholder={tp.oauth?.authorize_url ?? "https://…/authorize"} onChange={(e) => setAuth({ authorize_url: e.target.value || null })} /></label>
                  <label className="field"><span className="field-label">{t("conn.tokenUrl")}</span>
                    <input value={form.auth.token_url ?? ""} placeholder={tp.oauth?.token_url ?? "https://…/token"} onChange={(e) => setAuth({ token_url: e.target.value || null })} /></label>
                </div>
                <label className="field"><span className="field-label">Scope</span>
                  <input value={form.auth.scope ?? ""} placeholder={tp.oauth?.scope ?? ""} onChange={(e) => setAuth({ scope: e.target.value || null })} /></label>
                {generic && <>
                  <div className="grid2">
                    <label className="field"><span className="field-label">{t("conn.tokenIn")}</span>
                      <select value={form.auth.token_in ?? "header"} onChange={(e) => setAuth({ token_in: e.target.value as Auth["token_in"] })}>
                        <option value="header">{t("conn.tokenInHeader")}</option>
                        <option value="query">{t("conn.tokenInQuery")}</option>
                      </select></label>
                    <label className="field"><span className="field-label">{t("conn.tokenAuth")}</span>
                      <select value={form.auth.token_auth ?? "body"} onChange={(e) => setAuth({ token_auth: e.target.value as Auth["token_auth"] })}>
                        <option value="body">{t("conn.tokenAuthBody")}</option>
                        <option value="basic">{t("conn.tokenAuthBasic")}</option>
                      </select></label>
                  </div>
                  <label className="field"><span className="field-label">{t("conn.extraParams")}</span>
                    <textarea className="mono" rows={2} defaultValue={lines(form.auth.authorize_params)} key={`${sel}-${form.auth.authorize_url}-params`}
                      onBlur={(e) => setAuth({ authorize_params: unlines(e.target.value) })} />
                    <span className="field-hint">{t("conn.extraParamsHint")}</span></label>
                  {form.auth.exchange && <p className="muted small">{t("conn.exchange", { grant: form.auth.exchange.grant_type, url: form.auth.exchange.url })}</p>}
                </>}
                <label className="field"><span className="field-label">{t("conn.redirectUri")}</span>
                  <code className="redirect">{meta.oauth_redirect_uri}</code>
                  <span className="field-hint">{t("conn.redirectHint")}</span></label>
              </>}

              {m.secret && (
                <div className="secret-box">
                  <b>{t("conn.secret", { name: m.secret })}</b>{" "}
                  {isNew ? <span className="muted small">{t("conn.saveFirst")}</span>
                    : savedSecret?.set ? <span className="badge">{t("conn.secretSet", { source: savedSecret.source ?? "" })}</span>
                      : m.optional ? <span className="badge">{t("conn.secretOptional")}</span>
                        : <span className="badge warn">{t("conn.secretMissing")}</span>}
                  {m.optional && <span className="field-hint">{t("conn.clientSecretHint")}</span>}
                  {!isNew && <>
                    <div className="row">
                      <input type="password" autoComplete="new-password" placeholder={t("conn.secretValue")} value={secret} onChange={(e) => setSecret(e.target.value)} />
                      <button className="small" disabled={!secret || busy} onClick={() => saveSecret(false)}>{t("conn.save")}</button>
                    </div>
                    <div className="row">
                      <input placeholder={t("conn.envVar")} value={envVar} onChange={(e) => setEnvVar(e.target.value.toUpperCase())} />
                      <button className="small" disabled={!/^[A-Z_][A-Z0-9_]*$/.test(envVar) || busy} onClick={() => saveSecret(true)}>{t("conn.use")}</button>
                    </div>
                    <span className="field-hint">{t("conn.secretNever")}</span>
                  </>}
                </div>
              )}

              {method === "oauth_browser" && (
                <div className="secret-box">
                  <b>{t("conn.signin")}</b>{" "}
                  {signIn?.signed_in
                    ? <span className="badge">{signIn.expires_at ? t("conn.signedInUntil", { date: fmtDate(signIn.expires_at) }) : t("conn.signedIn")}
                      {signIn.refreshable ? ` · ${t("conn.renews")}` : ""}</span>
                    : <span className="badge warn">{t("conn.notSignedIn")}</span>}
                  {isNew || saved?.auth.method !== "oauth_browser"
                    ? <span className="field-hint">{t("conn.signInSaveFirst")}</span>
                    : <div className="row">
                      <button className="small primary" disabled={busy} onClick={startSignIn}>{t("conn.signInBtn")}</button>
                      {signIn?.signed_in && <button className="small danger" disabled={busy} onClick={signOut}>{t("conn.signOut")}</button>}
                    </div>}
                </div>
              )}

              {form.type === "website" && (
                <div className="secret-box">
                  <b>{t("conn.webSession")}</b>{" "}
                  {saved?.web_session?.signing_in ? <span className="badge">{t("conn.webSigningIn")}</span>
                    : saved?.web_session?.profile ? <span className="badge">{t("conn.webHasSession")}</span>
                      : <span className="badge warn">{t("conn.notSignedIn")}</span>}
                  <span className="field-hint">{t("conn.webSessionHint")}</span>
                  {isNew ? <span className="field-hint">{t("conn.signInSaveFirst")}</span>
                    : <div className="row">
                      <button className="small primary" disabled={busy || !!saved?.web_session?.signing_in} onClick={webSignIn}>{t("conn.signInBtn")}</button>
                      {saved?.web_session?.signing_in && <button className="small" onClick={() => reload(sel)}>{t("conn.webRefresh")}</button>}
                      {saved?.web_session?.profile && <button className="small danger" disabled={busy} onClick={webSignOut}>{t("conn.signOut")}</button>}
                    </div>}
                </div>
              )}

              <div className="row actions">
                <button className="primary" disabled={busy} onClick={save}>{t("conn.save")}</button>
                <button disabled={busy || isNew} onClick={test}>{t("conn.test")}</button>
                {!isNew && sel && <button disabled={busy} title={t("copy.connectorTitle")}
                  onClick={() => acts.copyTo({ kind: "connector", name: sel }, sel)}>{t("copy.button")}</button>}
                {!isNew && sel && <button disabled={busy} title={t("copy.moveConnectorTitle")}
                  onClick={() => acts.copyTo({ kind: "connector", name: sel }, sel, "move").then((r) => r && reload())}>{t("copy.moveButton")}</button>}
                {!isNew && <button className="danger" disabled={busy} onClick={remove}>{t("conn.delete")}</button>}
                {msg && <span className={`msg${msg.bad ? " bad" : ""}`} title={msg.text}>{msg.text}</span>}
              </div>
            </div>
          ) : (
            <div className="conns-form muted">
              <p>{t("conn.empty")} <code>connections.http_get</code> {t("conn.emptyEnd")}</p>
              {msg && <p className={`msg${msg.bad ? " bad" : ""}`}>{msg.text}</p>}
            </div>
          )}
        </div>
      </div>
    </div>
  );
}

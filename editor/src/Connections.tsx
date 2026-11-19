import { useEffect, useState } from "react";
import { useDialog } from "./Dialog";
import { LANGS, useI18n } from "./i18n";

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
  type: string; description?: string; environment?: string; base_url?: string | null; organization?: string | null;
  test_path?: string | null; allowed_paths?: string[]; auth: Auth; secrets?: Record<string, string>; access?: string;
  secret?: { field: string | null; source: string | null; set: boolean; optional?: boolean };
  oauth?: OAuthStatus | null;
}
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
}

async function req<T>(url: string, method: string, body?: unknown): Promise<T> {
  const r = await fetch(url, { method, headers: MUTATE, body: body === undefined ? undefined : JSON.stringify(body) });
  const data = await r.json().catch(() => ({}));
  if (!r.ok) {
    const d = (data as { detail?: unknown }).detail;
    throw new Error(Array.isArray(d) ? d.map((x: { msg: string }) => x.msg).join("; ") : String(d ?? r.statusText));
  }
  return data as T;
}

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
  const [busy, setBusy] = useState(false);

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
    try { await fn(); } catch (e) { setMsg({ text: String((e as Error).message), bad: true }); }
    finally { setBusy(false); }
  };
  const save = () => run(async () => {
    if (!form) return;
    if (!SLUG.test(name)) throw new Error(t("conn.slugError"));
    if (isNew && meta.connections[name]) throw new Error(t("conn.exists"));
    const body = { ...form, secrets: undefined, secret: undefined, oauth: undefined };
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
          <button className="small" onClick={onClose}>{t("conn.close")}</button>
        </div>
        <div className="conns-body">
          <div className="conns-list">
            {Object.entries(meta.connections).map(([n, c]) => (
              <button key={n} className={`conn-item${sel === n ? " on" : ""}`}
                onClick={() => { setSel(n); setName(n); setForm(structuredClone(c)); setMsg(null); }}>
                <b>{n}</b>
                <span className="muted small">{meta.types[c.type]?.label} · {c.environment ?? "dev"}
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
                <label className="field"><span className="field-label">{t("conn.testPath")}</span>
                  <input value={form.test_path ?? ""} placeholder={tp.test_path} onChange={(e) => set({ test_path: e.target.value || null })} /></label>
              </div>
              <label className="field"><span className="field-label">{t("conn.allowedPaths")}</span>
                <textarea className="mono" rows={2} defaultValue={(form.allowed_paths ?? []).join("\n")} key={`${sel}-paths`}
                  placeholder={t("conn.allowedPathsPlaceholder")}
                  onBlur={(e) => set({ allowed_paths: e.target.value.split("\n").map((s) => s.trim()).filter(Boolean) })} />
                <span className="field-hint">{t("conn.allowedPathsHint")}</span></label>

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

              <div className="row actions">
                <button className="primary" disabled={busy} onClick={save}>{t("conn.save")}</button>
                <button disabled={busy || isNew} onClick={test}>{t("conn.test")}</button>
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

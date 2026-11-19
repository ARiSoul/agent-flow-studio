import { useEffect, useState } from "react";
import { useI18n } from "./i18n";

const MUTATE = { "Content-Type": "application/json", "X-Flow-Editor": "1" };
const SLUG = /^[a-z][a-z0-9_]{0,39}$/;

interface Auth { method: string; header?: string; username?: string | null; tenant_id?: string | null; client_id?: string | null; token_url?: string | null; scope?: string | null }
interface Conn {
  type: string; description?: string; environment?: string; base_url?: string | null; organization?: string | null;
  test_path?: string | null; allowed_paths?: string[]; auth: Auth; secrets?: Record<string, string>; access?: string;
  secret?: { field: string | null; source: string | null; set: boolean };
}
interface Meta {
  types: Record<string, { label: string; methods: string[]; base_url: string | null; test_path: string }>;
  methods: Record<string, { label: string; secret: string | null }>;
  environments: string[];
  file: string;
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

export function Connections({ onClose }: { onClose: () => void }) {
  const { t, tOr } = useI18n();
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

  if (!meta) return <div className="modal"><div className="modal-box">{t("conn.loading")}</div></div>;
  const isNew = sel === "";
  const set = (p: Partial<Conn>) => setForm((f) => (f ? { ...f, ...p } : f));
  const setAuth = (p: Partial<Auth>) => setForm((f) => (f ? { ...f, auth: { ...f.auth, ...p } } : f));
  const tp = form ? meta.types[form.type] : null;
  const m = form ? meta.methods[form.auth.method] : null;
  const savedSecret = !isNew && sel ? meta.connections[sel]?.secret : undefined;

  const run = async (fn: () => Promise<void>) => {
    setBusy(true); setMsg(null);
    try { await fn(); } catch (e) { setMsg({ text: String((e as Error).message), bad: true }); }
    finally { setBusy(false); }
  };
  const save = () => run(async () => {
    if (!form) return;
    if (!SLUG.test(name)) throw new Error(t("conn.slugError"));
    if (isNew && meta.connections[name]) throw new Error(t("conn.exists"));
    const body = { ...form, secrets: undefined, secret: undefined };
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
    if (!sel || !confirm(t("conn.confirmDelete", { name: sel }))) return;
    await req(`/api/connections/${sel}`, "DELETE");
    setSel(null); setForm(null);
    await reload(null);
  });

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
                <span className="muted small">{meta.types[c.type]?.label} · {c.environment ?? "dev"}{c.secret && !c.secret.set ? t("conn.noSecret") : ""}</span>
              </button>
            ))}
            <div className="new-conn">
              {Object.keys(meta.types).map((tp) => (
                <button key={tp} className="small" onClick={() => { setSel(""); setName(""); setForm(blank(tp, meta)); setMsg(null); }}>
                  + {meta.types[tp].label}</button>
              ))}
            </div>
          </div>

          {form && tp && m ? (
            <div className="conns-form">
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
                <select value={form.auth.method} onChange={(e) => setAuth({ method: e.target.value })}>
                  {tp.methods.map((x) => <option key={x} value={x}>{tOr(`conn.methodLabel.${x}`, meta.methods[x].label)}</option>)}</select></label>
              {form.auth.method === "api_key" && (
                <label className="field"><span className="field-label">{t("conn.header")}</span>
                  <input value={form.auth.header ?? "X-API-Key"} onChange={(e) => setAuth({ header: e.target.value })} /></label>)}
              {form.auth.method === "basic" && (
                <label className="field"><span className="field-label">{t("conn.username")}</span>
                  <input value={form.auth.username ?? ""} onChange={(e) => setAuth({ username: e.target.value })} /></label>)}
              {form.auth.method === "client_credentials" && (
                <div className="grid2">
                  {form.type === "http_api"
                    ? <label className="field"><span className="field-label">{t("conn.tokenUrl")}</span>
                      <input value={form.auth.token_url ?? ""} placeholder="https://…/oauth2/token" onChange={(e) => setAuth({ token_url: e.target.value })} /></label>
                    : <label className="field"><span className="field-label">Tenant ID</span>
                      <input value={form.auth.tenant_id ?? ""} onChange={(e) => setAuth({ tenant_id: e.target.value })} /></label>}
                  <label className="field"><span className="field-label">Client ID</span>
                    <input value={form.auth.client_id ?? ""} onChange={(e) => setAuth({ client_id: e.target.value })} /></label>
                </div>)}
              {(form.auth.method === "client_credentials" || form.auth.method === "azure_cli") && (
                <label className="field"><span className="field-label">Scope</span>
                  <input value={form.auth.scope ?? ""} placeholder={t("conn.scopeDefault")} onChange={(e) => setAuth({ scope: e.target.value || null })} /></label>)}
              {(form.auth.method === "azure_cli" || form.auth.method === "gh_cli") && (
                <p className="muted small">{t("conn.cliSession", { cmd: form.auth.method === "azure_cli" ? "az login" : "gh auth login" })}</p>)}

              {m.secret && (
                <div className="secret-box">
                  <b>{t("conn.secret", { name: m.secret })}</b>{" "}
                  {isNew ? <span className="muted small">{t("conn.saveFirst")}</span>
                    : savedSecret?.set ? <span className="badge">{t("conn.secretSet", { source: savedSecret.source ?? "" })}</span>
                      : <span className="badge warn">{t("conn.secretMissing")}</span>}
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

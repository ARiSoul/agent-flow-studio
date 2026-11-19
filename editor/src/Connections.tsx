import { useEffect, useState } from "react";

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
  const [meta, setMeta] = useState<Meta | null>(null);
  const [sel, setSel] = useState<string | null>(null); // nome, ou "" para novo
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

  if (!meta) return <div className="modal"><div className="modal-box">A carregar…</div></div>;
  const isNew = sel === "";
  const set = (p: Partial<Conn>) => setForm((f) => (f ? { ...f, ...p } : f));
  const setAuth = (p: Partial<Auth>) => setForm((f) => (f ? { ...f, auth: { ...f.auth, ...p } } : f));
  const t = form ? meta.types[form.type] : null;
  const m = form ? meta.methods[form.auth.method] : null;
  const savedSecret = !isNew && sel ? meta.connections[sel]?.secret : undefined;

  const run = async (fn: () => Promise<void>) => {
    setBusy(true); setMsg(null);
    try { await fn(); } catch (e) { setMsg({ text: String((e as Error).message), bad: true }); }
    finally { setBusy(false); }
  };
  const save = () => run(async () => {
    if (!form) return;
    if (!SLUG.test(name)) throw new Error("Nome: minúsculas, dígitos e _ (começa por letra)");
    if (isNew && meta.connections[name]) throw new Error("Já existe um conector com esse nome");
    const body = { ...form, secrets: undefined, secret: undefined };
    await req(`/api/connections/${name}`, "PUT", body);
    await reload(name);
    setMsg({ text: "Guardado." });
  });
  const saveSecret = (useEnv: boolean) => run(async () => {
    if (!sel) return;
    await req(`/api/connections/${sel}/secret`, "PUT", useEnv ? { env: envVar } : { value: secret });
    setSecret(""); setEnvVar("");
    await reload(sel);
    setMsg({ text: useEnv ? "Segredo ligado à variável de ambiente." : "Segredo guardado no Gestor de Credenciais." });
  });
  const test = () => run(async () => {
    if (!sel) return;
    const r = await req<{ ok: boolean; status?: number; elapsed_ms?: number; detail: string }>(`/api/connections/${sel}/test`, "POST");
    setMsg({ text: `${r.ok ? "✓" : "✗"} ${r.status ? `HTTP ${r.status} · ` : ""}${r.elapsed_ms ? `${r.elapsed_ms} ms · ` : ""}${r.detail}`, bad: !r.ok });
  });
  const remove = () => run(async () => {
    if (!sel || !confirm(`Apagar o conector "${sel}" e o seu segredo guardado?`)) return;
    await req(`/api/connections/${sel}`, "DELETE");
    setSel(null); setForm(null);
    await reload(null);
  });

  return (
    <div className="modal" onMouseDown={(e) => e.target === e.currentTarget && onClose()}>
      <div className="modal-box conns">
        <div className="panel-head">
          <h2>Conectores</h2>
          <span className="muted small">{meta.file} · segredos no Gestor de Credenciais ou em variáveis de ambiente</span>
          <button className="small" onClick={onClose}>Fechar</button>
        </div>
        <div className="conns-body">
          <div className="conns-list">
            {Object.entries(meta.connections).map(([n, c]) => (
              <button key={n} className={`conn-item${sel === n ? " on" : ""}`}
                onClick={() => { setSel(n); setName(n); setForm(structuredClone(c)); setMsg(null); }}>
                <b>{n}</b>
                <span className="muted small">{meta.types[c.type]?.label} · {c.environment ?? "dev"}{c.secret && !c.secret.set ? " · sem segredo" : ""}</span>
              </button>
            ))}
            <div className="new-conn">
              {Object.keys(meta.types).map((tp) => (
                <button key={tp} className="small" onClick={() => { setSel(""); setName(""); setForm(blank(tp, meta)); setMsg(null); }}>
                  + {meta.types[tp].label}</button>
              ))}
            </div>
          </div>

          {form && t && m ? (
            <div className="conns-form">
              <div className="grid2">
                <label className="field"><span className="field-label">Nome</span>
                  <input value={name} disabled={!isNew} placeholder="ex.: devops_empresa" onChange={(e) => setName(e.target.value)} /></label>
                <label className="field"><span className="field-label">Ambiente</span>
                  <select value={form.environment ?? "dev"} onChange={(e) => set({ environment: e.target.value })}>
                    {meta.environments.map((x) => <option key={x}>{x}</option>)}</select></label>
              </div>
              <label className="field"><span className="field-label">Acesso</span>
                <select value={form.access ?? "read"} onChange={(e) => set({ access: e.target.value })}>
                  <option value="read">só leitura (GET)</option>
                  <option value="write">leitura e escrita (POST/PUT/PATCH/DELETE)</option>
                </select>
                {form.access === "write" && <span className="field-hint">As escritas seguem a autonomia do flow/agente; usa credenciais com o mínimo de permissões.</span>}</label>
              <label className="field"><span className="field-label">Descrição</span>
                <input value={form.description ?? ""} placeholder="Para que serve (aparece no prompt do agente)" onChange={(e) => set({ description: e.target.value })} /></label>
              {form.type === "azure_devops" && (
                <label className="field"><span className="field-label">Organização</span>
                  <input value={form.organization ?? ""} placeholder="dev.azure.com/<organização>" onChange={(e) => set({ organization: e.target.value || null })} /></label>
              )}
              <div className="grid2">
                <label className="field"><span className="field-label">URL base</span>
                  <input value={form.base_url ?? ""} placeholder={t.base_url ?? "https://api.exemplo.com"} onChange={(e) => set({ base_url: e.target.value || null })} /></label>
                <label className="field"><span className="field-label">Caminho de teste</span>
                  <input value={form.test_path ?? ""} placeholder={t.test_path} onChange={(e) => set({ test_path: e.target.value || null })} /></label>
              </div>
              <label className="field"><span className="field-label">Caminhos permitidos</span>
                <textarea className="mono" rows={2} defaultValue={(form.allowed_paths ?? []).join("\n")} key={`${sel}-paths`}
                  placeholder="Um prefixo por linha (ex.: /_apis/projects). Vazio = todos."
                  onBlur={(e) => set({ allowed_paths: e.target.value.split("\n").map((s) => s.trim()).filter(Boolean) })} />
                <span className="field-hint">Aplica-se a leituras e escritas.</span></label>

              <h3>Autenticação</h3>
              <label className="field"><span className="field-label">Método</span>
                <select value={form.auth.method} onChange={(e) => setAuth({ method: e.target.value })}>
                  {t.methods.map((x) => <option key={x} value={x}>{meta.methods[x].label}</option>)}</select></label>
              {form.auth.method === "api_key" && (
                <label className="field"><span className="field-label">Cabeçalho</span>
                  <input value={form.auth.header ?? "X-API-Key"} onChange={(e) => setAuth({ header: e.target.value })} /></label>)}
              {form.auth.method === "basic" && (
                <label className="field"><span className="field-label">Utilizador</span>
                  <input value={form.auth.username ?? ""} onChange={(e) => setAuth({ username: e.target.value })} /></label>)}
              {form.auth.method === "client_credentials" && (
                <div className="grid2">
                  {form.type === "http_api"
                    ? <label className="field"><span className="field-label">URL do token</span>
                      <input value={form.auth.token_url ?? ""} placeholder="https://…/oauth2/token" onChange={(e) => setAuth({ token_url: e.target.value })} /></label>
                    : <label className="field"><span className="field-label">Tenant ID</span>
                      <input value={form.auth.tenant_id ?? ""} onChange={(e) => setAuth({ tenant_id: e.target.value })} /></label>}
                  <label className="field"><span className="field-label">Client ID</span>
                    <input value={form.auth.client_id ?? ""} onChange={(e) => setAuth({ client_id: e.target.value })} /></label>
                </div>)}
              {(form.auth.method === "client_credentials" || form.auth.method === "azure_cli") && (
                <label className="field"><span className="field-label">Scope</span>
                  <input value={form.auth.scope ?? ""} placeholder="por defeito o do tipo" onChange={(e) => setAuth({ scope: e.target.value || null })} /></label>)}
              {(form.auth.method === "azure_cli" || form.auth.method === "gh_cli") && (
                <p className="muted small">Usa a sessão já iniciada nesta máquina ({form.auth.method === "azure_cli" ? "az login" : "gh auth login"}). Nada é guardado.</p>)}

              {m.secret && (
                <div className="secret-box">
                  <b>Segredo: {m.secret}</b>{" "}
                  {isNew ? <span className="muted small">guarda o conector primeiro</span>
                    : savedSecret?.set ? <span className="badge">definido · {savedSecret.source}</span>
                      : <span className="badge warn">em falta</span>}
                  {!isNew && <>
                    <div className="row">
                      <input type="password" autoComplete="new-password" placeholder="Valor (vai para o Gestor de Credenciais)" value={secret} onChange={(e) => setSecret(e.target.value)} />
                      <button className="small" disabled={!secret || busy} onClick={() => saveSecret(false)}>Guardar</button>
                    </div>
                    <div className="row">
                      <input placeholder="ou nome de variável de ambiente (ex.: DEVOPS_PAT)" value={envVar} onChange={(e) => setEnvVar(e.target.value.toUpperCase())} />
                      <button className="small" disabled={!/^[A-Z_][A-Z0-9_]*$/.test(envVar) || busy} onClick={() => saveSecret(true)}>Usar</button>
                    </div>
                    <span className="field-hint">O valor nunca é mostrado nem escrito em ficheiros.</span>
                  </>}
                </div>
              )}

              <div className="row actions">
                <button className="primary" disabled={busy} onClick={save}>Guardar</button>
                <button disabled={busy || isNew} onClick={test}>Testar ligação</button>
                {!isNew && <button className="danger" disabled={busy} onClick={remove}>Apagar</button>}
                {msg && <span className={`msg${msg.bad ? " bad" : ""}`} title={msg.text}>{msg.text}</span>}
              </div>
            </div>
          ) : (
            <div className="conns-form muted">
              <p>Escolhe um conector ou cria um novo. Os agentes usam-nos com a ferramenta <code>connections.http_get</code> (só leitura), e cada agente só acede aos conectores que lhe atribuíres no painel de propriedades.</p>
              {msg && <p className={`msg${msg.bad ? " bad" : ""}`}>{msg.text}</p>}
            </div>
          )}
        </div>
      </div>
    </div>
  );
}

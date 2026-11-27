import { useCallback, useEffect, useState } from "react";
import { errorText } from "./api";
import { useDialog } from "./Dialog";
import { useI18n } from "./i18n";
import type { MessageKey } from "./i18n/en";

/** Model providers of this machine (providers.yaml): where an agent's model runs. The file never
 * holds a key: a key goes to the system keyring, or is read from an environment variable. */

const MUTATE = { "Content-Type": "application/json", "X-Flow-Editor": "1" };
const NAME = /^[a-z][a-z0-9-]{0,39}$/;
const KINDS = ["gateway", "anthropic", "bedrock", "vertex", "foundry"] as const;
type Kind = typeof KINDS[number];

interface Prov {
  kind: Kind; title?: string | null; base_url?: string | null; key?: string | null;
  region?: string | null; project?: string | null; resource?: string | null; aws_profile?: string | null;
  models: string[]; small_model?: string | null; priced?: boolean | null;
  location?: string; key_status?: "none" | "set" | "missing";
}
interface Meta { file: string; kinds: string[]; providers: Record<string, Prov>; error?: string }
interface Check { ok: boolean | null; message: string; models?: string[] }

// which fields each kind uses (the server checks the same: providers.Provider.check)
const FIELDS: Record<Kind, (keyof Prov)[]> = {
  gateway: ["base_url"],
  anthropic: ["base_url"],
  bedrock: ["region", "aws_profile", "base_url"],
  vertex: ["region", "project", "base_url"],
  foundry: ["resource", "base_url"],
};
const USES_KEY: Record<Kind, boolean> = { gateway: true, anthropic: true, foundry: true, bedrock: false, vertex: false };
const PLACEHOLDER: Partial<Record<keyof Prov, string>> = {
  base_url: "http://localhost:4000", region: "eu-west-1", project: "my-gcp-project",
  resource: "my-foundry-resource", aws_profile: "default",
};

async function req<T>(url: string, method: string, body?: unknown): Promise<T> {
  const r = await fetch(url, { method, headers: MUTATE, body: body === undefined ? undefined : JSON.stringify(body) });
  const data = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(String((data as { detail?: unknown }).detail ?? r.statusText));
  return data as T;
}

export function Providers({ onClose, focus }: { onClose: () => void; focus?: string | null }) {
  const { t } = useI18n();
  const tk = (k: string, v?: Record<string, string>) => t(k as MessageKey, v);  // a key built from data
  const dialog = useDialog();
  const [meta, setMeta] = useState<Meta | null>(null);
  const [sel, setSel] = useState<string | null>(null);  // "" = a new one
  const [name, setName] = useState("");
  const [form, setForm] = useState<Prov | null>(null);
  const [modelsText, setModelsText] = useState("");
  const [keyValue, setKeyValue] = useState("");
  const [keyEnv, setKeyEnv] = useState("");
  const [msg, setMsg] = useState<{ text: string; bad?: boolean } | null>(null);
  const [check, setCheck] = useState<Check | null>(null);
  const [busy, setBusy] = useState(false);

  const pick = useCallback((n: string, p: Prov) => {
    setSel(n); setName(n); setForm(structuredClone(p)); setModelsText(p.models.join("\n"));
    setKeyValue(""); setKeyEnv(p.key?.startsWith("${") ? p.key.slice(2, -1) : ""); setMsg(null); setCheck(null);
  }, []);
  const reload = useCallback(async (keep?: string | null) => {
    const m = await (await fetch("/api/providers")).json() as Meta;
    setMeta(m);
    const n = keep ?? null;
    if (n && m.providers[n]) pick(n, m.providers[n]);
    return m;
  }, [pick]);
  useEffect(() => { reload(focus ?? null).catch(() => undefined); }, [reload, focus]);
  useEffect(() => {
    const k = (e: KeyboardEvent) => e.key === "Escape" && onClose();
    window.addEventListener("keydown", k);
    return () => window.removeEventListener("keydown", k);
  }, [onClose]);

  if (!meta) return <div className="modal"><div className="modal-box">{t("conn.loading")}</div></div>;
  const isNew = sel === "";
  const shared = !!form?.location && form.location !== "local";
  const run = async (fn: () => Promise<void>) => {
    setBusy(true); setMsg(null);
    try { await fn(); } catch (e) { setMsg({ text: errorText(e, t), bad: true }); } finally { setBusy(false); }
  };
  const set = (patch: Partial<Prov>) => setForm((f) => f && { ...f, ...patch });
  const blank = (kind: Kind) => {
    setSel(""); setName(""); setForm({ kind, models: [] }); setModelsText(""); setKeyValue(""); setKeyEnv("");
    setMsg(null); setCheck(null);
  };
  const models = () => modelsText.split(/[\n,]/).map((x) => x.trim()).filter(Boolean);
  const body = (f: Prov): Prov => {
    const out: Prov = { ...f, models: models() };
    for (const k of Object.keys(out) as (keyof Prov)[]) if (out[k] === "") (out as unknown as Record<string, unknown>)[k] = null;
    if (!FIELDS[f.kind].includes("region")) out.region = null;
    if (!FIELDS[f.kind].includes("project")) out.project = null;
    if (!FIELDS[f.kind].includes("resource")) out.resource = null;
    if (!FIELDS[f.kind].includes("aws_profile")) out.aws_profile = null;
    if (!USES_KEY[f.kind]) out.key = null;
    delete out.location; delete out.key_status;
    return out;
  };
  const save = () => run(async () => {
    if (!form) return;
    const n = isNew ? name.trim() : sel!;
    if (!NAME.test(n)) throw new Error(t("prov.nameBad"));
    if (isNew && meta.providers[n]) throw new Error(t("prov.nameTaken", { name: n }));
    await req(`/api/providers/${n}`, "PUT", body(form));
    await reload(n);
    setMsg({ text: t("prov.saved") });
  });
  const saveKey = (how: "value" | "env") => run(async () => {
    if (!sel) return;
    const payload = how === "value" ? { value: keyValue } : { env: keyEnv.trim() };
    await req(`/api/providers/${sel}/key`, "PUT", payload);
    setKeyValue("");
    await reload(sel);
    setMsg({ text: how === "value" ? t("prov.keySaved") : t("prov.keyEnvSaved", { name: keyEnv.trim() }) });
  });
  const test = () => run(async () => {
    if (!sel) return;
    setCheck(await req<Check>(`/api/providers/${sel}/test`, "POST"));
  });
  const remove = () => run(async () => {
    if (!sel || !(await dialog.confirm({ title: t("prov.deleteTitle"), message: t("prov.confirmDelete", { name: sel }),
      ok: t("conn.delete"), danger: true }))) return;
    await req(`/api/providers/${sel}`, "DELETE");
    setSel(null); setForm(null);
    await reload(null);
  });
  const addModels = (ids: string[]) => {
    const have = new Set(models());
    setModelsText([...models(), ...ids.filter((x) => !have.has(x))].join("\n"));
  };
  const keyLabel = (p: Prov) => !USES_KEY[p.kind] ? t("prov.keyCloud")
    : p.key_status === "set" ? (p.key === "keyring" ? t("prov.keyInKeyring") : t("prov.keyInEnv", { name: p.key?.slice(2, -1) ?? "" }))
    : p.key_status === "missing" ? t("prov.keyMissing") : t("prov.keyNone");

  return (
    <div className="modal" onMouseDown={(e) => e.target === e.currentTarget && onClose()}>
      <div className="modal-box conns" role="dialog" aria-label={t("prov.title")}>
        <div className="panel-head">
          <h2>{t("prov.title")}</h2>
          <span className="muted small">{meta.file} · {t("prov.secretsWhere")}</span>
          <button className="small" onClick={onClose}>{t("conn.close")}</button>
        </div>
        {meta.error && <p className="field-err" style={{ padding: "0 16px" }}>{meta.error}</p>}
        <div className="conns-body">
          <div className="conns-list">
            {Object.entries(meta.providers).map(([n, p]) => (
              <button key={n} className={`conn-item${sel === n ? " on" : ""}`} onClick={() => pick(n, p)}>
                <b>{p.title || n}</b>
                <span className="muted small">{n} · {tk(`prov.kind.${p.kind}`)} · {t("prov.nModels", { n: String(p.models.length) })}
                  {p.key_status === "missing" ? ` · ${t("prov.keyMissingShort")}` : ""}
                  {p.location && p.location !== "local" ? ` · ${p.location}` : ""}</span>
              </button>
            ))}
            {Object.keys(meta.providers).length === 0 && <p className="muted small">{t("prov.none")}</p>}
            <div className="new-conn">
              {KINDS.map((k) => <button key={k} className="small" onClick={() => blank(k)} title={tk(`prov.kindHint.${k}`)}>
                + {tk(`prov.kind.${k}`)}</button>)}
            </div>
          </div>

          {form ? (
            <div className="conns-form">
              <p className="muted small">{tk(`prov.kindHint.${form.kind}`)}</p>
              {shared && <p className="warn-box small">{t("prov.shared", { where: form.location ?? "" })}</p>}
              <fieldset disabled={shared || busy} className="plain-set">
                <div className="grid2">
                  <label className="field"><span className="field-label">{t("prov.name")}</span>
                    <input value={name} disabled={!isNew} placeholder="litellm" onChange={(e) => setName(e.target.value)} />
                    <span className="field-hint">{t("prov.nameHint", { name: name || "litellm" })}</span></label>
                  <label className="field"><span className="field-label">{t("prov.titleField")}</span>
                    <input value={form.title ?? ""} placeholder={t("prov.titlePlaceholder")} onChange={(e) => set({ title: e.target.value || null })} /></label>
                </div>
                <label className="field"><span className="field-label">{t("prov.kind")}</span>
                  <select value={form.kind} onChange={(e) => set({ kind: e.target.value as Kind })}>
                    {KINDS.map((k) => <option key={k} value={k}>{tk(`prov.kind.${k}`)}</option>)}
                  </select></label>
                <div className="grid2">
                  {FIELDS[form.kind].map((f) => (
                    <label key={f} className="field"><span className="field-label">{tk(`prov.f.${f}`)}</span>
                      <input value={(form[f] as string | null | undefined) ?? ""} placeholder={PLACEHOLDER[f]}
                        onChange={(e) => set({ [f]: e.target.value || null } as Partial<Prov>)} />
                      <span className="field-hint">{tk(`prov.fh.${f}`)}</span></label>
                  ))}
                </div>
                <label className="field"><span className="field-label">{t("prov.models")}</span>
                  <textarea rows={4} className="mono" value={modelsText} placeholder={form.kind === "gateway" ? "gpt-5\ngemini-2.5-pro" : "claude-sonnet-4-6"}
                    onChange={(e) => setModelsText(e.target.value)} />
                  <span className="field-hint">{t("prov.modelsHint", { name: name || "…" })}</span></label>
                <div className="grid2">
                  <label className="field"><span className="field-label">{t("prov.smallModel")}</span>
                    <input className="mono" value={form.small_model ?? ""} onChange={(e) => set({ small_model: e.target.value || null })} />
                    <span className="field-hint">{t("prov.smallModelHint")}</span></label>
                  {form.kind === "gateway" && (
                    <label className="check"><input type="checkbox" checked={!!form.priced}
                      onChange={(e) => set({ priced: e.target.checked || null })} />
                      <span>{t("prov.priced")}</span>
                      <span className="field-hint">{t("prov.pricedHint")}</span></label>)}
                </div>
              </fieldset>
              <div className="row">
                {!shared && <button className="primary" disabled={busy} onClick={save}>{t("prov.save")}</button>}
                {!isNew && <button disabled={busy} onClick={test}>{t("prov.test")}</button>}
                <span className="grow" />
                {!isNew && !shared && <button className="link danger" disabled={busy} onClick={remove}>{t("conn.delete")}</button>}
              </div>
              {msg && <p className={msg.bad ? "field-err" : "muted small"} role="status">{msg.text}</p>}
              {check && (
                <div className={check.ok === false ? "warn-box small" : "muted small"} role="status">
                  {check.ok === true ? "✓ " : check.ok === false ? "✗ " : ""}{check.ok === null ? t("prov.testCloud") : check.message}
                  {check.models && check.models.length > 0 && <>
                    {" · "}{t("prov.testModels", { n: String(check.models.length) })}{" "}
                    <button className="link small" onClick={() => addModels(check.models!)}>{t("prov.addModels")}</button></>}
                </div>)}

              {!isNew && !shared && USES_KEY[form.kind] && (
                <div className="prov-key">
                  <h3>{t("prov.key")}</h3>
                  <p className="small">{keyLabel(form)}</p>
                  <div className="grid2">
                    <label className="field"><span className="field-label">{t("prov.keyValue")}</span>
                      <input type="password" autoComplete="off" value={keyValue} onChange={(e) => setKeyValue(e.target.value)} />
                      <span className="field-hint">{t("prov.keyValueHint")}</span>
                      <button className="small" disabled={busy || !keyValue} onClick={() => saveKey("value")}>{t("prov.keySave")}</button></label>
                    <label className="field"><span className="field-label">{t("prov.keyEnv")}</span>
                      <input className="mono" value={keyEnv} placeholder="LITELLM_KEY" onChange={(e) => setKeyEnv(e.target.value.toUpperCase())} />
                      <span className="field-hint">{t("prov.keyEnvHint")}</span>
                      <button className="small" disabled={busy || !/^[A-Z_][A-Z0-9_]*$/.test(keyEnv)} onClick={() => saveKey("env")}>{t("prov.keyUseEnv")}</button></label>
                  </div>
                  {form.kind === "foundry" && <p className="muted small">{t("prov.foundryNoKey")}</p>}
                </div>)}
              {!isNew && !USES_KEY[form.kind] && <p className="muted small">{t("prov.keyCloud")}</p>}
              {isNew && USES_KEY[form.kind] && <p className="muted small">{t("prov.keyAfterSave")}</p>}
            </div>
          ) : (
            <div className="conns-form"><p className="muted">{t("prov.intro")}</p>
              <ul className="small muted">
                <li>{t("prov.introRef")}</li>
                <li>{t("prov.introModes")}</li>
                <li>{t("prov.introSecrets")}</li>
              </ul></div>
          )}
        </div>
      </div>
    </div>
  );
}

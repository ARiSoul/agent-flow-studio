// The agent library window: list, edit (definition in YAML + prompt), duplicate, move between
// this machine / an extension / public, delete. No flow needed.
import { useEffect, useState } from "react";
import { api, errorText } from "./api";
import { useDialog } from "./Dialog";
import { useI18n } from "./i18n";
import { AgentIcon } from "./icons";
import type { LibraryAgent } from "./types";
import { useWorkspaceActions } from "./Workspaces";

const ID = /^[a-z][a-z0-9_]{0,39}$/;
const NEW_YAML = "description: What this agent does and when to use it.\ntools: []\ninputs: {}\noutputs: {}\n";

type Form = { id: string; title: string; location: string; yaml: string; prompt: string; isNew: boolean };

export function locLabel(t: ReturnType<typeof useI18n>["t"], loc: string) {
  return loc === "local" ? t("loc.local") : loc === "public" ? t("loc.public") : t("loc.extension", { name: loc });
}

export function LibraryManager({ library, locations, onChange, onClose }: {
  library: LibraryAgent[]; locations: string[]; onChange: () => Promise<void>; onClose: () => void;
}) {
  const { t } = useI18n();
  const dialog = useDialog();
  const [sel, setSel] = useState<string | null>(library[0]?.id ?? null);
  const acts = useWorkspaceActions(undefined, (text, bad) => setMsg({ text, bad }));
  const [form, setForm] = useState<Form | null>(null);
  const [msg, setMsg] = useState<{ text: string; bad?: boolean } | null>(null);
  const [busy, setBusy] = useState(false);
  const current = library.find((a) => a.id === sel);

  useEffect(() => {
    if (current) setForm({ id: current.id, title: current.title, location: current.location ?? "local",
      yaml: current.agent_yaml ?? "", prompt: current.prompt ?? "", isNew: false });
  }, [sel, library]); // eslint-disable-line react-hooks/exhaustive-deps
  useEffect(() => {
    const k = (e: KeyboardEvent) => { if (e.key === "Escape") onClose(); };
    window.addEventListener("keydown", k);
    return () => window.removeEventListener("keydown", k);
  }, [onClose]);

  const run = async (fn: () => Promise<void>) => {
    setBusy(true); setMsg(null);
    try { await fn(); } catch (e) { setMsg({ text: errorText(e, t), bad: true }); } finally { setBusy(false); }
  };
  const save = () => run(async () => {
    if (!form) return;
    if (!ID.test(form.id)) throw new Error(t("insp.nameHint"));
    const moving = !form.isNew && current && form.location !== current.location;
    if (moving) {  // first move it, then save the edits in the new place
      await api.moveLibraryAgent(form.id, form.location);
    }
    await api.saveToLibrary({ id: form.id, title: form.title, agent_yaml: form.yaml, prompt: form.prompt,
      overwrite: !form.isNew, location: form.location });
    await onChange();
    setSel(form.id);
    setMsg({ text: t("lib.saved", { name: form.id }) });
  });
  const duplicate = () => {
    if (!form) return;
    const ids = new Set(library.map((a) => a.id));
    let i = 2;
    while (ids.has(`${form.id}_${i}`)) i++;
    setSel(null);
    setForm({ ...form, id: `${form.id}_${i}`, isNew: true, location: form.location === "public" ? "local" : form.location });
  };
  const remove = () => run(async () => {
    if (!form || form.isNew || !(await dialog.confirm({ title: t("libm.deleteTitle", { name: form.id }),
      message: t(form.location === "public" ? "libm.deletePublic" : "libm.deleteMessage"), ok: t("app.delete"), danger: true }))) return;
    await api.deleteLibraryAgent(form.id);
    await onChange();
    setSel(null); setForm(null);
  });

  return (
    <div className="modal" onMouseDown={(e) => e.target === e.currentTarget && onClose()}>
      <div className="modal-box conns" role="dialog" aria-modal="true" aria-labelledby="libm-title">
        <div className="panel-head">
          <h2 id="libm-title">{t("palette.library")}</h2>
          <span className="grow" />
          <button className="small primary" onClick={() => { setSel(null); setMsg(null);
            setForm({ id: "", title: "", location: "local", yaml: NEW_YAML, prompt: "", isNew: true }); }}>{t("libm.new")}</button>
          <button className="small" onClick={onClose}>{t("dialog.close")}</button>
        </div>
        <div className="conns-body">
          <div className="conns-list">
            {library.map((a) => (
              <button key={a.id} className={`conn-item${sel === a.id ? " on" : ""}`} onClick={() => { setSel(a.id); setMsg(null); }}>
                <b><AgentIcon icon={a.icon} size={14} /> {a.title}</b>
                <span className={`muted small${a.location === "public" ? " warn-text" : ""}`}>{a.id} · {locLabel(t, a.location ?? "local")}</span>
              </button>))}
          </div>
          {form ? (
            <div className="conns-form">
              {form.location === "public" && <p className="badge warn">{t("libm.publicNote")}</p>}
              <div className="grid2">
                <label className="field"><span className="field-label">{t("lib.saveId")}</span>
                  <input className="mono" value={form.id} disabled={!form.isNew} onChange={(e) => setForm({ ...form, id: e.target.value })} />
                  <span className="field-hint">{t("lib.saveIdHint")}</span></label>
                <label className="field"><span className="field-label">{t("lib.saveTitleField")}</span>
                  <input value={form.title} onChange={(e) => setForm({ ...form, title: e.target.value })} /></label>
              </div>
              <label className="field"><span className="field-label">{t("loc.where")}</span>
                <select value={form.location} onChange={(e) => setForm({ ...form, location: e.target.value })}>
                  {locations.map((l) => <option key={l} value={l}>{locLabel(t, l)}</option>)}
                </select>
                <span className="field-hint">{t(form.isNew ? "loc.whereHint" : "libm.moveHint")}</span></label>
              <label className="field"><span className="field-label">{t("libm.definition")}</span>
                <textarea className="conn-instructions" rows={10} spellCheck={false} value={form.yaml}
                  onChange={(e) => setForm({ ...form, yaml: e.target.value })} />
                <span className="field-hint">{t("libm.definitionHint")}</span></label>
              <label className="field"><span className="field-label">{t("insp.prompt")}</span>
                <textarea className="conn-instructions" rows={10} value={form.prompt}
                  onChange={(e) => setForm({ ...form, prompt: e.target.value })} /></label>
              <div className="row actions">
                <button className="primary" disabled={busy} onClick={save}>{t("cmd.saveOk")}</button>
                {!form.isNew && <button disabled={busy} onClick={duplicate}>{t("menu.duplicate")}</button>}
                {!form.isNew && <button disabled={busy} title={t("copy.agentLibTitle")}
                  onClick={() => acts.copyTo({ kind: "agent", name: form.id }, form.id)}>{t("copy.button")}</button>}
                {!form.isNew && <button disabled={busy} title={t("copy.moveAgentLibTitle")}
                  onClick={() => acts.copyTo({ kind: "agent", name: form.id }, form.id, "move").then((r) => r && onChange())}>{t("copy.moveButton")}</button>}
                {!form.isNew && <button className="danger" disabled={busy} onClick={remove}>{t("app.delete")}</button>}
                {msg && <span className={`msg${msg.bad ? " bad" : ""}`} title={msg.text}>{msg.text}</span>}
              </div>
            </div>
          ) : <div className="conns-form muted">{t("libm.pick")}</div>}
        </div>
      </div>
    </div>
  );
}

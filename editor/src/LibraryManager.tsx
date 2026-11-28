// The agent library window: list, edit (definition in YAML + prompt), duplicate, move between
// this machine / an extension / the built-in library, copy or move to a workspace, delete. No flow
// needed. The form keeps its draft while the library reloads; the definition is checked while you
// type (Save stays off with a problem); Save writes in place first, then moves (a failed save
// moves nothing). Unsaved edits are never lost without a question.
import { useEffect, useRef, useState } from "react";
import { api, errorText } from "./api";
import { useDialog } from "./Dialog";
import { useI18n } from "./i18n";
import { localizeServer } from "./i18n/server";
import { AgentIcon } from "./icons";
import { Modal, useModalGuard } from "./Modal";
import { MoreMenu } from "./RunParts";
import type { LibraryAgent } from "./types";
import { useWorkspaceActions } from "./Workspaces";

const ID = /^[a-z][a-z0-9_]{0,39}$/;
const NEW_YAML = "description: What this agent does and when to use it.\ntools: []\ninputs: {}\noutputs: {}\n";

type Form = { id: string; title: string; location: string; yaml: string; prompt: string; isNew: boolean };

/** Where a flow or connector is kept (Local, public (git), an extension…). */
export function locLabel(t: ReturnType<typeof useI18n>["t"], loc: string) {
  return loc === "local" ? t("loc.local") : loc === "public" ? t("loc.public")
    : loc === "workspace" ? t("loc.workspace") : t("loc.extension", { name: loc });
}

/** Where a library agent is kept, in the palette's words (Built-in, Local, Workspace, Extension). */
export function libLocLabel(t: ReturnType<typeof useI18n>["t"], loc: string) {
  return loc === "public" ? t("lib.loc.builtin") : loc === "local" ? t("lib.loc.local")
    : loc === "workspace" ? t("lib.loc.workspace") : t("lib.loc.extension", { name: loc });
}

const formOf = (a: LibraryAgent): Form => ({ id: a.id, title: a.title, location: a.location ?? "local",
  yaml: a.agent_yaml ?? "", prompt: a.prompt ?? "", isNew: false });

export function LibraryManager({ library, locations, onChange, onClose }: {
  library: LibraryAgent[]; locations: string[]; onChange: () => Promise<void>; onClose: () => void;
}) {
  const { t, lang } = useI18n();
  const dialog = useDialog();
  const [sel, setSel] = useState<string | null>(library[0]?.id ?? null);
  const acts = useWorkspaceActions(undefined, (text, bad) => setMsg({ text, bad }));
  const [form, setForm] = useState<Form | null>(() => (library[0] ? formOf(library[0]) : null));
  const [base, setBase] = useState<string>(() => JSON.stringify(library[0] ? formOf(library[0]) : null));
  const [msg, setMsg] = useState<{ text: string; bad?: boolean } | null>(null);
  const [problem, setProblem] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const current = library.find((a) => a.id === sel);
  const dirty = form !== null && JSON.stringify(form) !== base;
  const guard = useModalGuard(dirty);

  /** Shows another agent (or a new one) after asking about unsaved edits. */
  const show = async (next: Form | null, id: string | null) => {
    if (!(await guard())) return;
    setSel(id); setForm(next); setBase(JSON.stringify(next)); setMsg(null); setProblem(null);
  };
  // the definition is checked while you type (what Save would refuse)
  const seq = useRef(0);
  useEffect(() => {
    if (!form) return;
    const n = ++seq.current;
    const h = setTimeout(() => {
      if (!ID.test(form.id)) { setProblem(t("insp.nameHint")); return; }
      api.checkLibraryAgent({ id: form.id, title: form.title, agent_yaml: form.yaml, prompt: form.prompt,
        location: form.isNew ? form.location : current?.location ?? form.location })
        .then((r) => { if (n === seq.current) setProblem(r.ok ? null : localizeServer(r, r.error ?? "?", lang)); })
        .catch(() => { if (n === seq.current) setProblem(null); });  // no answer: Save decides
    }, 300);
    return () => clearTimeout(h);
  }, [form?.id, form?.title, form?.yaml, form?.prompt]);  // eslint-disable-line react-hooks/exhaustive-deps

  const run = async (fn: () => Promise<void>) => {
    setBusy(true); setMsg(null);
    try { await fn(); } catch (e) { setMsg({ text: errorText(e, t), bad: true }); } finally { setBusy(false); }
  };
  const save = () => run(async () => {
    if (!form || problem) return;
    const from = form.isNew ? form.location : current?.location ?? form.location;
    // in its place first: a refused save changes nothing; then the move
    await api.saveToLibrary({ id: form.id, title: form.title, agent_yaml: form.yaml, prompt: form.prompt,
      overwrite: !form.isNew, location: from });
    if (form.location !== from) await api.moveLibraryAgent(form.id, form.location);
    await onChange();
    const saved = { ...form, isNew: false };
    setSel(form.id); setForm(saved); setBase(JSON.stringify(saved));
    setMsg({ text: t("lib.saved", { name: form.id }) });
  });
  const revert = () => { if (current) { const f = formOf(current); setForm(f); setBase(JSON.stringify(f)); } setMsg(null); };
  const duplicate = async () => {
    if (!form) return;
    const ids = new Set(library.map((a) => a.id));
    let i = 2;
    while (ids.has(`${form.id}_${i}`)) i++;
    // the copy keeps the edits (nothing is lost): no question
    const copy = { ...form, id: `${form.id}_${i}`, isNew: true, location: form.location === "public" ? "local" : form.location };
    setSel(null); setForm(copy); setBase(JSON.stringify(copy)); setMsg(null); setProblem(null);
  };
  const remove = () => run(async () => {
    if (!form || form.isNew || !(await dialog.confirm({ title: t("libm.deleteTitle", { name: form.id }),
      message: t(form.location === "public" ? "libm.deletePublic" : "libm.deleteMessage"), ok: t("app.delete"), danger: true }))) return;
    await api.deleteLibraryAgent(form.id);
    await onChange();
    setSel(null); setForm(null); setBase("null");
  });

  const foot = form && <>
    <button className="primary" disabled={busy || !!problem || (!dirty && !form.isNew)} aria-disabled={!dirty && !form.isNew}
      onClick={save}>{t("cmd.saveOk")}</button>
    {!form.isNew && <button disabled={busy || !dirty} onClick={revert}>{t("modal.revert")}</button>}
    {msg && <span className={`msg${msg.bad ? " bad" : ""}`} title={msg.text}>{msg.text}</span>}
    <span className="grow" />
    {!form.isNew && <MoreMenu label={t("libm.moreFor", { name: form.id })} className="lib-more" items={[
      { label: t("menu.duplicate"), onSelect: duplicate },
      { label: t("copy.button"), title: t("copy.agentLibTitle"), onSelect: () => acts.copyTo({ kind: "agent", name: form.id }, form.id) },
      { label: t("copy.moveButton"), title: t("copy.moveAgentLibTitle"), disabled: form.location === "public",
        note: form.location === "public" ? t("lib.builtinNoMove") : undefined,
        onSelect: () => acts.copyTo({ kind: "agent", name: form.id }, form.id, "move").then((r) => r && onChange()) },
      { kind: "sep" },
      { label: t("libm.deleteAgent"), danger: true, disabled: form.location === "public",
        note: form.location === "public" ? t("lib.builtinNoDelete") : undefined, onSelect: remove },
    ]}>⋯ {t("libm.more")}</MoreMenu>}
  </>;

  return (
    <Modal title={t("palette.library")} onClose={onClose} dirty={dirty} className="conns" foot={foot}
      live={msg?.text ?? problem ?? ""}
      head={<button className="small" onClick={() => show({ id: "", title: "", location: "local", yaml: NEW_YAML, prompt: "", isNew: true }, null)}>
        + {t("libm.new")}</button>}>
      <div className="conns-body">
        <div className="conns-list" role="list" aria-label={t("palette.library")}>
          {library.map((a) => (
            <button key={a.id} role="listitem" className={`conn-item${sel === a.id ? " on" : ""}`} aria-current={sel === a.id}
              onClick={() => { if (a.id !== sel) show(formOf(a), a.id); }}>
              <b><AgentIcon icon={a.icon} size={14} /> {a.title}</b>
              <span className="muted small">{a.id} · {libLocLabel(t, a.location ?? "local")}</span>
            </button>))}
        </div>
        {form ? (
          <div className="conns-form">
            {form.location === "public" && <p className="lib-note">{t("lib.publicNote")}<br />
              <span className="muted">{t("lib.builtinEdits")}</span></p>}
            <div className="grid2">
              <label className="field"><span className="field-label">{t("lib.saveId")}</span>
                <input className="mono" value={form.id} disabled={!form.isNew} onChange={(e) => setForm({ ...form, id: e.target.value })} />
                <span className="field-hint">{t("lib.saveIdHint")}</span></label>
              <label className="field"><span className="field-label">{t("lib.saveTitleField")}</span>
                <input value={form.title} onChange={(e) => setForm({ ...form, title: e.target.value })} /></label>
            </div>
            <label className="field"><span className="field-label">{t("loc.where")}</span>
              <select value={form.location} onChange={(e) => setForm({ ...form, location: e.target.value })}>
                {locations.map((l) => <option key={l} value={l}>{libLocLabel(t, l)}</option>)}
              </select>
              <span className="field-hint">{t(form.isNew ? "loc.whereHint" : "libm.moveHint")}</span></label>
            <label className="field"><span className="field-label">{t("libm.definition")}</span>
              <textarea className={`conn-instructions lib-code${problem ? " bad" : ""}`} rows={10} spellCheck={false} value={form.yaml}
                aria-invalid={!!problem} onChange={(e) => setForm({ ...form, yaml: e.target.value })} />
              {problem ? <span className="field-err" role="alert">{problem}</span>
                : <span className="field-hint">{t("libm.definitionHint")}</span>}</label>
            <label className="field"><span className="field-label">{t("insp.prompt")}</span>
              <textarea className="conn-instructions lib-code" rows={10} value={form.prompt}
                onChange={(e) => setForm({ ...form, prompt: e.target.value })} /></label>
          </div>
        ) : <div className="conns-form muted">{t("libm.pick")}</div>}
      </div>
    </Modal>
  );
}

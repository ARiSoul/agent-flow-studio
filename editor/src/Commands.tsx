// Saved assistant commands: a compact bar above the chat input (favorites first), a form to save
// or edit one (message + instructions sent with it) and a window to manage them all.
import { useEffect, useState } from "react";
import { api, errorText } from "./api";
import { useDialog } from "./Dialog";
import { useI18n } from "./i18n";
import type { SavedCommand } from "./types";

export const sortCommands = (cs: SavedCommand[]) =>
  [...cs].sort((a, b) => Number(b.favorite) - Number(a.favorite) || a.name.localeCompare(b.name));

/** Opens the save/edit form; resolves to the saved command, or null when cancelled. */
export function useCommandForm() {
  const { t } = useI18n();
  const dialog = useDialog();
  return async (init: Partial<SavedCommand>): Promise<SavedCommand | null> => {
    const r = await dialog.form({
      title: init.id ? t("cmd.editTitle", { name: init.name ?? "" }) : t("cmd.saveTitle"),
      message: t("cmd.formMessage"), ok: t("cmd.saveOk"),
      fields: [
        { key: "name", label: t("cmd.name"), value: init.name ?? "", placeholder: t("cmd.namePlaceholder"),
          validate: (v) => (!v.trim() ? t("app.required") : v.trim().length > 60 ? t("cmd.nameTooLong") : null) },
        { key: "text", label: t("cmd.text"), value: init.text ?? "", multiline: true, hint: t("cmd.textHint"),
          validate: (v) => (!v.trim() ? t("app.required") : null) },
        { key: "instructions", label: t("cmd.instructions"), value: init.instructions ?? "", multiline: true,
          hint: t("cmd.instructionsHint") },
        { key: "favorite", label: t("cmd.favorite"), value: init.favorite ? "1" : "", checkbox: true },
      ],
    });
    if (!r) return null;
    return api.saveCommand({ id: init.id, name: r.name, text: r.text, instructions: r.instructions,
      favorite: r.favorite === "1" });
  };
}

/** The compact bar: one chip per command (★ favorites first); click = use it in the input. */
export function CommandBar({ commands, active, onUse, onManage }: {
  commands: SavedCommand[]; active: string | null; onUse: (c: SavedCommand) => void; onManage: () => void;
}) {
  const { t } = useI18n();
  return (
    <div className="cmd-bar" role="toolbar" aria-label={t("cmd.bar")}>
      {sortCommands(commands).map((c) => (
        <button key={c.id} type="button" className={`cmd-chip${c.favorite ? " fav" : ""}${active === c.id ? " on" : ""}`}
          title={`${c.text}${c.instructions ? `\n\n${t("cmd.instructions")}: ${c.instructions.slice(0, 300)}` : ""}`}
          onClick={() => onUse(c)}>
          {c.favorite && <span aria-hidden="true">★ </span>}{c.name}
        </button>))}
      <button type="button" className="cmd-chip manage" onClick={onManage} title={t("cmd.manageTitle")}
        aria-label={t("cmd.manageTitle")}>{commands.length ? "⋯" : t("cmd.none")}</button>
    </div>
  );
}

/** Window to manage the commands: details on demand, use, edit, favorite, delete. */
export function CommandsManager({ commands, onChange, onUse, onClose }: {
  commands: SavedCommand[]; onChange: () => void; onUse: (c: SavedCommand) => void; onClose: () => void;
}) {
  const { t } = useI18n();
  const dialog = useDialog();
  const form = useCommandForm();
  const [open, setOpen] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => { if (e.key === "Escape") onClose(); };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);
  const act = async (fn: () => Promise<unknown>) => {
    setError(null);
    try { await fn(); onChange(); } catch (e) { setError(errorText(e, t)); }
  };
  return (
    <div className="modal" onMouseDown={(e) => e.target === e.currentTarget && onClose()}>
      <div className="modal-box cmd-manager" role="dialog" aria-modal="true" aria-labelledby="cmd-title">
        <div className="panel-head">
          <h2 id="cmd-title">{t("cmd.manageTitle")}</h2>
          <span className="grow" />
          <button className="small primary" onClick={() => act(() => form({}))}>{t("cmd.new")}</button>
          <button className="small" onClick={onClose}>{t("dialog.close")}</button>
        </div>
        <p className="muted small">{t("cmd.manageHint")}</p>
        {error && <p className="field-err">{error}</p>}
        {commands.length === 0 && <p className="muted">{t("cmd.empty")}</p>}
        <ul className="cmd-list">
          {sortCommands(commands).map((c) => (
            <li key={c.id}>
              <div className="cmd-row">
                <button className={`cmd-star${c.favorite ? " on" : ""}`} aria-pressed={c.favorite}
                  title={t(c.favorite ? "cmd.unfavorite" : "cmd.favorite")} aria-label={t(c.favorite ? "cmd.unfavorite" : "cmd.favorite")}
                  onClick={() => act(() => api.saveCommand({ ...c, favorite: !c.favorite }))}>{c.favorite ? "★" : "☆"}</button>
                <button className="cmd-name" aria-expanded={open === c.id} onClick={() => setOpen(open === c.id ? null : c.id)}>
                  {open === c.id ? "▾" : "▸"} {c.name}
                </button>
                <span className="grow" />
                <button className="small" onClick={() => { onUse(c); onClose(); }}>{t("cmd.use")}</button>
                <button className="small" onClick={() => act(() => form(c))}>{t("cmd.edit")}</button>
                <button className="small danger" onClick={() => act(async () => {
                  if (await dialog.confirm({ title: t("cmd.deleteTitle", { name: c.name }), message: t("cmd.deleteMessage"),
                    ok: t("app.delete"), danger: true })) await api.deleteCommand(c.id);
                })}>{t("app.delete")}</button>
              </div>
              {open === c.id && (
                <div className="cmd-details">
                  <span className="field-label">{t("cmd.text")}</span>
                  <pre className="mono">{c.text}</pre>
                  <span className="field-label">{t("cmd.instructions")}</span>
                  <pre className="mono">{c.instructions || t("cmd.noInstructions")}</pre>
                </div>)}
            </li>))}
        </ul>
      </div>
    </div>
  );
}

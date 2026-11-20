// History of the assistant conversations of a flow: open, rename, delete (runs/chats/<flow>/).
import { useEffect, useState } from "react";
import { api, errorText } from "./api";
import { useDialog } from "./Dialog";
import { useI18n } from "./i18n";
import type { ChatSummary } from "./types";

/** A new conversation id (hex, as the server expects). */
export function newChatId(): string {
  const b = new Uint8Array(6);
  crypto.getRandomValues(b);
  return [...b].map((x) => x.toString(16).padStart(2, "0")).join("");
}

export function ChatHistory({ flow, current, onOpen, onRenamed, onDeleted, onClose }: {
  flow: string; current: string; onOpen: (id: string) => void; onRenamed: (id: string, title: string) => void;
  onDeleted: (id: string) => void; onClose: () => void;
}) {
  const { t, lang } = useI18n();
  const dialog = useDialog();
  const [list, setList] = useState<ChatSummary[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const load = () => api.chats(flow).then(setList).catch((e) => setError(errorText(e, t)));
  useEffect(() => { load(); }, [flow]); // eslint-disable-line react-hooks/exhaustive-deps
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => { if (e.key === "Escape") onClose(); };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);
  const when = (s: number) => new Date(s * 1000).toLocaleString(lang, { dateStyle: "short", timeStyle: "short" });
  const rename = async (c: ChatSummary) => {
    const r = await dialog.form({ title: t("chat.renameTitle"), ok: t("cmd.saveOk"),
      fields: [{ key: "title", label: t("chat.titleField"), value: c.title,
        validate: (v) => (!v.trim() ? t("app.required") : v.length > 120 ? t("chat.titleTooLong") : null) }] });
    if (!r) return;
    try {
      const full = await api.chat(flow, c.id);
      await api.saveChat(flow, c.id, { ...full, title: r.title });
      onRenamed(c.id, r.title);
      load();
    } catch (e) { setError(errorText(e, t)); }
  };
  const remove = async (c: ChatSummary) => {
    if (!(await dialog.confirm({ title: t("chat.deleteTitle", { name: c.title || t("chat.untitled") }),
      message: t("chat.deleteMessage"), ok: t("app.delete"), danger: true }))) return;
    try { await api.deleteChat(flow, c.id); onDeleted(c.id); load(); }
    catch (e) { setError(errorText(e, t)); }
  };
  return (
    <div className="modal" onMouseDown={(e) => e.target === e.currentTarget && onClose()}>
      <div className="modal-box cmd-manager" role="dialog" aria-modal="true" aria-labelledby="chat-hist-title">
        <div className="panel-head">
          <h2 id="chat-hist-title">{t("chat.historyTitle")}</h2>
          <span className="grow" />
          <button className="small" onClick={onClose}>{t("dialog.close")}</button>
        </div>
        <p className="muted small">{t("chat.historyHint", { flow })}</p>
        {error && <p className="field-err">{error}</p>}
        {list && list.length === 0 && <p className="muted">{t("chat.historyEmpty")}</p>}
        <ul className="cmd-list">
          {(list ?? []).map((c) => (
            <li key={c.id}>
              <div className="cmd-row">
                <button className="cmd-name" onClick={() => onOpen(c.id)} title={t("chat.open")}>
                  {c.title || t("chat.untitled")}
                </button>
                {c.id === current && <span className="badge">{t("chat.current")}</span>}
                <span className="grow" />
                <span className="muted small">{t("chat.messages", { n: c.messages })} · {when(c.updated)}</span>
                <button className="small" onClick={() => rename(c)}>{t("chat.rename")}</button>
                <button className="small danger" onClick={() => remove(c)}>{t("app.delete")}</button>
              </div>
            </li>))}
        </ul>
      </div>
    </div>
  );
}


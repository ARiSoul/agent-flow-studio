import { useEffect, useRef, useState, type ReactNode } from "react";
import { api, errorText } from "./api";
import { useDialog } from "./Dialog";
import { useI18n, type Lang } from "./i18n";
import { AgentIcon } from "./icons";
import { ChatHistory, newChatId } from "./ChatHistory";
import { Markdown } from "./Markdown";
import { CommandBar, CommandsManager, useCommandForm } from "./Commands";
import type { AgentRaw, Doc, SavedCommand, Validation } from "./types";

/** An agent proposed by the assistant for the library (saved only by the user). */
type Proposal = { id: string; title: string; exists: boolean; notes: string[]; agent: AgentRaw; prompt: string };

type Item =
  | { kind: "user"; text: string; cmd?: string }
  | { kind: "assistant" | "error" | "info"; text: string }
  | { kind: "tool"; name: string; arg: string; edit: boolean }
  | { kind: "library"; p: Proposal };

type Event =
  | { type: "text"; text: string }
  | { type: "tool"; name: string; arg: string; edit: boolean }
  | { type: "done"; session_id: string; cost: number | null; turns: number; is_error: boolean;
      changed: boolean; doc: Doc | null; validation: Validation; library?: Proposal[] }
  | { type: "error"; message: string };

const SUGGESTIONS = ["chat.suggest1", "chat.suggest2", "chat.suggest3", "chat.suggest4"] as const;

/** Inline `code` and ``` blocks — the rest is plain text (no HTML). */
function Rich({ text }: { text: string }) {
  const out: ReactNode[] = [];
  text.split(/```[a-z]*\n?/).forEach((part, i) => {
    if (i % 2) out.push(<pre key={i} className="mono">{part}</pre>);
    else part.split(/(`[^`\n]+`)/).forEach((s, j) =>
      out.push(s.startsWith("`") && s.endsWith("`") && s.length > 1
        ? <code key={`${i}-${j}`}>{s.slice(1, -1)}</code> : <span key={`${i}-${j}`}>{s}</span>));
  });
  return <>{out}</>;
}

/** A proposed library agent: summary, prompt on demand, and the user's "Save to library". */
function LibraryCard({ p, onSaved }: { p: Proposal; onSaved: () => void }) {
  const { t } = useI18n();
  const dialog = useDialog();
  const [state, setState] = useState<{ saved?: boolean; error?: string; busy?: boolean }>({});
  const a = p.agent;
  const save = async () => {
    if (p.exists && !(await dialog.confirm({ title: t("lib.overwriteTitle", { name: p.id }),
      message: t("lib.overwrite"), ok: t("lib.overwriteOk"), danger: true }))) return;
    setState({ busy: true });
    try {
      await api.saveToLibrary({ id: p.id, title: p.title, agent: a, prompt: p.prompt, overwrite: p.exists });
      setState({ saved: true });
      onSaved();
    } catch (e) { setState({ error: errorText(e, t) }); }
  };
  const io = (m?: Record<string, { type?: string | null }>) =>
    Object.entries(m ?? {}).map(([n, x]) => `${n}: ${x.type ?? "text"}`).join(", ") || "—";
  return (
    <div className="chat-lib">
      <div className="chat-lib-head">
        <AgentIcon icon={a.icon} size={16} /> <b>{p.title}</b> <code>{p.id}</code>
        <span className={`badge${p.exists ? " warn" : ""}`}>{t(p.exists ? "chat.libReplaces" : "chat.libNew")}</span>
      </div>
      <p>{a.description}</p>
      <dl>
        <dt>{t("insp.tools")}</dt><dd>{(a.tools ?? []).join(", ") || "—"}</dd>
        <dt>{t("io.inputs")}</dt><dd>{io(a.inputs)}</dd>
        <dt>{t("io.outputs")}</dt><dd>{io(a.outputs)}</dd>
      </dl>
      <details><summary>{t("insp.prompt")}</summary><pre className="mono">{p.prompt}</pre></details>
      {p.notes.map((n, i) => <p key={i} className="muted small">{n}</p>)}
      <div className="row">
        {state.saved
          ? <span className="ok-text">{t("lib.saved", { name: p.id })}</span>
          : <button className="small primary" disabled={state.busy} onClick={save}>{t("chat.libSave")}</button>}
        {state.error && <span className="field-err">{state.error}</span>}
      </div>
    </div>
  );
}

export function Chat({ flow, lang, doc, apply, onBusy, onLibrary }: {
  flow: string; lang: Lang; doc: Doc; apply: (d: Doc) => void; onBusy: (b: boolean) => void;
  onLibrary: () => void;
}) {
  const { t } = useI18n();
  const [items, setItems] = useState<Item[]>([]);
  const [input, setInput] = useState("");
  const [busy, setBusy] = useState(false);
  const [session, setSession] = useState<string | null>(null);
  const [cost, setCost] = useState(0);
  const abort = useRef<AbortController | null>(null);
  const end = useRef<HTMLDivElement>(null);
  const docRef = useRef(doc);
  docRef.current = doc;
  // saved commands: a message + instructions sent with it (the active one goes with the next send)
  const [commands, setCommands] = useState<SavedCommand[]>([]);
  const [active, setActive] = useState<SavedCommand | null>(null);
  const [manage, setManage] = useState(false);
  const [cmdError, setCmdError] = useState<string | null>(null);
  const commandForm = useCommandForm();
  const loadCommands = () => api.commands().then(setCommands).catch((e) => setCmdError(errorText(e, t)));
  useEffect(() => { loadCommands(); }, []); // eslint-disable-line react-hooks/exhaustive-deps -- once
  const applyCommand = (c: SavedCommand) => { setInput(c.text); setActive(c); };
  const saveCommand = async (text: string) => {
    setCmdError(null);
    try {
      const c = await commandForm({ text, name: text.split("\n")[0].slice(0, 40) });
      if (c) await loadCommands();
    } catch (e) { setCmdError(errorText(e, t)); }
  };

  // history: one conversation at a time, saved after each reply (runs/chats/<flow>/<id>.json)
  const [chatId, setChatId] = useState<string>(newChatId);
  const [title, setTitle] = useState("");
  const [history, setHistory] = useState(false);
  const pending = useRef(false);  // the conversation changed and is not saved yet
  const openChat = async (id: string) => {
    try {
      const c = await api.chat(flow, id);
      setChatId(c.id); setTitle(c.title); setItems(c.items as Item[]); setSession(c.session_id);
      setCost(c.cost); setActive(null);
    } catch (e) { push({ kind: "error", text: errorText(e, t) }); }
  };
  const newChat = () => { setChatId(newChatId()); setTitle(""); setItems([]); setSession(null); setCost(0); };
  useEffect(() => {  // the flow's last conversation, if there is one
    api.chats(flow).then((cs) => { if (cs.length) openChat(cs[0].id); }).catch(() => undefined);
  }, [flow]); // eslint-disable-line react-hooks/exhaustive-deps -- once per flow (Chat is keyed by flow)
  useEffect(() => {
    if (busy || !pending.current || !items.length) return;
    pending.current = false;
    const first = items.find((i) => i.kind === "user");
    const name = title || (first ? first.text.split("\n")[0].slice(0, 80) : "");
    if (!title && name) setTitle(name);
    api.saveChat(flow, chatId, { title: name, session_id: session, cost, items })
      .catch((e) => push({ kind: "error", text: t("chat.notSaved", { error: errorText(e, t) }) }));
  }, [busy, items, session, cost, title, chatId, flow]); // eslint-disable-line react-hooks/exhaustive-deps

  useEffect(() => { end.current?.scrollIntoView({ behavior: "smooth" }); }, [items]);
  useEffect(() => () => abort.current?.abort(), []);
  const push = (it: Item) => setItems((p) => [...p, it]);
  const setB = (b: boolean) => { setBusy(b); onBusy(b); };

  const send = async (text: string) => {
    const msg = text.trim();
    if (!msg || busy) return;
    setInput("");
    const cmd = active;
    setActive(null);
    push({ kind: "user", text: msg, cmd: cmd?.name });
    pending.current = true;
    setB(true);
    const ctrl = new AbortController();
    abort.current = ctrl;
    try {
      const r = await fetch("/api/assist", {
        method: "POST", signal: ctrl.signal,
        headers: { "Content-Type": "application/json", "X-Flow-Editor": "1" },
        body: JSON.stringify({ flow, message: msg, session_id: session, lang, command: cmd?.id ?? null, ...docRef.current }),
      });
      if (!r.ok || !r.body) throw new Error((await r.text()) || r.statusText);
      const reader = r.body.getReader();
      const dec = new TextDecoder();
      let buf = "";
      for (;;) {
        const { value, done } = await reader.read();
        if (done) break;
        buf += dec.decode(value, { stream: true });
        let nl;
        while ((nl = buf.indexOf("\n")) >= 0) {
          const line = buf.slice(0, nl).trim();
          buf = buf.slice(nl + 1);
          if (!line) continue;
          const ev = JSON.parse(line) as Event;
          if (ev.type === "text") push({ kind: "assistant", text: ev.text });
          else if (ev.type === "tool") push({ kind: "tool", name: ev.name, arg: ev.arg, edit: ev.edit });
          else if (ev.type === "error") push({ kind: "error", text: ev.message });
          else if (ev.type === "done") {
            setSession(ev.session_id);
            setCost((c) => c + (ev.cost ?? 0));
            if (ev.is_error) push({ kind: "error", text: t("chat.endedWithError") });
            for (const p of ev.library ?? []) push({ kind: "library", p });
            if (ev.changed && ev.doc) {
              apply(ev.doc);
              push({ kind: "info", text: (ev.validation.ok ? t("chat.applied")
                : t("chat.appliedWithProblems", { n: ev.validation.errors.length })) + t("chat.undoHint") });
            }
          }
        }
      }
    } catch (e) {
      if ((e as Error).name !== "AbortError") push({ kind: "error", text: String(e) });
      else push({ kind: "info", text: t("chat.interrupted") });
    } finally {
      abort.current = null;
      setB(false);
    }
  };

  return (
    <div className="chat">
      <div className="chat-log">
        {items.length === 0 && (
          <div className="chat-empty">
            <p>{t("chat.intro")}</p>
            {SUGGESTIONS.map((s) => <button key={s} className="suggest" onClick={() => send(t(s))}>{t(s)}</button>)}
          </div>
        )}
        {items.map((it, i) => it.kind === "tool"
          ? <div key={i} className={`chat-tool${it.edit ? " edit" : ""}`}>{it.edit ? "✎" : "·"} {it.name} {it.arg && <code>{it.arg}</code>}</div>
          : it.kind === "library" ? <LibraryCard key={i} p={it.p} onSaved={onLibrary} />
          : it.kind === "user" ? (
            <div key={i} className="chat-msg user">
              {it.cmd && <span className="cmd-used">⌘ {it.cmd}</span>}
              <Rich text={it.text} />
              <button className="cmd-save-msg" title={t("cmd.saveFromMessage")} aria-label={t("cmd.saveFromMessage")}
                onClick={() => saveCommand(it.text)}>☆</button>
            </div>)
          : it.kind === "assistant" ? <div key={i} className="chat-msg assistant"><Markdown text={it.text} /></div>
          : <div key={i} className={`chat-msg ${it.kind}`}><Rich text={it.text} /></div>)}
        {busy && <div className="chat-tool">{t("chat.thinking")}</div>}
        <div ref={end} />
      </div>
      <div className="chat-input">
        <CommandBar commands={commands} active={active?.id ?? null} onUse={applyCommand} onManage={() => setManage(true)} />
        {active && (
          <div className="cmd-active">
            <span>⌘ <b>{active.name}</b>{active.instructions ? ` · ${t("cmd.withInstructions")}` : ""}</span>
            <button className="small" onClick={() => setActive(null)} title={t("cmd.detach")} aria-label={t("cmd.detach")}>✕</button>
          </div>)}
        {cmdError && <span className="field-err">{cmdError}</span>}
        <textarea rows={3} value={input} placeholder={t("chat.placeholder")}
          onChange={(e) => setInput(e.target.value)}
          onKeyDown={(e) => { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); send(input); } }} />
        <div className="chat-actions">
          <span className="muted small">{cost > 0 ? t("chat.cost", { cost: cost.toFixed(3) }) : ""}</span>
          <button className="small" onClick={() => saveCommand(input)} title={t("cmd.saveTitle")}>{t("cmd.save")}</button>
          <button className="small" disabled={busy} onClick={() => setHistory(true)}
            title={t("chat.historyTitle")}>{t("chat.history")}</button>
          <button className="small" disabled={busy || !items.length} onClick={newChat}
            title={t("chat.newConversationTitle")}>{t("chat.newConversation")}</button>
          {busy
            ? <button className="small danger" onClick={() => abort.current?.abort()}>{t("chat.stop")}</button>
            : <button className="small primary" disabled={!input.trim()} onClick={() => send(input)}>{t("chat.send")}</button>}
        </div>
      </div>
      {manage && <CommandsManager commands={commands} onChange={loadCommands} onUse={applyCommand}
        onClose={() => setManage(false)} />}
      {history && <ChatHistory flow={flow} current={chatId} onOpen={(id) => { setHistory(false); openChat(id); }}
        onRenamed={(id, name) => { if (id === chatId) setTitle(name); }}
        onDeleted={(id) => { if (id === chatId) newChat(); }} onClose={() => setHistory(false)} />}
    </div>
  );
}

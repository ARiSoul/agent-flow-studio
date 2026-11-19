import { useEffect, useRef, useState, type ReactNode } from "react";
import type { Doc, Validation } from "./types";

type Item =
  | { kind: "user" | "assistant" | "error" | "info"; text: string }
  | { kind: "tool"; name: string; arg: string; edit: boolean };

type Event =
  | { type: "text"; text: string }
  | { type: "tool"; name: string; arg: string; edit: boolean }
  | { type: "done"; session_id: string; cost: number | null; turns: number; is_error: boolean;
      changed: boolean; doc: Doc | null; validation: Validation }
  | { type: "error"; message: string };

const SUGGESTIONS = [
  "Explica-me este flow em 5 linhas.",
  "Revê este flow e sugere melhorias (sem alterar nada).",
  "Adiciona um agente revisor antes do último passo.",
  "Cria um flow novo de raiz: pergunta-me o que preciso.",
];

/** `código` em linha e blocos ``` — o resto é texto simples (sem HTML). */
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

export function Chat({ flow, doc, apply, onBusy }: {
  flow: string; doc: Doc; apply: (d: Doc) => void; onBusy: (b: boolean) => void;
}) {
  const [items, setItems] = useState<Item[]>([]);
  const [input, setInput] = useState("");
  const [busy, setBusy] = useState(false);
  const [session, setSession] = useState<string | null>(null);
  const [cost, setCost] = useState(0);
  const abort = useRef<AbortController | null>(null);
  const end = useRef<HTMLDivElement>(null);
  const docRef = useRef(doc);
  docRef.current = doc;

  useEffect(() => { end.current?.scrollIntoView({ behavior: "smooth" }); }, [items]);
  useEffect(() => () => abort.current?.abort(), []);
  const push = (it: Item) => setItems((p) => [...p, it]);
  const setB = (b: boolean) => { setBusy(b); onBusy(b); };

  const send = async (text: string) => {
    const msg = text.trim();
    if (!msg || busy) return;
    setInput("");
    push({ kind: "user", text: msg });
    setB(true);
    const ctrl = new AbortController();
    abort.current = ctrl;
    try {
      const r = await fetch("/api/assist", {
        method: "POST", signal: ctrl.signal,
        headers: { "Content-Type": "application/json", "X-Flow-Editor": "1" },
        body: JSON.stringify({ flow, message: msg, session_id: session, ...docRef.current }),
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
            if (ev.is_error) push({ kind: "error", text: "O assistente terminou com erro (limite de turnos?)." });
            if (ev.changed && ev.doc) {
              apply(ev.doc);
              push({ kind: "info", text: (ev.validation.ok ? "Alterações aplicadas ao canvas (por gravar). "
                : `Alterações aplicadas, com ${ev.validation.errors.length} problema(s). `) + "Ctrl+Z desfaz." });
            }
          }
        }
      }
    } catch (e) {
      if ((e as Error).name !== "AbortError") push({ kind: "error", text: String(e) });
      else push({ kind: "info", text: "Interrompido; nada foi aplicado." });
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
            <p>Pede-me para criar ou mudar agentes, gates, ligações, prompts, ferramentas, artefactos ou MCPs.
              Altero só o rascunho no canvas: tu revês e gravas.</p>
            {SUGGESTIONS.map((s) => <button key={s} className="suggest" onClick={() => send(s)}>{s}</button>)}
          </div>
        )}
        {items.map((it, i) => it.kind === "tool"
          ? <div key={i} className={`chat-tool${it.edit ? " edit" : ""}`}>{it.edit ? "✎" : "·"} {it.name} {it.arg && <code>{it.arg}</code>}</div>
          : <div key={i} className={`chat-msg ${it.kind}`}><Rich text={it.text} /></div>)}
        {busy && <div className="chat-tool">a pensar…</div>}
        <div ref={end} />
      </div>
      <div className="chat-input">
        <textarea rows={3} value={input} placeholder="Escreve… (Enter envia, Shift+Enter muda de linha)"
          onChange={(e) => setInput(e.target.value)}
          onKeyDown={(e) => { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); send(input); } }} />
        <div className="chat-actions">
          <span className="muted small">{cost > 0 ? `≈ ${cost.toFixed(3)} USD nesta conversa` : ""}</span>
          <button className="small" disabled={busy || !items.length}
            onClick={() => { setItems([]); setSession(null); setCost(0); }}>Nova conversa</button>
          {busy
            ? <button className="small danger" onClick={() => abort.current?.abort()}>Parar</button>
            : <button className="small primary" disabled={!input.trim()} onClick={() => send(input)}>Enviar</button>}
        </div>
      </div>
    </div>
  );
}

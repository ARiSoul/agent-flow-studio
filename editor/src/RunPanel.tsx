import { useEffect, useRef, useState } from "react";
import type { Doc } from "./types";

const MUTATE = { "Content-Type": "application/json", "X-Flow-Editor": "1" };

export type NodeRun = "active" | "done" | "error" | "gate";
type Ev = { i: number; type: string; [k: string]: unknown };
type RunInfo = { id: string; status: string; gate: { gate: string; kind: string; label: string; reason: string } | null; cost: number; input: string; mode?: string; target?: string | null };
export type RunMode = "full" | "step" | "until" | "agent";
export type RunRequest = { mode: RunMode; target?: string; nonce: number };

async function post<T>(url: string, body?: unknown): Promise<T> {
  const r = await fetch(url, { method: "POST", headers: MUTATE, body: body ? JSON.stringify(body) : undefined });
  const data = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error((data as { detail?: string }).detail ?? r.statusText);
  return data as T;
}

/** Estado de cada nó a partir dos eventos (para o canvas). */
export function traceOf(events: Ev[]): Record<string, NodeRun> {
  const s: Record<string, NodeRun> = {};
  for (const e of events) {
    const a = e.agent as string | undefined;
    if (e.type === "delegate" && a) {
      for (const k of Object.keys(s)) if (s[k] === "active") s[k] = "done";
      s[a] = "active";
    }
    if (e.type === "agent_done" && a) s[a] = e.error ? "error" : "done";
    if (e.type === "agent_blocked" && a) delete s[a];  // recusado pelo modo passo a passo
    if (e.type === "gate") s[e.gate as string] = "gate";
    if (e.type === "decision") s[e.gate as string] = "done";
  }
  return s;
}

const MODE_LABEL: Record<string, string> = {
  full: "flow completo", step: "passo a passo", until: "até ao agente", agent: "só o agente",
};

function line(e: Ev): { cls: string; text: string } | null {
  const who = e.agent ? `${e.agent}: ` : "";
  switch (e.type) {
    case "start": return { cls: "info", text: `Início · ${e.flow} · ${e.input} · ${MODE_LABEL[String(e.mode ?? "full")]}${e.target ? ` ${e.target}` : ""}${e.mock ? " · MOCK" : " · DADOS REAIS"}` };
    case "delegate": return { cls: "deleg", text: `→ ${e.agent}${e.task ? ` — ${e.task}` : ""}` };
    case "tool": return { cls: "tool", text: `${who}${e.tool}${e.artifact ? ` (${e.artifact})` : ""}` };
    case "tool_error": return { cls: "err", text: `${who}ferramenta devolveu erro` };
    case "agent_done": return { cls: e.error ? "err" : "ok", text: `✓ ${e.agent} terminou${e.error ? " com erro" : ""}` };
    case "text": return { cls: e.agent ? "sub" : "say", text: `${who}${e.text}` };
    case "gate": return { cls: "gate", text: `⏸ ${e.label} — ${e.reason}` };
    case "approval": return { cls: "gate", text: `✎ ${e.agent}: pede aprovação — ${e.summary}` };
    case "approval_result": return { cls: e.approved ? "ok" : "err", text: `${e.approved ? "✓ aprovado" : "✗ recusado"}: ${e.summary}${e.comment ? ` — ${e.comment}` : ""}` };
    case "agent_blocked": return null;
    case "continue": return { cls: "info", text: e.how === "full" ? "Continuar até ao fim" : e.how === "auto" ? "Próximo passo (automático)" : "Próximo passo" };
    case "decision": return { cls: "info", text: `Decisão em ${e.gate}: ${e.decision}${e.comment ? ` — ${e.comment}` : ""}` };
    case "result": return { cls: "info", text: `Sessão ${String(e.session_id).slice(0, 8)}… · ${e.turns} turnos · ≈ ${Number(e.cost ?? 0).toFixed(3)} USD${e.denials ? ` · ${e.denials} recusas do hook` : ""}` };
    case "end": return { cls: e.status === "error" ? "err" : "info", text: `Fim: ${e.status}${e.message ? ` — ${e.message}` : ""}${e.note ? ` (${e.note})` : ""}` };
    default: return null;
  }
}

export function RunPanel({ flow, doc, dirty, mock, onTrace, request }: {
  flow: string; doc: Doc; dirty: boolean; mock: boolean; onTrace: (t: Record<string, NodeRun>) => void;
  request?: RunRequest | null;
}) {
  const [mode, setMode] = useState<RunMode>("full");
  const [target, setTarget] = useState("");
  const [instruction, setInstruction] = useState("");
  const [inputs, setInputs] = useState<string[]>([]);  // artefactos já existentes para a entrada
  useEffect(() => {
    if (request) { setMode(request.mode); setTarget(request.target ?? ""); }
  }, [request]);
  const [input, setInput] = useState("");
  const [run, setRun] = useState<RunInfo | null>(null);
  const [events, setEvents] = useState<Ev[]>([]);
  const [comment, setComment] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [arts, setArts] = useState<{ name: string; size: number }[]>([]);
  const [view, setView] = useState<{ name: string; text: string } | null>(null);
  const [sent, setSent] = useState<Set<string>>(new Set());
  const evRef = useRef<Ev[]>([]);
  const end = useRef<HTMLDivElement>(null);
  const label = doc.spec.input?.label ?? "Entrada";

  useEffect(() => { onTrace(traceOf(events)); end.current?.scrollIntoView({ block: "nearest" }); }, [events, onTrace]);
  useEffect(() => () => onTrace({}), [onTrace]);

  const loadArts = async (inp: string) => {
    const r = await fetch(`/api/artifacts/${encodeURIComponent(inp)}`);
    setArts(r.ok ? await r.json() : []);
  };

  const follow = async (id: string, inp: string) => {
    const r = await fetch(`/api/runs/${id}/events?after=${evRef.current.length}`);
    if (!r.body) return;
    const reader = r.body.getReader();
    const dec = new TextDecoder();
    let buf = "";
    for (;;) {
      const { value, done } = await reader.read();
      if (done) break;
      buf += dec.decode(value, { stream: true });
      let nl;
      while ((nl = buf.indexOf("\n")) >= 0) {
        const raw = buf.slice(0, nl).trim();
        buf = buf.slice(nl + 1);
        if (!raw) continue;
        const ev = JSON.parse(raw) as Ev;
        if (ev.type === "state") setRun(ev as unknown as RunInfo);
        else { evRef.current = [...evRef.current, ev]; setEvents(evRef.current); }
        if (ev.type === "tool" || ev.type === "agent_done") loadArts(inp);
      }
    }
    loadArts(inp);
  };

  const start = async () => {
    setError(null);
    const inp = input.trim();
    if (!inp) return;
    if (!mock && !confirm("AFS_MOCK está desligado: os conectores vão ler dados reais. Continuar?")) return;
    try {
      if ((mode === "until" || mode === "agent") && !target) throw new Error("Escolhe o agente");
      const r = await post<RunInfo>("/api/runs", { flow, input: inp, mode, target: target || null, instruction: mode === "agent" ? instruction : "" });
      evRef.current = []; setEvents([]); setView(null); setRun(r);
      await follow(r.id, inp);
    } catch (e) { setError(String((e as Error).message)); }
  };

  const cont = async (how: "step" | "full") => {
    if (!run) return;
    setError(null);
    try {
      const r = await post<RunInfo>(`/api/runs/${run.id}/continue`, { how });
      setRun(r);
      await follow(r.id, run.input);
    } catch (e) { setError(String((e as Error).message)); }
  };

  // artefactos de que o agente-alvo precisa (modo "só o agente")
  useEffect(() => {
    const inp = input.trim();
    if (mode !== "agent" || !/^[A-Za-z0-9_-]{1,40}$/.test(inp)) { setInputs([]); return; }
    const t = setTimeout(() => fetch(`/api/artifacts/${encodeURIComponent(inp)}`)
      .then((r) => (r.ok ? r.json() : [])).then((a: { name: string }[]) => setInputs(a.map((x) => x.name)))
      .catch(() => setInputs([])), 300);
    return () => clearTimeout(t);
  }, [mode, input]);
  const needs = mode === "agent" && target ? doc.spec.agents[target]?.reads ?? [] : [];
  const missing = needs.filter((n) => !inputs.includes(n));

  const decide = async (decision: "approve" | "reject" | "answer") => {
    if (!run) return;
    setError(null);
    try {
      const r = await post<RunInfo>(`/api/runs/${run.id}/decision`, { decision, comment });
      setComment(""); setRun(r);
      await follow(r.id, run.input);
    } catch (e) { setError(String((e as Error).message)); }
  };

  const open = async (name: string) => {
    if (!run) return;
    const r = await fetch(`/api/artifacts/${encodeURIComponent(run.input)}/${encodeURIComponent(name)}`);
    if (r.ok) setView(await r.json());
  };

  const decided = new Set(events.filter((e) => e.type === "approval_result").map((e) => e.id as string));
  const pending = events.filter((e) => e.type === "approval" && !decided.has(e.id as string) && !sent.has(e.id as string));
  const answer = async (id: string, ok: boolean) => {
    if (!run || sent.has(id)) return;
    setSent((s) => new Set(s).add(id));  // esconde já o cartão (evita duplo clique)
    try { await post(`/api/runs/${run.id}/approvals/${id}`, { approve: ok, comment }); setComment(""); }
    catch (e) { setError(String((e as Error).message)); }
  };
  const running = run?.status === "running";
  const gate = run?.status === "gate" ? run.gate : null;
  const paused = run?.status === "paused";
  const agents = Object.keys(doc.spec.agents);
  return (
    <div className="runp">
      <div className="run-head">
        <div className="row">
          <input placeholder={`${label} (ex.: teste-001)`} value={input} disabled={running}
            onChange={(e) => setInput(e.target.value)} onKeyDown={(e) => e.key === "Enter" && start()} />
          {running
            ? <button className="danger" onClick={() => run && post(`/api/runs/${run.id}/stop`)}>Parar</button>
            : <button className="primary" disabled={!input.trim() || dirty} onClick={start}
              title={dirty ? "Grava o flow antes de executar (corre a versão em disco)" : "Executar"}>Executar</button>}
        </div>
        <div className="row mode-row">
          <select value={mode} disabled={running} onChange={(e) => setMode(e.target.value as RunMode)}
            title="Como correr: o flow todo, um agente de cada vez, até um agente, ou só um agente isolado">
            <option value="full">Flow completo</option>
            <option value="step">Passo a passo</option>
            <option value="until">Até ao agente…</option>
            <option value="agent">Só um agente (isolado)</option>
          </select>
          {(mode === "until" || mode === "agent") && (
            <select value={target} disabled={running} onChange={(e) => setTarget(e.target.value)}>
              <option value="">— agente —</option>
              {agents.map((a) => <option key={a}>{a}</option>)}
            </select>)}
        </div>
        {mode === "agent" && <>
          <textarea rows={2} disabled={running} placeholder="Tarefa para o agente (opcional; por defeito, o seu passo do flow)"
            value={instruction} onChange={(e) => setInstruction(e.target.value)} />
          {target && needs.length > 0 && (
            <p className={`small ${missing.length ? "field-err" : "muted"}`}>
              {missing.length ? `Faltam artefactos de entrada em runs/${input || "<entrada>"}/: ${missing.join(", ")} (corre antes os agentes anteriores, ou um flow até aqui)`
                : `Artefactos de entrada presentes: ${needs.join(", ")}`}</p>)}
        </>}
        <span className={`badge ${mock ? "" : "warn"}`}>{mock ? "mock (dados simulados)" : "dados reais"}</span>
        {dirty && <span className="muted small"> grava antes de executar</span>}
        {run && <span className="muted small"> · {run.status}{run.cost ? ` · ≈ ${run.cost.toFixed(3)} USD` : ""}</span>}
        {error && <div className="field-err">{error}</div>}
      </div>

      <div className="run-log">
        {!run && <p className="muted">Corre o flow gravado com uma entrada. Vês no canvas o agente ativo, os que terminaram e o gate onde parou. Nos gates de aprovação humana decides aqui: a decisão fica registada em <code>runs/&lt;entrada&gt;/decisions.jsonl</code> e nada é executado.</p>}
        {events.map((e) => { const l = line(e); return l && <div key={e.i} className={`ev ${l.cls}`}>{l.text}</div>; })}
        {pending.map((p) => (
          <div key={p.id as string} className="gate-card write">
            <b>Escrita pedida por {String(p.agent)} · {String(p.environment)}</b>
            <p className="small mono">{String(p.summary)}</p>
            <details open><summary className="small">Detalhe</summary>
              <pre className="mono art-view">{JSON.stringify(p.detail, null, 2)}</pre></details>
            <textarea rows={2} placeholder="Comentário (opcional; em caso de recusa, explica porquê)" value={comment} onChange={(e) => setComment(e.target.value)} />
            <div className="row">
              <button className="primary small" onClick={() => answer(p.id as string, true)}>Aprovar escrita</button>
              <button className="danger small" onClick={() => answer(p.id as string, false)}>Recusar</button>
            </div>
          </div>
        ))}
        {running && !pending.length && <div className="ev tool">a trabalhar…</div>}
        {paused && (
          <div className="gate-card step">
            <b>Em pausa{run?.target && run.mode === "until" ? ` — ${run.target} terminou` : " depois deste passo"}</b>
            <p className="small muted">Revê o trace e os artefactos abaixo. Podes editar o flow e gravar antes de continuar: o passo seguinte usa a versão gravada.</p>
            <div className="row">
              <button className="primary small" onClick={() => cont("step")}>Próximo passo</button>
              <button className="small" onClick={() => cont("full")}>Continuar até ao fim</button>
              <button className="danger small" onClick={() => run && post(`/api/runs/${run.id}/stop`).then(() => setRun({ ...run, status: "stopped" }))}>Terminar</button>
            </div>
          </div>
        )}
        {gate && gate.kind === "human" && (
          <div className="gate-card">
            <b>Aprovação humana: {gate.label}</b>
            <p className="small">{gate.reason}</p>
            <textarea rows={3} placeholder="Comentário ou resposta (opcional)" value={comment} onChange={(e) => setComment(e.target.value)} />
            <div className="row">
              <button className="primary small" onClick={() => decide("approve")}>Aprovar</button>
              <button className="small" onClick={() => decide("answer")} disabled={!comment.trim()}>Responder e continuar</button>
              <button className="danger small" onClick={() => decide("reject")}>Rejeitar</button>
            </div>
            <p className="muted small">Aprovar não executa ações: os rascunhos continuam rascunhos. Se o gate tiver passos seguintes, o flow continua.</p>
          </div>
        )}
        <div ref={end} />
      </div>

      {arts.length > 0 && (
        <div className="run-arts">
          <h2>Artefactos · runs/{run?.input}/</h2>
          <div className="arts">{arts.map((a) => (
            <button key={a.name} className={`small${view?.name === a.name ? " on" : ""}`} onClick={() => open(a.name)}>{a.name}</button>
          ))}</div>
          {view && <pre className="mono art-view">{view.name.endsWith(".json") ? (() => { try { return JSON.stringify(JSON.parse(view.text), null, 2); } catch { return view.text; } })() : view.text}</pre>}
        </div>
      )}
    </div>
  );
}

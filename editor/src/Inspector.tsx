import { useEffect, useState, type ReactNode } from "react";
import { artifactOwners, renameNode, SLUG, nodeNames } from "./model";
import type { AgentRaw, ArtifactRaw, Catalog, Doc, FlowRaw, McpRaw, Selection } from "./types";

type Update = (fn: (d: Doc) => void) => void;
interface Props {
  doc: Doc;
  sel: Selection;
  catalog: Catalog;
  update: Update;
  select: (s: Selection) => void;
  remove: (s: Selection) => void;
  testAgent?: (id: string) => void;
}

function Field({ label, hint, children }: { label: string; hint?: string; children: ReactNode }) {
  return (
    <label className="field">
      <span className="field-label">{label}</span>
      {children}
      {hint && <span className="field-hint">{hint}</span>}
    </label>
  );
}

/** Campo que só confirma ao sair (Enter ou blur): usado para nomes, que mexem nas ligações. */
function CommitInput({ value, onCommit, pattern }: { value: string; onCommit: (v: string) => string | null; pattern?: RegExp }) {
  const [v, setV] = useState(value);
  const [err, setErr] = useState<string | null>(null);
  useEffect(() => { setV(value); setErr(null); }, [value]);
  const commit = () => {
    if (v === value) return;
    if (pattern && !pattern.test(v)) return setErr("minúsculas, dígitos e _ (começa por letra)");
    const e = onCommit(v);
    setErr(e);
    if (e) setV(value);
  };
  return (
    <>
      <input className={err ? "bad" : ""} value={v} onChange={(e) => setV(e.target.value)} onBlur={commit}
        onKeyDown={(e) => e.key === "Enter" && (e.target as HTMLInputElement).blur()} />
      {err && <span className="field-err">{err}</span>}
    </>
  );
}

const AUTONOMY: Record<string, string> = {
  approve: "aprovar cada escrita", approve_prod: "aprovar só em produção", auto: "autónomo (sem aprovação)",
};

const lines = (s: string) => s.split("\n").map((x) => x.trim()).filter(Boolean);
const csv = (s: string) => s.split(",").map((x) => x.trim()).filter(Boolean);
const kv = (s: string, sep: string) =>
  Object.fromEntries(lines(s).map((l) => {
    const i = l.indexOf(sep);
    return i < 0 ? [l, ""] : [l.slice(0, i).trim(), l.slice(i + 1).trim()];
  }));
const unkv = (o: Record<string, string> | undefined, sep: string) =>
  Object.entries(o ?? {}).map(([k, v]) => `${k}${sep}${v}`).join("\n");

/** Prompt em linha ou num ficheiro prompts/*.md (editado aqui e gravado com o flow). */
function PromptEditor({ owner, defaultPath, doc, update, pick }: {
  owner: { prompt?: string | null; prompt_file?: string | null };
  defaultPath: string;
  doc: Doc;
  update: Update;
  pick: (d: Doc) => { prompt?: string | null; prompt_file?: string | null };
}) {
  const file = owner.prompt_file ?? null;
  const text = file ? doc.prompts[file] ?? "" : owner.prompt ?? "";
  const missing = file !== null && !(file in doc.prompts);
  return (
    <div className="prompt">
      <div className="seg">
        <button className={file ? "" : "on"} onClick={() => update((d) => {
          const o = pick(d);
          if (!o.prompt_file) return;
          o.prompt = d.prompts[o.prompt_file] ?? "";
          delete o.prompt_file;
        })}>Em linha</button>
        <button className={file ? "on" : ""} onClick={() => update((d) => {
          const o = pick(d);
          if (o.prompt_file) return;
          const path = defaultPath;
          if (!(path in d.prompts)) d.prompts[path] = o.prompt ?? "";
          o.prompt_file = path;
          delete o.prompt;
        })}>Ficheiro</button>
        {file && <code className="path">{file}</code>}
      </div>
      {missing && <span className="field-err">ficheiro não existe; será criado ao guardar</span>}
      <textarea className="mono" rows={14} value={text} spellCheck={false}
        onChange={(e) => {
          const v = e.target.value;
          update((d) => {
            const o = pick(d);
            if (o.prompt_file) d.prompts[o.prompt_file] = v;
            else o.prompt = v;
          });
        }} />
    </div>
  );
}

function ToolPicker({ agent, spec, catalog, set }: {
  agent: AgentRaw; spec: FlowRaw; catalog: Catalog; set: (tools: string[]) => void;
}) {
  const chosen = new Set(agent.tools ?? []);
  const groups: [string, { id: string; name: string; description: string; note?: string }[]][] = [];
  for (const s of catalog.servers)
    groups.push([s, catalog.tools.filter((t) => t.server === s).map((t) => ({
      ...t, note: t.writes_local_only ? "escreve só em runs/<id>/" : t.kind === "write" ? "escrita" : undefined,
    }))]);
  for (const [s, m] of Object.entries(spec.mcp_servers ?? {}))
    groups.push([`${s} (externo)`, m.tools.map((n) => ({
      id: `${s}.${n}`, name: n, description: m.description ?? "",
      note: !m.approved ? "MCP não aprovado" : !m.read_only ? "MCP sem read_only" : undefined,
    }))]);
  const unknown = [...chosen].filter((t) => !groups.some(([, ts]) => ts.some((x) => x.id === t)));
  const toggle = (id: string) => {
    const next = new Set(chosen);
    if (next.has(id)) next.delete(id); else next.add(id);
    set([...(agent.tools ?? []).filter((t) => next.has(t)), ...[...next].filter((t) => !chosen.has(t))]);
  };
  return (
    <div className="tools">
      {groups.map(([g, ts]) => (
        <details key={g} open={ts.some((t) => chosen.has(t.id))}>
          <summary>{g} <span className="muted">{ts.filter((t) => chosen.has(t.id)).length}/{ts.length}</span></summary>
          {ts.map((t) => (
            <label key={t.id} className="check" title={t.description}>
              <input type="checkbox" checked={chosen.has(t.id)} onChange={() => toggle(t.id)} />
              <span>{t.name}</span>
              {t.note && <span className="muted small">{t.note}</span>}
            </label>
          ))}
        </details>
      ))}
      {unknown.map((t) => (
        <label key={t} className="check bad">
          <input type="checkbox" checked onChange={() => toggle(t)} /> <span>{t}</span>
          <span className="small">não existe no catálogo</span>
        </label>
      ))}
    </div>
  );
}

function WritesEditor({ writes, catalog, set }: { writes: ArtifactRaw[]; catalog: Catalog; set: (w: ArtifactRaw[]) => void }) {
  const patch = (i: number, p: Partial<ArtifactRaw>) => set(writes.map((w, j) => (j === i ? { ...w, ...p } : w)));
  return (
    <div className="rows">
      {writes.map((w, i) => (
        <div key={i} className="row">
          <input value={w.name} placeholder="01_nome.json" onChange={(e) => patch(i, { name: e.target.value })} />
          {w.name.endsWith(".json") && (
            <select value={w.contract ?? ""} onChange={(e) => patch(i, { contract: e.target.value || undefined })}>
              <option value="">sem contrato</option>
              {Object.keys(catalog.contracts).map((c) => <option key={c}>{c}</option>)}
            </select>
          )}
          {w.name.endsWith(".md") && (
            <label className="check inline" title="Rascunho para aprovação humana (marcado automaticamente)">
              <input type="checkbox" checked={!!w.draft} onChange={(e) => patch(i, { draft: e.target.checked || undefined })} />
              rascunho
            </label>
          )}
          <button className="icon" title="Remover" onClick={() => set(writes.filter((_, j) => j !== i))}>×</button>
        </div>
      ))}
      <button className="small" onClick={() => set([...writes, { name: "" }])}>+ artefacto</button>
    </div>
  );
}

function AgentPanel({ id, doc, catalog, update, select, testAgent }: { id: string } & Omit<Props, "sel" | "remove">) {
  const spec = doc.spec;
  const a = spec.agents[id];
  const setA = (fn: (a: AgentRaw) => void) => update((d) => fn(d.spec.agents[id]));
  const owners = artifactOwners(spec);
  const readable = [...owners.entries()].filter(([, o]) => o !== id).map(([n]) => n);
  const extraReads = (a.reads ?? []).filter((r) => !readable.includes(r));
  return (
    <>
      {testAgent && (
        <div className="row test-row">
          <button className="small" onClick={() => testAgent(id)} title="Abre Executar no modo 'só este agente'">▶ Testar este agente</button>
          <button className="small" onClick={() => testAgent(`until:${id}`)} title="Corre o flow passo a passo até este agente terminar">⏭ Correr até aqui</button>
        </div>)}
      <Field label="Nome" hint="Identificador do subagente (usado na delegação)">
        <CommitInput value={id} pattern={SLUG} onCommit={(v) => {
          if (nodeNames(spec).includes(v)) return "já existe um nó com esse nome";
          update((d) => renameNode(d.spec, id, v));
          select({ kind: "node", id: v });
          return null;
        }} />
      </Field>
      <label className="check">
        <input type="checkbox" checked={spec.start === id} disabled={spec.start === id}
          onChange={() => update((d) => { d.spec.start = id; })} />
        Início do flow
      </label>
      <Field label="Descrição" hint="O orquestrador usa-a para decidir quando delegar">
        <textarea rows={3} value={a.description} onChange={(e) => setA((x) => { x.description = e.target.value; })} />
      </Field>
      <Field label="Prompt">
        <PromptEditor owner={a} defaultPath={`prompts/${id}.md`} doc={doc} update={update}
          pick={(d) => d.spec.agents[id]} />
      </Field>
      <div className="grid2">
        <Field label="Modelo">
          <input list="models" value={a.model ?? ""} placeholder="o do flow"
            onChange={(e) => setA((x) => { x.model = e.target.value || undefined; })} />
        </Field>
        <Field label="Máx. turnos">
          <input type="number" min={1} max={200} value={a.max_turns ?? ""} placeholder="—"
            onChange={(e) => setA((x) => { x.max_turns = e.target.value ? Number(e.target.value) : undefined; })} />
        </Field>
      </div>
      <Field label="Ferramentas" hint="Lista fechada: o hook de guarda só deixa este agente usar estas">
        <ToolPicker agent={a} spec={spec} catalog={catalog} set={(t) => setA((x) => { x.tools = t; })} />
      </Field>
      <Field label="Conectores" hint="Só leitura (GET). Ativar um conector dá ao agente a ferramenta connections.http_get">
        <div className="tools">
          {Object.keys(catalog.connections).length === 0 && <span className="muted small">sem conectores: cria-os no botão Conectores</span>}
          {[...new Set([...Object.keys(catalog.connections), ...(a.connections ?? [])])].map((c) => {
            const info = catalog.connections[c];
            return (
              <label key={c} className={`check${info ? "" : " bad"}`}>
                <input type="checkbox" checked={(a.connections ?? []).includes(c)} onChange={(e) => setA((x) => {
                  const s = new Set(x.connections ?? []);
                  if (e.target.checked) s.add(c); else s.delete(c);
                  if (s.size) x.connections = [...s]; else delete x.connections;
                  const tools = new Set(x.tools ?? []);
                  if (s.size) tools.add("connections.http_get"); else tools.delete("connections.http_get");
                  x.tools = [...(x.tools ?? []).filter((t) => tools.has(t)), ...[...tools].filter((t) => !(x.tools ?? []).includes(t))];
                })} />
                <span>{c}</span><span className="muted small">{info ? `${info.type} · ${info.environment}` : "não configurado nesta máquina"}</span>
              </label>);
          })}
        </div>
      </Field>
      <Field label="Conectores com escrita" hint="POST/PUT/PATCH/DELETE com connections.http_write, conforme a autonomia">
        <div className="tools">
          {Object.entries(catalog.connections).filter(([, i]) => i.access === "write").length === 0 &&
            <span className="muted small">nenhum conector com acesso de escrita</span>}
          {[...new Set([...Object.entries(catalog.connections).filter(([, i]) => i.access === "write").map(([n]) => n),
            ...(a.write_connections ?? [])])].map((c) => {
            const info = catalog.connections[c];
            return (
              <label key={c} className={`check${info?.access === "write" ? "" : " bad"}`}>
                <input type="checkbox" checked={(a.write_connections ?? []).includes(c)} onChange={(e) => setA((x) => {
                  const s = new Set(x.write_connections ?? []);
                  if (e.target.checked) s.add(c); else s.delete(c);
                  if (s.size) x.write_connections = [...s]; else delete x.write_connections;
                  const tools = new Set(x.tools ?? []);
                  if (s.size) tools.add("connections.http_write"); else tools.delete("connections.http_write");
                  x.tools = [...(x.tools ?? []).filter((t) => tools.has(t)), ...[...tools].filter((t) => !(x.tools ?? []).includes(t))];
                })} />
                <span>{c}</span><span className="muted small">{info ? `${info.type} · ${info.environment}` : "não configurado"}</span>
              </label>);
          })}
        </div>
      </Field>
      <Field label="Autonomia nas escritas" hint="Vazio = a do flow">
        <select value={a.autonomy ?? ""} onChange={(e) => setA((x) => { x.autonomy = (e.target.value || undefined) as AgentRaw["autonomy"]; })}>
          <option value="">a do flow ({AUTONOMY[spec.autonomy ?? "approve"]})</option>
          {Object.entries(AUTONOMY).map(([k, v]) => <option key={k} value={k}>{v}</option>)}
        </select>
      </Field>
      <Field label="Lê artefactos">
        <div className="tools">
          {readable.length === 0 && <span className="muted small">nenhum outro agente escreve artefactos</span>}
          {[...readable, ...extraReads].map((r) => (
            <label key={r} className={`check${extraReads.includes(r) ? " bad" : ""}`}>
              <input type="checkbox" checked={(a.reads ?? []).includes(r)} onChange={(e) => setA((x) => {
                const s = new Set(x.reads ?? []);
                if (e.target.checked) s.add(r); else s.delete(r);
                x.reads = [...readable, ...extraReads].filter((n) => s.has(n));
              })} />
              <span>{r}</span><span className="muted small">{owners.get(r) ?? "ninguém o escreve"}</span>
            </label>
          ))}
        </div>
      </Field>
      <Field label="Escreve artefactos" hint="JSON com contrato é validado ao guardar; .md de rascunho fica marcado para aprovação">
        <WritesEditor writes={a.writes ?? []} catalog={catalog} set={(w) => setA((x) => { x.writes = w; })} />
      </Field>
      <Field label="Skills" hint="Separadas por vírgulas (experimental)">
        <input value={(a.skills ?? []).join(", ")} onChange={(e) => setA((x) => { x.skills = csv(e.target.value); })} />
      </Field>
    </>
  );
}

function GatePanel({ id, doc, update, select }: { id: string } & Omit<Props, "sel" | "remove" | "catalog">) {
  const g = doc.spec.gates![id];
  return (
    <>
      <Field label="Nome">
        <CommitInput value={id} pattern={SLUG} onCommit={(v) => {
          if (nodeNames(doc.spec).includes(v)) return "já existe um nó com esse nome";
          update((d) => renameNode(d.spec, id, v));
          select({ kind: "node", id: v });
          return null;
        }} />
      </Field>
      <Field label="Tipo" hint="Aprovação humana: o flow pára e pode ser retomado. Paragem: termina.">
        <select value={g.kind} onChange={(e) => update((d) => { d.spec.gates![id].kind = e.target.value as "human" | "stop"; })}>
          <option value="human">Aprovação humana</option>
          <option value="stop">Paragem</option>
        </select>
      </Field>
      <Field label="Texto mostrado">
        <input value={g.label} onChange={(e) => update((d) => { d.spec.gates![id].label = e.target.value; })} />
      </Field>
    </>
  );
}

function EdgePanel({ index, doc, update }: { index: number; doc: Doc; update: Update }) {
  const e = doc.spec.edges![index];
  if (!e) return null;
  return (
    <>
      <p className="edge-title"><code>{e.from}</code> → <code>{e.to}</code></p>
      <Field label="Condição" hint="Em linguagem natural; vazio = segue sempre. Vai para o prompt do orquestrador.">
        <textarea rows={3} value={e.when ?? ""} onChange={(ev) => update((d) => { d.spec.edges![index].when = ev.target.value; })} />
      </Field>
    </>
  );
}

function McpEditor({ name, m, update }: { name: string; m: McpRaw; update: Update }) {
  const set = (fn: (m: McpRaw) => void) => update((d) => fn(d.spec.mcp_servers![name]));
  const remote = m.type !== "stdio";
  return (
    <details className="mcp" open>
      <summary>
        <b>{name}</b> <span className="muted">{m.type}</span>
        {!(m.approved && m.read_only) && <span className="badge warn">bloqueado</span>}
      </summary>
      <div className="grid2">
        <Field label="Tipo">
          <select value={m.type} onChange={(e) => set((x) => { x.type = e.target.value as McpRaw["type"]; })}>
            <option>stdio</option><option>http</option><option>sse</option>
          </select>
        </Field>
        <Field label="Descrição">
          <input value={m.description ?? ""} onChange={(e) => set((x) => { x.description = e.target.value; })} />
        </Field>
      </div>
      {remote ? (
        <>
          <Field label="URL (https)"><input value={m.url ?? ""} onChange={(e) => set((x) => { x.url = e.target.value; })} /></Field>
          <Field label="Cabeçalhos" hint="Um por linha, Nome: valor. Segredos só como ${VAR}">
            <textarea className="mono" rows={2} defaultValue={unkv(m.headers, ": ")}
              onBlur={(e) => set((x) => { x.headers = kv(e.target.value, ":"); })} />
          </Field>
        </>
      ) : (
        <>
          <Field label="Comando"><input value={m.command ?? ""} onChange={(e) => set((x) => { x.command = e.target.value; })} /></Field>
          <Field label="Argumentos" hint="Um por linha">
            <textarea className="mono" rows={2} defaultValue={(m.args ?? []).join("\n")}
              onBlur={(e) => set((x) => { x.args = lines(e.target.value); })} />
          </Field>
          <Field label="Ambiente" hint="Um por linha, NOME=${VAR}. Os valores ficam no .env, nunca aqui">
            <textarea className="mono" rows={2} defaultValue={unkv(m.env, "=")}
              onBlur={(e) => set((x) => { x.env = kv(e.target.value, "="); })} />
          </Field>
        </>
      )}
      <Field label="Ferramentas permitidas" hint="Nomes exatos, separados por vírgulas (sem *)">
        <input defaultValue={m.tools.join(", ")} onBlur={(e) => set((x) => { x.tools = csv(e.target.value); })} />
      </Field>
      <label className="check"><input type="checkbox" checked={!!m.read_only}
        onChange={(e) => set((x) => { x.read_only = e.target.checked; })} /> As ferramentas listadas só leem</label>
      <label className="check"><input type="checkbox" checked={!!m.approved}
        onChange={(e) => set((x) => { x.approved = e.target.checked; })} /> Aprovado para uso neste flow</label>
      <button className="danger small" onClick={() => update((d) => {
        delete d.spec.mcp_servers![name];
        for (const a of Object.values(d.spec.agents)) a.tools = (a.tools ?? []).filter((t) => !t.startsWith(name + "."));
      })}>Remover MCP</button>
    </details>
  );
}

function FlowPanel({ doc, catalog, update }: { doc: Doc; catalog: Catalog; update: Update }) {
  const s = doc.spec;
  const [newMcp, setNewMcp] = useState("");
  const set = (fn: (s: FlowRaw) => void) => update((d) => fn(d.spec));
  const reserved = new Set(catalog.servers);
  return (
    <>
      <Field label="Título"><input value={s.title} onChange={(e) => set((x) => { x.title = e.target.value; })} /></Field>
      <Field label="Descrição">
        <textarea rows={3} value={s.description ?? ""} onChange={(e) => set((x) => { x.description = e.target.value; })} />
      </Field>
      <div className="grid2">
        <Field label="Modelo"><input list="models" value={s.model ?? ""} placeholder="por defeito"
          onChange={(e) => set((x) => { x.model = e.target.value || undefined; })} /></Field>
        <Field label="Máx. turnos"><input type="number" min={1} max={500} value={s.max_turns ?? 60}
          onChange={(e) => set((x) => { x.max_turns = Number(e.target.value) || undefined; })} /></Field>
      </div>
      <div className="grid2">
        <Field label="Entrada"><input value={s.input?.label ?? "Entrada"}
          onChange={(e) => set((x) => { x.input = { ...x.input, label: e.target.value }; })} /></Field>
        <Field label="Padrão da entrada" hint="Regex; é também o id da pasta em runs/">
          <input className="mono" value={s.input?.pattern ?? "^[A-Za-z0-9_-]{1,40}$"}
            onChange={(e) => set((x) => { x.input = { ...x.input, pattern: e.target.value }; })} /></Field>
      </div>
      <Field label="Tarefa" hint="Mensagem inicial; {input} é substituído pela entrada">
        <textarea rows={3} value={s.task ?? ""} onChange={(e) => set((x) => { x.task = e.target.value; })} />
      </Field>
      <Field label="Início">
        <select value={s.start} onChange={(e) => set((x) => { x.start = e.target.value; })}>
          {Object.keys(s.agents).map((a) => <option key={a}>{a}</option>)}
        </select>
      </Field>
      <Field label="Prompt do orquestrador" hint="A secção “Fluxo” é gerada a partir do grafo e acrescentada a este texto">
        <PromptEditor owner={s.orchestrator ?? {}} defaultPath="prompts/orchestrator.md" doc={doc} update={update}
          pick={(d) => (d.spec.orchestrator = d.spec.orchestrator ?? {})} />
      </Field>
      <h3>Escrita</h3>
      <Field label="Autonomia" hint="Edit/Write/Bash no workspace e http_write nos conectores. Os agentes podem sobrepor.">
        <select value={s.autonomy ?? "approve"} onChange={(e) => set((x) => { x.autonomy = e.target.value as FlowRaw["autonomy"]; })}>
          {Object.entries(AUTONOMY).map(([k, v]) => <option key={k} value={k}>{v}</option>)}
        </select>
      </Field>
      {s.autonomy === "auto" && <p className="field-err">Autónomo: as escritas correm sem te perguntar (ficam no audit). O hook continua a recusar comandos perigosos e caminhos fora do workspace.</p>}
      <label className="check">
        <input type="checkbox" checked={!!s.workspace} onChange={(e) => set((x) => {
          if (e.target.checked) x.workspace = { path: "", environment: "dev" }; else delete x.workspace;
        })} /> Workspace (repositório onde os agentes trabalham com ficheiros, git, testes)
      </label>
      {s.workspace && <>
        <Field label="Pasta" hint="Absoluta (ex.: D:\repos\app). Ferramentas workspace.* só funcionam aqui dentro.">
          <input className="mono" value={s.workspace.path} onChange={(e) => set((x) => { x.workspace = { ...x.workspace!, path: e.target.value }; })} />
        </Field>
        <div className="grid2">
          <Field label="Ambiente">
            <select value={s.workspace.environment ?? "dev"} onChange={(e) => set((x) => { x.workspace = { ...x.workspace!, environment: e.target.value as "dev" }; })}>
              <option>dev</option><option>staging</option><option>prod</option>
            </select>
          </Field>
          <Field label="Branches protegidas" hint="Sem push direto">
            <input value={(s.workspace.protected_branches ?? ["main", "master"]).join(", ")}
              onChange={(e) => set((x) => { x.workspace = { ...x.workspace!, protected_branches: csv(e.target.value) }; })} />
          </Field>
        </div>
      </>}
      <h3>MCPs externos</h3>
      <p className="muted small">Só ficam disponíveis com aprovação, só leitura e lista fechada de ferramentas.</p>
      {Object.entries(s.mcp_servers ?? {}).map(([n, m]) => <McpEditor key={n} name={n} m={m} update={update} />)}
      <div className="row">
        <input placeholder="nome_do_servidor" value={newMcp} onChange={(e) => setNewMcp(e.target.value)} />
        <button className="small" disabled={!SLUG.test(newMcp) || reserved.has(newMcp) || newMcp in (s.mcp_servers ?? {})}
          onClick={() => { set((x) => { x.mcp_servers = { ...x.mcp_servers, [newMcp]: { type: "stdio", command: "", tools: [] } }; }); setNewMcp(""); }}>
          + MCP</button>
      </div>
    </>
  );
}

export function Inspector(p: Props) {
  const { doc, sel } = p;
  let title = "Flow", body: ReactNode = <FlowPanel doc={doc} catalog={p.catalog} update={p.update} />;
  if (sel?.kind === "node" && sel.id in doc.spec.agents) {
    title = "Agente";
    body = <AgentPanel key={sel.id} id={sel.id} {...p} />;
  } else if (sel?.kind === "node" && sel.id in (doc.spec.gates ?? {})) {
    title = "Gate";
    body = <GatePanel key={sel.id} id={sel.id} {...p} />;
  } else if (sel?.kind === "edge" && doc.spec.edges?.[sel.index]) {
    title = "Ligação";
    body = <EdgePanel index={sel.index} doc={doc} update={p.update} />;
  }
  return (
    <aside className="inspector">
      <div className="panel-head">
        <h2>{title}</h2>
        {sel && <>
          <button className="small" onClick={() => p.select(null)}>Flow</button>
          <button className="danger small" disabled={sel.kind === "node" && sel.id === doc.spec.start}
            title={sel.kind === "node" && sel.id === doc.spec.start ? "Muda o início antes de apagar" : "Apagar (Delete)"}
            onClick={() => p.remove(sel)}>Apagar</button>
        </>}
      </div>
      <div className="panel-body">{body}</div>
      <datalist id="models">{p.catalog.models.map((m) => <option key={m} value={m} />)}</datalist>
    </aside>
  );
}

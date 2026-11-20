import { useState, type ReactNode } from "react";
import { CommitInput, Field, NumberInput } from "./fields";
import { Icon, ICON_IDS } from "./icons";
import { AgentInputsEditor, ExprInput, IOListEditor, exprRefs } from "./IOEditors";
import { artifactOwners, isTyped, renameNode, SLUG, nodeNames } from "./model";
import { useI18n } from "./i18n";
import type { AgentRaw, ArtifactRaw, Catalog, Doc, FlowRaw, McpRaw, Selection, Validation } from "./types";

type Update = (fn: (d: Doc) => void) => void;
interface Props {
  doc: Doc;
  sel: Selection;
  catalog: Catalog;
  val: Validation | null;
  update: Update;
  select: (s: Selection) => void;
  remove: (s: Selection) => void;
  testAgent?: (id: string) => void;
}

const AUTONOMY = ["approve", "approve_prod", "auto"] as const;

const lines = (s: string) => s.split("\n").map((x) => x.trim()).filter(Boolean);
const csv = (s: string) => s.split(",").map((x) => x.trim()).filter(Boolean);
const kv = (s: string, sep: string) =>
  Object.fromEntries(lines(s).map((l) => {
    const i = l.indexOf(sep);
    return i < 0 ? [l, ""] : [l.slice(0, i).trim(), l.slice(i + 1).trim()];
  }));
const unkv = (o: Record<string, string> | undefined, sep: string) =>
  Object.entries(o ?? {}).map(([k, v]) => `${k}${sep}${v}`).join("\n");

/** Inline prompt or in a prompts/*.md file (edited here and saved with the flow). */
function PromptEditor({ owner, defaultPath, doc, update, pick }: {
  owner: { prompt?: string | null; prompt_file?: string | null };
  defaultPath: string;
  doc: Doc;
  update: Update;
  pick: (d: Doc) => { prompt?: string | null; prompt_file?: string | null };
}) {
  const { t } = useI18n();
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
        })}>{t("insp.inline")}</button>
        <button className={file ? "on" : ""} onClick={() => update((d) => {
          const o = pick(d);
          if (o.prompt_file) return;
          const path = defaultPath;
          if (!(path in d.prompts)) d.prompts[path] = o.prompt ?? "";
          o.prompt_file = path;
          delete o.prompt;
        })}>{t("insp.file")}</button>
        {file && <code className="path">{file}</code>}
      </div>
      {missing && <span className="field-err">{t("insp.fileMissing")}</span>}
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
  const { t } = useI18n();
  const chosen = new Set(agent.tools ?? []);
  const groups: [string, { id: string; name: string; description: string; note?: string }[]][] = [];
  for (const s of catalog.servers)
    groups.push([s, catalog.tools.filter((x) => x.server === s).map((x) => ({
      ...x, note: x.writes_local_only ? t("insp.writesRunsOnly") : x.kind === "write" ? t("insp.write") : undefined,
    }))]);
  for (const [s, m] of Object.entries(spec.mcp_servers ?? {}))
    groups.push([t("insp.external", { name: s }), m.tools.map((n) => ({
      id: `${s}.${n}`, name: n, description: m.description ?? "",
      note: !m.approved ? t("insp.mcpNotApproved") : !m.read_only ? t("insp.mcpNotReadOnly") : undefined,
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
      {unknown.map((id) => (
        <label key={id} className="check bad">
          <input type="checkbox" checked onChange={() => toggle(id)} /> <span>{id}</span>
          <span className="small">{t("insp.notInCatalog")}</span>
        </label>
      ))}
    </div>
  );
}

function WritesEditor({ writes, catalog, set }: { writes: ArtifactRaw[]; catalog: Catalog; set: (w: ArtifactRaw[]) => void }) {
  const { t } = useI18n();
  const patch = (i: number, p: Partial<ArtifactRaw>) => set(writes.map((w, j) => (j === i ? { ...w, ...p } : w)));
  return (
    <div className="rows">
      {writes.map((w, i) => (
        <div key={i} className="row">
          <input value={w.name} placeholder="01_name.json" onChange={(e) => patch(i, { name: e.target.value })} />
          {w.name.endsWith(".json") && (
            <select value={w.contract ?? ""} onChange={(e) => patch(i, { contract: e.target.value || undefined })}>
              <option value="">{t("insp.noContract")}</option>
              {Object.keys(catalog.contracts).map((c) => <option key={c}>{c}</option>)}
            </select>
          )}
          {w.name.endsWith(".md") && (
            <label className="check inline" title={t("insp.draftTitle")}>
              <input type="checkbox" checked={!!w.draft} onChange={(e) => patch(i, { draft: e.target.checked || undefined })} />
              {t("insp.draft")}
            </label>
          )}
          <button className="icon" title={t("insp.remove")} onClick={() => set(writes.filter((_, j) => j !== i))}>×</button>
        </div>
      ))}
      <button className="small" onClick={() => set([...writes, { name: "" }])}>{t("insp.addArtifact")}</button>
    </div>
  );
}

function AgentPanel({ id, doc, catalog, update, select, testAgent, focus }: { id: string; focus?: string } & Omit<Props, "sel" | "remove" | "val">) {
  const { t } = useI18n();
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
          <button className="small" onClick={() => testAgent(id)} title={t("insp.testAgentTitle")}>{t("insp.testAgent")}</button>
          <button className="small" onClick={() => testAgent(`until:${id}`)} title={t("insp.runUntilTitle")}>{t("insp.runUntil")}</button>
        </div>)}
      <Field label={t("insp.name")} hint={t("insp.nameHint")}>
        <CommitInput value={id} pattern={SLUG} onCommit={(v) => {
          if (nodeNames(spec).includes(v)) return t("insp.nameTaken");
          update((d) => renameNode(d.spec, id, v));
          select({ kind: "node", id: v });
          return null;
        }} />
      </Field>
      <label className="check">
        <input type="checkbox" checked={spec.start === id} disabled={spec.start === id}
          onChange={() => update((d) => { d.spec.start = id; })} />
        {t("insp.isStart")}
      </label>
      {/* a div, not Field's <label>: a click on the text must not press the first icon */}
      <div className="field">
        <span className="field-label">{t("insp.icon")}</span>
        <div className="icon-grid" role="radiogroup" aria-label={t("insp.icon")}>
          {ICON_IDS.map((k) => (
            <button key={k} type="button" role="radio" aria-checked={(a.icon || "robot") === k} title={k}
              className={`icon-pick${(a.icon || "robot") === k ? " on" : ""}`}
              onClick={() => setA((x) => { x.icon = k === "robot" ? undefined : k; })}>
              <Icon id={k} size={18} />
            </button>))}
          <input className="icon-emoji" maxLength={8} placeholder={t("insp.iconEmoji")} aria-label={t("insp.iconEmoji")}
            value={a.icon && !ICON_IDS.includes(a.icon) ? a.icon : ""}
            onChange={(e) => setA((x) => { x.icon = e.target.value.trim() || undefined; })} />
        </div>
        <span className="field-hint">{t("insp.iconHint")}</span>
      </div>
      <Field label={t("insp.description")} hint={t("insp.descriptionHint")}>
        <textarea rows={3} value={a.description} onChange={(e) => setA((x) => { x.description = e.target.value; })} />
      </Field>
      <Field label={t("insp.prompt")}>
        <PromptEditor owner={a} defaultPath={`prompts/${id}.md`} doc={doc} update={update}
          pick={(d) => d.spec.agents[id]} />
      </Field>
      <h3>{t("io.inputs")}</h3>
      <p className="muted small">{t("io.agentInputsHint")}</p>
      <AgentInputsEditor id={id} doc={doc} update={update} focus={focus} />
      <h3>{t("io.outputs")}</h3>
      <p className="muted small">{t("io.agentOutputsHint")}</p>
      <IOListEditor root={id} doc={doc} catalog={catalog} update={update} />
      {spec.execution === "graph" && (
        <div className="grid2">
          <Field label={t("io.join")} hint={t("io.joinHint")}>
            <select value={a.join ?? "all"} onChange={(e) => setA((x) => { x.join = e.target.value as "all" | "any"; })}>
              <option value="all">{t("io.joinAll")}</option>
              <option value="any">{t("io.joinAny")}</option>
            </select>
          </Field>
          <Field label={t("io.maxRuns")} hint={t("io.maxRunsHint")}>
            <NumberInput value={a.max_runs ?? 3} min={1} max={20} step={1}
              onChange={(v) => setA((x) => { if (v === null) delete x.max_runs; else x.max_runs = v; })} />
          </Field>
        </div>)}
      <div className="grid2">
        <Field label={t("insp.model")}>
          <input list="models" value={a.model ?? ""} placeholder={t("insp.modelFromFlow")}
            onChange={(e) => setA((x) => { x.model = e.target.value || undefined; })} />
        </Field>
        <Field label={t("insp.maxTurns")}>
          <input type="number" min={1} max={200} value={a.max_turns ?? ""} placeholder="—"
            onChange={(e) => setA((x) => { x.max_turns = e.target.value ? Number(e.target.value) : undefined; })} />
        </Field>
      </div>
      <Field label={t("insp.tools")} hint={t("insp.toolsHint")}>
        <ToolPicker agent={a} spec={spec} catalog={catalog} set={(t) => setA((x) => { x.tools = t; })} />
      </Field>
      <Field label={t("insp.connectors")} hint={t("insp.connectorsHint")}>
        <div className="tools">
          {Object.keys(catalog.connections).length === 0 && <span className="muted small">{t("insp.noConnectors")}</span>}
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
                <span>{c}</span><span className="muted small">{info ? `${info.type} · ${info.environment}` : t("insp.notConfiguredHere")}</span>
              </label>);
          })}
        </div>
      </Field>
      <Field label={t("insp.writeConnectors")} hint={t("insp.writeConnectorsHint")}>
        <div className="tools">
          {Object.entries(catalog.connections).filter(([, i]) => i.access === "write").length === 0 &&
            <span className="muted small">{t("insp.noWriteConnectors")}</span>}
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
                <span>{c}</span><span className="muted small">{info ? `${info.type} · ${info.environment}` : t("insp.notConfigured")}</span>
              </label>);
          })}
        </div>
      </Field>
      <Field label={t("insp.agentAutonomy")} hint={t("insp.agentAutonomyHint")}>
        <select value={a.autonomy ?? ""} onChange={(e) => setA((x) => { x.autonomy = (e.target.value || undefined) as AgentRaw["autonomy"]; })}>
          <option value="">{t("insp.flowAutonomyOption", { value: t(`autonomy.${spec.autonomy ?? "approve"}`) })}</option>
          {AUTONOMY.map((k) => <option key={k} value={k}>{t(`autonomy.${k}`)}</option>)}
        </select>
      </Field>
      <Field label={t("insp.reads")}>
        <div className="tools">
          {readable.length === 0 && <span className="muted small">{t("insp.noOtherWriters")}</span>}
          {[...readable, ...extraReads].map((r) => (
            <label key={r} className={`check${extraReads.includes(r) ? " bad" : ""}`}>
              <input type="checkbox" checked={(a.reads ?? []).includes(r)} onChange={(e) => setA((x) => {
                const s = new Set(x.reads ?? []);
                if (e.target.checked) s.add(r); else s.delete(r);
                x.reads = [...readable, ...extraReads].filter((n) => s.has(n));
              })} />
              <span>{r}</span><span className="muted small">{owners.get(r) ?? t("insp.nobodyWrites")}</span>
            </label>
          ))}
        </div>
      </Field>
      <Field label={t("insp.writes")} hint={t("insp.writesHint")}>
        <WritesEditor writes={a.writes ?? []} catalog={catalog} set={(w) => setA((x) => { x.writes = w; })} />
      </Field>
      <Field label={t("insp.skills")} hint={t("insp.skillsHint")}>
        <input value={(a.skills ?? []).join(", ")} onChange={(e) => setA((x) => { x.skills = csv(e.target.value); })} />
      </Field>
    </>
  );
}

function GatePanel({ id, doc, update, select }: { id: string } & Omit<Props, "sel" | "remove" | "catalog" | "val">) {
  const { t } = useI18n();
  const g = doc.spec.gates![id];
  return (
    <>
      <Field label={t("insp.name")}>
        <CommitInput value={id} pattern={SLUG} onCommit={(v) => {
          if (nodeNames(doc.spec).includes(v)) return t("insp.nameTaken");
          update((d) => renameNode(d.spec, id, v));
          select({ kind: "node", id: v });
          return null;
        }} />
      </Field>
      <Field label={t("insp.gateKind")} hint={t("insp.gateKindHint")}>
        <select value={g.kind} onChange={(e) => update((d) => { d.spec.gates![id].kind = e.target.value as "human" | "stop"; })}>
          <option value="human">{t("node.human")}</option>
          <option value="stop">{t("node.stop")}</option>
        </select>
      </Field>
      <Field label={t("insp.gateLabel")}>
        <input value={g.label} onChange={(e) => update((d) => { d.spec.gates![id].label = e.target.value; })} />
      </Field>
    </>
  );
}

function EdgePanel({ index, doc, val, update }: { index: number; doc: Doc; val: Validation | null; update: Update }) {
  const { t } = useI18n();
  const e = doc.spec.edges![index];
  if (!e) return null;
  const tag = `edge ${e.from}→${e.to}: `;
  const errors = (val?.errors ?? []).filter((x) => x.msg.startsWith(tag)).map((x) => x.msg.slice(tag.length));
  return (
    <>
      <p className="edge-title"><code>{e.from}</code> → <code>{e.to}</code></p>
      <div className="field">
        <span className="field-label">{t("insp.expression")}</span>
        <span className="field-hint">{t("insp.expressionHint")}</span>
        <ExprInput value={e.if ?? ""} refs={exprRefs(doc.spec, e.from)} errors={errors}
          onChange={(v) => update((d) => { d.spec.edges![index].if = v; })} />
      </div>
      <Field label={t("insp.condition")} hint={doc.spec.execution === "graph" ? t("insp.conditionHintGraph") : t("insp.conditionHint")}>
        <textarea rows={3} value={e.when ?? ""} onChange={(ev) => update((d) => { d.spec.edges![index].when = ev.target.value; })} />
      </Field>
      <p className="muted small">{t("insp.bothHint")}</p>
    </>
  );
}

function McpEditor({ name, m, update }: { name: string; m: McpRaw; update: Update }) {
  const { t } = useI18n();
  const set = (fn: (m: McpRaw) => void) => update((d) => fn(d.spec.mcp_servers![name]));
  const remote = m.type !== "stdio";
  return (
    <details className="mcp" open>
      <summary>
        <b>{name}</b> <span className="muted">{m.type}</span>
        {!(m.approved && m.read_only) && <span className="badge warn">{t("insp.blocked")}</span>}
      </summary>
      <div className="grid2">
        <Field label={t("insp.mcpType")}>
          <select value={m.type} onChange={(e) => set((x) => { x.type = e.target.value as McpRaw["type"]; })}>
            <option>stdio</option><option>http</option><option>sse</option>
          </select>
        </Field>
        <Field label={t("insp.description")}>
          <input value={m.description ?? ""} onChange={(e) => set((x) => { x.description = e.target.value; })} />
        </Field>
      </div>
      {remote ? (
        <>
          <Field label={t("insp.url")}><input value={m.url ?? ""} onChange={(e) => set((x) => { x.url = e.target.value; })} /></Field>
          <Field label={t("insp.headers")} hint={t("insp.headersHint")}>
            <textarea className="mono" rows={2} defaultValue={unkv(m.headers, ": ")}
              onBlur={(e) => set((x) => { x.headers = kv(e.target.value, ":"); })} />
          </Field>
        </>
      ) : (
        <>
          <Field label={t("insp.command")}><input value={m.command ?? ""} onChange={(e) => set((x) => { x.command = e.target.value; })} /></Field>
          <Field label={t("insp.args")} hint={t("insp.argsHint")}>
            <textarea className="mono" rows={2} defaultValue={(m.args ?? []).join("\n")}
              onBlur={(e) => set((x) => { x.args = lines(e.target.value); })} />
          </Field>
          <Field label={t("insp.env")} hint={t("insp.envHint")}>
            <textarea className="mono" rows={2} defaultValue={unkv(m.env, "=")}
              onBlur={(e) => set((x) => { x.env = kv(e.target.value, "="); })} />
          </Field>
        </>
      )}
      <Field label={t("insp.allowedTools")} hint={t("insp.allowedToolsHint")}>
        <input defaultValue={m.tools.join(", ")} onBlur={(e) => set((x) => { x.tools = csv(e.target.value); })} />
      </Field>
      <label className="check"><input type="checkbox" checked={!!m.read_only}
        onChange={(e) => set((x) => { x.read_only = e.target.checked; })} /> {t("insp.readOnly")}</label>
      <label className="check"><input type="checkbox" checked={!!m.approved}
        onChange={(e) => set((x) => { x.approved = e.target.checked; })} /> {t("insp.approved")}</label>
      <button className="danger small" onClick={() => update((d) => {
        delete d.spec.mcp_servers![name];
        for (const a of Object.values(d.spec.agents)) a.tools = (a.tools ?? []).filter((t) => !t.startsWith(name + "."));
      })}>{t("insp.removeMcp")}</button>
    </details>
  );
}

function FlowPanel({ doc, catalog, update }: { doc: Doc; catalog: Catalog; update: Update }) {
  const { t } = useI18n();
  const s = doc.spec;
  const [newMcp, setNewMcp] = useState("");
  const set = (fn: (s: FlowRaw) => void) => update((d) => fn(d.spec));
  const reserved = new Set(catalog.servers);
  return (
    <>
      <Field label={t("insp.title")}><input value={s.title} onChange={(e) => set((x) => { x.title = e.target.value; })} /></Field>
      <Field label={t("insp.description")}>
        <textarea rows={3} value={s.description ?? ""} onChange={(e) => set((x) => { x.description = e.target.value; })} />
      </Field>
      <div className="grid2">
        <Field label={t("insp.model")}><input list="models" value={s.model ?? ""} placeholder={t("insp.modelDefault")}
          onChange={(e) => set((x) => { x.model = e.target.value || undefined; })} /></Field>
        <Field label={t("insp.maxTurns")}><input type="number" min={1} max={500} value={s.max_turns ?? 60}
          onChange={(e) => set((x) => { x.max_turns = Number(e.target.value) || undefined; })} /></Field>
      </div>
      <Field label={t("insp.execution")} hint={t(s.execution === "graph" ? "insp.executionGraphHint" : "insp.executionOrchHint")}>
        <select value={s.execution ?? "orchestrator"} onChange={(e) => set((x) => { x.execution = e.target.value as FlowRaw["execution"]; })}>
          <option value="orchestrator">{t("insp.execOrchestrator")}</option>
          <option value="graph">{t("insp.execGraph")}</option>
        </select>
      </Field>
      {s.execution === "graph" && (
        <Field label={t("insp.maxParallel")} hint={t("insp.maxParallelHint")}>
          <NumberInput value={s.max_parallel ?? 4} min={1} max={16} step={1}
            onChange={(v) => set((x) => { if (v === null) delete x.max_parallel; else x.max_parallel = v; })} />
        </Field>)}
      <h3>{t("io.flowInputs")}</h3>
      {isTyped(s)
        ? <p className="muted small">{t("io.flowInputsHint")}</p>
        : <>
          <p className="muted small">{t("io.legacyHint")}</p>
          <div className="grid2">
            <Field label={t("insp.input")}><input value={s.input?.label ?? t("insp.inputDefault")}
              onChange={(e) => set((x) => { x.input = { ...x.input, label: e.target.value }; })} /></Field>
            <Field label={t("insp.inputPattern")} hint={t("insp.inputPatternHint")}>
              <input className="mono" value={s.input?.pattern ?? "^[A-Za-z0-9_-]{1,40}$"}
                onChange={(e) => set((x) => { x.input = { ...x.input, pattern: e.target.value }; })} /></Field>
          </div>
        </>}
      <IOListEditor root="input" doc={doc} catalog={catalog} update={update} />
      <Field label={t("insp.task")} hint={t(isTyped(s) ? "insp.taskHintTyped" : "insp.taskHint")}>
        <textarea rows={3} value={s.task ?? ""} onChange={(e) => set((x) => { x.task = e.target.value; })} />
      </Field>
      <Field label={t("insp.start")}>
        <select value={s.start} onChange={(e) => set((x) => { x.start = e.target.value; })}>
          {Object.keys(s.agents).map((a) => <option key={a}>{a}</option>)}
        </select>
      </Field>
      <Field label={t("insp.orchestratorPrompt")} hint={t("insp.orchestratorPromptHint")}>
        <PromptEditor owner={s.orchestrator ?? {}} defaultPath="prompts/orchestrator.md" doc={doc} update={update}
          pick={(d) => (d.spec.orchestrator = d.spec.orchestrator ?? {})} />
      </Field>
      <h3>{t("insp.writing")}</h3>
      <Field label={t("insp.autonomy")} hint={t("insp.autonomyHint")}>
        <select value={s.autonomy ?? "approve"} onChange={(e) => set((x) => { x.autonomy = e.target.value as FlowRaw["autonomy"]; })}>
          {AUTONOMY.map((k) => <option key={k} value={k}>{t(`autonomy.${k}`)}</option>)}
        </select>
      </Field>
      {s.autonomy === "auto" && <p className="field-err">{t("insp.autoWarning")}</p>}
      <label className="check">
        <input type="checkbox" checked={!!s.workspace} onChange={(e) => set((x) => {
          if (e.target.checked) x.workspace = { path: "", environment: "dev" }; else delete x.workspace;
        })} /> {t("insp.workspace")}
      </label>
      {s.workspace && <>
        <Field label={t("insp.folder")} hint={t("insp.folderHint")}>
          <input className="mono" value={s.workspace.path} onChange={(e) => set((x) => { x.workspace = { ...x.workspace!, path: e.target.value }; })} />
        </Field>
        <div className="grid2">
          <Field label={t("insp.environment")}>
            <select value={s.workspace.environment ?? "dev"} onChange={(e) => set((x) => { x.workspace = { ...x.workspace!, environment: e.target.value as "dev" }; })}>
              <option>dev</option><option>staging</option><option>prod</option>
            </select>
          </Field>
          <Field label={t("insp.protectedBranches")} hint={t("insp.protectedBranchesHint")}>
            <input value={(s.workspace.protected_branches ?? ["main", "master"]).join(", ")}
              onChange={(e) => set((x) => { x.workspace = { ...x.workspace!, protected_branches: csv(e.target.value) }; })} />
          </Field>
        </div>
      </>}
      <h3>{t("insp.externalMcps")}</h3>
      <p className="muted small">{t("insp.externalMcpsHint")}</p>
      {Object.entries(s.mcp_servers ?? {}).map(([n, m]) => <McpEditor key={n} name={n} m={m} update={update} />)}
      <div className="row">
        <input placeholder={t("insp.serverName")} value={newMcp} onChange={(e) => setNewMcp(e.target.value)} />
        <button className="small" disabled={!SLUG.test(newMcp) || reserved.has(newMcp) || newMcp in (s.mcp_servers ?? {})}
          onClick={() => { set((x) => { x.mcp_servers = { ...x.mcp_servers, [newMcp]: { type: "stdio", command: "", tools: [] } }; }); setNewMcp(""); }}>
          + MCP</button>
      </div>
    </>
  );
}

export function Inspector(p: Props) {
  const { t } = useI18n();
  const { doc, sel } = p;
  let title = t("insp.flow"), body: ReactNode = <FlowPanel doc={doc} catalog={p.catalog} update={p.update} />;
  if (sel?.kind === "node" && sel.id in doc.spec.agents) {
    title = t("insp.agent");
    body = <AgentPanel key={sel.id} id={sel.id} {...p} />;
  } else if (sel?.kind === "data" && sel.agent in doc.spec.agents) {
    const from = doc.spec.agents[sel.agent].inputs?.[sel.input]?.from;
    title = t("insp.dataLink");
    body = <>
      <p className="edge-title"><code>{from ?? "—"}</code> → <code>{sel.agent}.{sel.input}</code></p>
      <AgentPanel key={sel.agent} id={sel.agent} focus={sel.input} {...p} />
    </>;
  } else if (sel?.kind === "node" && sel.id in (doc.spec.gates ?? {})) {
    title = t("insp.gate");
    body = <GatePanel key={sel.id} id={sel.id} {...p} />;
  } else if (sel?.kind === "edge" && doc.spec.edges?.[sel.index]) {
    title = t("insp.edge");
    body = <EdgePanel index={sel.index} doc={doc} val={p.val} update={p.update} />;
  }
  return (
    <aside className="inspector">
      <div className="panel-head">
        <h2>{title}</h2>
        {sel && <>
          <button className="small" onClick={() => p.select(null)}>{t("insp.flow")}</button>
          <button className="danger small" disabled={sel.kind === "node" && sel.id === doc.spec.start}
            title={sel.kind === "node" && sel.id === doc.spec.start ? t("insp.changeStartFirst")
              : sel.kind === "data" ? t("insp.deleteLinkTitle") : t("insp.deleteTitle")}
            onClick={() => p.remove(sel)}>{t("insp.delete")}</button>
        </>}
      </div>
      <div className="panel-body">{body}</div>
      <datalist id="models">{p.catalog.models.map((m) => <option key={m} value={m} />)}</datalist>
    </aside>
  );
}

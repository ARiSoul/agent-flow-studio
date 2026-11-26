import { useEffect, useRef, useState, type ReactNode } from "react";
import { api } from "./api";
import { CommitInput, Field, NumberInput } from "./fields";
import { Icon, ICON_IDS } from "./icons";
import { AgentInputsEditor, ExprInput, IOListEditor, exprRefs } from "./IOEditors";
import { artifactOwners, isTyped, renameNode, SLUG, nodeNames, uniqueName } from "./model";
import { useI18n } from "./i18n";
import { localizeParam, localizeServer } from "./i18n/server";
import type { AgentRaw, ArtifactRaw, Catalog, Doc, FlowRaw, GateRaw, McpRaw, Selection, Validation } from "./types";

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
  flows?: string[];  // the other flows of this workspace (subflow nodes)
  library?: {
    state: (id: string) => string; has: (lib: string) => boolean;
    update: (id: string) => void; detach: (id: string) => void; save: (id: string) => void;
  };
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
/** The workspace's variables and roots as buttons that insert {vars.x} / {roots.x} at the cursor. */
function VarChips({ catalog, onInsert }: { catalog?: Catalog; onInsert: (text: string) => void }) {
  const { t } = useI18n();
  const ws = catalog?.workspace;
  const items = [...(ws?.vars ?? []).map((v) => `{vars.${v}}`), ...(ws?.roots ?? []).map((r) => `{roots.${r}}`)];
  if (!items.length) return null;
  return (
    <div className="var-chips" role="group" aria-label={t("insp.vars")}>
      <span className="muted small" title={t("insp.varsHint")}>{t("insp.vars")}</span>
      {items.map((x) => <button key={x} type="button" className="chip mono" title={t("insp.varsInsert")}
        onMouseDown={(e) => e.preventDefault()} onClick={() => onInsert(x)}>{x}</button>)}
    </div>
  );
}

/** Inserts text at the cursor of a text field (or at the end). */
function insertAt(el: HTMLTextAreaElement | HTMLInputElement | null, text: string, add: string): string {
  const a = el?.selectionStart ?? text.length, b = el?.selectionEnd ?? text.length;
  return text.slice(0, a) + add + text.slice(b);
}

function PromptEditor({ owner, defaultPath, doc, update, pick, catalog }: {
  owner: { prompt?: string | null; prompt_file?: string | null };
  defaultPath: string;
  doc: Doc;
  update: Update;
  pick: (d: Doc) => { prompt?: string | null; prompt_file?: string | null };
  catalog?: Catalog;
}) {
  const { t } = useI18n();
  const ref = useRef<HTMLTextAreaElement>(null);
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
      <textarea ref={ref} className="mono" rows={14} value={text} spellCheck={false}
        onChange={(e) => {
          const v = e.target.value;
          update((d) => {
            const o = pick(d);
            if (o.prompt_file) d.prompts[o.prompt_file] = v;
            else o.prompt = v;
          });
        }} />
      <VarChips catalog={catalog} onInsert={(x) => {
        const v = insertAt(ref.current, text, x);
        update((d) => { const o = pick(d); if (o.prompt_file) d.prompts[o.prompt_file] = v; else o.prompt = v; });
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

function AgentPanel({ id, doc, catalog, update, select, testAgent, focus, library, val, flows }: { id: string; focus?: string } & Omit<Props, "sel" | "remove">) {
  const { t } = useI18n();
  const spec = doc.spec;
  const a = spec.agents[id];
  const setA = (fn: (a: AgentRaw) => void) => update((d) => fn(d.spec.agents[id]));
  const owners = artifactOwners(spec);
  const readable = [...owners.entries()].filter(([, o]) => o !== id).map(([n]) => n);
  const extraReads = (a.reads ?? []).filter((r) => !readable.includes(r));
  // a connector used with direct requests (http_get/http_write); websites and "only these
  // actions" are used only through their actions (run_action, given implicitly)
  const direct = (n: string) => { const i = catalog.connections[n]; return !!i && i.type !== "website" && !i.actions_only; };
  const gives = (n: string, access: "read" | "write") => {
    const acts = Object.entries(catalog.connections[n]?.actions ?? {}).filter(([, x]) => access === "write" || x.access === "read");
    const parts = [];
    if (acts.length) parts.push(t("insp.connGivesActions", { actions: acts.map(([k]) => k).join(", ") }));
    if (direct(n)) parts.push(t(access === "write" ? "insp.connGivesWrite" : "insp.connGivesGet"));
    return parts.length ? parts.join(" · ") : t("insp.connGivesNothing");
  };
  return (
    <>
      {testAgent && (
        <div className="row test-row">
          <button className="small" onClick={() => testAgent(id)} title={t("insp.testAgentTitle")}>{t("insp.testAgent")}</button>
          <button className="small" onClick={() => testAgent(`until:${id}`)} title={t("insp.runUntilTitle")}>{t("insp.runUntil")}</button>
        </div>)}
      {library && (
        <div className="lib-box">
          {a.origin
            ? <span>{t("lib.from", { lib: val?.origins?.[id]?.title ?? a.origin.agent })} · <b>{library.state(id)}</b></span>
            : <span className="muted">{t("lib.notFrom")}</span>}
          <div className="row">
            {a.origin && <button className="small" disabled={!library.has(a.origin.agent)}
              onClick={() => library.update(id)}>{t("lib.update")}</button>}
            {a.origin && <button className="small" onClick={() => library.detach(id)}>{t("lib.detach")}</button>}
            <button className="small" onClick={() => library.save(id)}>{t("lib.save")}</button>
          </div>
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
      {a.action
        ? <ActionNodeEditor a={a} catalog={catalog} setA={setA} />
        : a.subflow
        ? <SubflowEditor id={id} a={a} flows={(flows ?? []).filter((f) => f !== doc.spec.name)} setA={setA} />
        : <Field label={t("insp.prompt")}>
          <PromptEditor owner={a} defaultPath={`prompts/${id}.md`} doc={doc} update={update}
            pick={(d) => d.spec.agents[id]} catalog={catalog} />
        </Field>}
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
      {!a.action && !a.subflow && <>{/* a model agent: model, tools, connectors, artifacts (not for an action or subflow node) */}
      <div className="grid2">
        <Field label={t("insp.model")}>
          <input list="models" value={a.model ?? ""} placeholder={t("insp.modelFromFlow")}
            onChange={(e) => setA((x) => { x.model = e.target.value || undefined; })} />
        </Field>
        <Field label={t("insp.agentMaxTurns")} hint={t("insp.agentMaxTurnsHint")}>
          <input type="number" min={1} max={200} value={a.max_turns ?? ""} placeholder="40"
            className={(a.max_turns ?? 3) < 3 ? "bad" : undefined}
            onChange={(e) => setA((x) => { x.max_turns = e.target.value ? Number(e.target.value) : undefined; })} />
          {(a.max_turns ?? 3) < 3 && <span className="field-err">{t("insp.agentMaxTurnsLow")}</span>}
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
                  // http_get only for connectors used with direct requests (not websites or
                  // "only these actions": those come with run_action)
                  const tools = new Set(x.tools ?? []);
                  if ([...s].some((n) => direct(n))) tools.add("connections.http_get"); else tools.delete("connections.http_get");
                  x.tools = [...(x.tools ?? []).filter((t) => tools.has(t)), ...[...tools].filter((t) => !(x.tools ?? []).includes(t))];
                })} />
                <span>{c}</span><span className="muted small">{info ? `${info.type} · ${info.environment}` : t("insp.notConfiguredHere")}</span>
                {info && (a.connections ?? []).includes(c) && <span className="conn-gives muted small">{gives(c, "read")}</span>}
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
                  if ([...s].some((n) => direct(n))) tools.add("connections.http_write"); else tools.delete("connections.http_write");
                  x.tools = [...(x.tools ?? []).filter((t) => tools.has(t)), ...[...tools].filter((t) => !(x.tools ?? []).includes(t))];
                })} />
                <span>{c}</span><span className="muted small">{info ? `${info.type} · ${info.environment}` : t("insp.notConfigured")}</span>
                {info && (a.write_connections ?? []).includes(c) && <span className="conn-gives muted small">{gives(c, "write")}</span>}
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
      </>}
    </>
  );
}

/** An action node: which connector action the studio runs, its parameters (texts with
 * {expression} parts) and where each output comes from in the result (`from`, `match`). */
function ActionNodeEditor({ a, catalog, setA }: { a: AgentRaw; catalog: Catalog; setA: (fn: (a: AgentRaw) => void) => void }) {
  const { t } = useI18n();
  const act = a.action!;
  const conn = catalog.connections[act.connector];
  const actions = Object.entries(conn?.actions ?? {}).filter(([, x]) => x.access === "read");
  const chosen = conn?.actions?.[act.name];
  const setAct = (fn: (x: NonNullable<AgentRaw["action"]>) => void) => setA((x) => { fn(x.action!); });
  const setOut = (o: string, key: "from" | "match", v: string) => setAct((x) => {
    const outs = (x.outputs = x.outputs ?? {});
    const m = (outs[o] = { ...outs[o] });
    if (v) m[key] = v; else delete m[key];
    if (!m.from && !m.match) delete outs[o];
  });
  return <>
    <p className="muted small">{t("insp.actionNodeHint")}</p>
    <div className="grid2">
      <Field label={t("insp.actionConnector")}>
        <select value={act.connector} onChange={(e) => setAct((x) => { x.connector = e.target.value; x.name = ""; x.params = {}; })}>
          {!conn && <option value={act.connector}>{act.connector || "—"}</option>}
          {Object.keys(catalog.connections).map((n) => <option key={n} value={n}>{n}</option>)}
        </select>
      </Field>
      <Field label={t("insp.actionName")} hint={chosen?.description}>
        <select value={act.name} onChange={(e) => setAct((x) => { x.name = e.target.value; x.params = {}; })}>
          <option value="">—</option>
          {actions.map(([n]) => <option key={n} value={n}>{n}</option>)}
        </select>
      </Field>
    </div>
    {(chosen?.params ?? []).length > 0 && (
      <div className="field">
        <span className="field-label">{t("insp.actionParams")}</span>
        <span className="field-hint">{t("insp.actionParamsHint")}</span>
        {(chosen?.params ?? []).map((p) => (
          <label key={p} className="field"><span className="field-label">{p}</span>
            <input className="mono" value={act.params?.[p] ?? ""} placeholder="{input.x}"
              onChange={(e) => setAct((x) => { const ps = (x.params = x.params ?? {}); if (e.target.value) ps[p] = e.target.value; else delete ps[p]; })} />
          </label>))}
      </div>)}
    <div className="field">
      <span className="field-label">{t("insp.actionOutputs")}</span>
      <span className="field-hint">{t("insp.actionOutputsHint")}</span>
      {Object.keys(a.outputs ?? {}).length === 0 && <span className="muted small">{t("insp.actionNoOutputs")}</span>}
      {Object.keys(a.outputs ?? {}).map((o) => (
        <div key={o} className="grid2">
          <label className="field"><span className="field-label">{o} ← {t("insp.actionFrom")}</span>
            <input className="mono" value={act.outputs?.[o]?.from ?? ""} placeholder={o}
              onChange={(e) => setOut(o, "from", e.target.value)} /></label>
          <label className="field"><span className="field-label">{t("insp.actionMatch")}</span>
            <input className="mono" value={act.outputs?.[o]?.match ?? ""} placeholder={"(\\d+)\\D*$"}
              onChange={(e) => setOut(o, "match", e.target.value)} /></label>
        </div>))}
    </div>
  </>;
}

/** A subflow node: which flow runs, what its inputs get ({expression} texts), and where each output
 * of this node comes from (an output of the child flow, or the message of its stop gate). */
function SubflowEditor({ id, a, flows, setA }: { id: string; a: AgentRaw; flows: string[];
  setA: (fn: (a: AgentRaw) => void) => void }) {
  const { t } = useI18n();
  const sub = a.subflow!;
  const [child, setChild] = useState<Doc["spec"] | null>(null);
  const [err, setErr] = useState<string | null>(null);
  useEffect(() => {
    setChild(null); setErr(null);
    if (!sub.flow) return;
    api.load(sub.flow).then((r) => setChild(r.spec)).catch((e) => setErr(String((e as Error).message ?? e)));
  }, [sub.flow]);
  const setSub = (fn: (x: NonNullable<AgentRaw["subflow"]>) => void) => setA((x) => { fn(x.subflow!); });
  const childOuts = child ? Object.entries(child.agents).flatMap(([an, ca]) =>
    Object.entries(ca.outputs ?? {}).map(([on, o]) => ({ ref: `${an}.${on}`, name: on, type: o.type ?? "text" }))) : [];
  const humans = child ? Object.entries(child.gates ?? {}).filter(([, g]) => g.kind === "human").map(([n]) => n) : [];
  const takeAll = () => setA((x) => {
    const outs = (x.outputs = x.outputs ?? {});
    const map = (x.subflow!.outputs = x.subflow!.outputs ?? {});
    const mapped = new Set(Object.values(map).map((m) => m.from));
    for (const o of childOuts) {
      if (mapped.has(o.ref)) continue;
      let n = o.name, i = 2;
      while (n in outs) n = `${o.name}_${i++}`;
      outs[n] = { type: o.type as "text" };
      map[n] = { from: o.ref };
    }
  });
  return <>
    <p className="muted small">{t("insp.subflowHint")}</p>
    <Field label={t("insp.subflowFlow")}>
      <select value={sub.flow} onChange={(e) => setSub((x) => { x.flow = e.target.value; x.inputs = {}; x.outputs = {}; })}>
        {!flows.includes(sub.flow) && <option value={sub.flow}>{sub.flow || "—"}</option>}
        {flows.map((f) => <option key={f} value={f}>{f}</option>)}
      </select>
    </Field>
    {err && <span className="field-err">{err}</span>}
    {child && child.execution !== "graph" && <span className="field-err">{t("insp.subflowNotGraph")}</span>}
    {humans.length > 0 && <span className="field-err">{t("insp.subflowHuman", { gates: humans.join(", ") })}</span>}
    {child && Object.keys(child.inputs ?? {}).length > 0 && (
      <div className="field">
        <span className="field-label">{t("insp.subflowInputs")}</span>
        <span className="field-hint">{t("insp.subflowInputsHint")}</span>
        {Object.entries(child.inputs ?? {}).map(([n, x]) => (
          <label key={n} className="field"><span className="field-label">{n} <span className="muted">· {x.type ?? "text"}{x.required === false ? ` · ${t("app.optional")}` : ""}</span></span>
            <input className="mono" value={sub.inputs?.[n] ?? ""} placeholder={`{input.${n}}`}
              onChange={(e) => setSub((s2) => { const m = (s2.inputs = s2.inputs ?? {}); if (e.target.value) m[n] = e.target.value; else delete m[n]; })} />
          </label>))}
      </div>)}
    <div className="field">
      <span className="field-label">{t("insp.subflowOutputs")}</span>
      <span className="field-hint">{t("insp.subflowOutputsHint")}</span>
      {Object.keys(a.outputs ?? {}).map((o) => (
        <label key={o} className="field"><span className="field-label">{o} ← </span>
          <select value={sub.outputs?.[o]?.from ?? ""}
            onChange={(e) => setSub((s2) => { const m = (s2.outputs = s2.outputs ?? {}); if (e.target.value) m[o] = { from: e.target.value }; else delete m[o]; })}>
            <option value="">—</option>
            {childOuts.map((c) => <option key={c.ref} value={c.ref}>{c.ref} · {c.type}</option>)}
            <option value="message">{t("insp.subflowMessage")}</option>
          </select>
        </label>))}
      <button className="small" disabled={!childOuts.length} onClick={takeAll}>{t("insp.subflowTakeAll")}</button>
      <span className="muted small">{t("insp.subflowRun", { id })}</span>
    </div>
  </>;
}

function GatePanel({ id, doc, update, select, val }: { id: string } & Omit<Props, "sel" | "remove" | "catalog">) {
  const { t, lang } = useI18n();
  const g = doc.spec.gates![id];
  const mine = (x: { params?: Record<string, unknown> }) => x.params?.gate === `'${id}'`;
  const errs = val?.errors ?? [];
  // "gate 'x': message: <reason>": the reason, in the UI language
  const msgErrors = errs.filter((x) => mine(x) && (x.code === "spec.message_reason" || x.code === "spec.message_only_stop"))
    .map((x) => (x.code === "spec.message_reason" ? localizeParam(x.params?.reason, lang) : null) ?? localizeServer(x, x.msg, lang));
  const decErrors = errs.filter((x) => mine(x) && x.code?.startsWith("spec.decision_")).map((x) => localizeServer(x, x.msg, lang));
  const setMessage = (v: string) => update((d) => { const x = d.spec.gates![id]; if (v) x.message = v; else delete x.message; });
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
        <select value={g.kind} onChange={(e) => update((d) => { d.spec.gates![id].kind = e.target.value as GateRaw["kind"]; })}>
          <option value="human">{t("node.human")}</option>
          <option value="stop">{t("node.stop")}</option>
          <option value="decision">{t("node.decision")}</option>
        </select>
      </Field>
      <Field label={g.kind === "decision" ? t("insp.decisionQuestion") : t("insp.gateLabel")}>
        <input value={g.label} onChange={(e) => update((d) => { d.spec.gates![id].label = e.target.value; })} />
      </Field>
      {g.kind === "stop" && (
        <div className="field">
          <span className="field-label">{t("insp.stopMessage")}</span>
          <span className="field-hint">{t("insp.stopMessageHint")}</span>
          <textarea rows={3} className={msgErrors.length ? "bad" : undefined} value={g.message ?? ""}
            placeholder={t("insp.stopMessagePlaceholder")} onChange={(e) => setMessage(e.target.value)} />
          {msgErrors.map((m, k) => <span key={k} className="field-err">{m}</span>)}
          <div className="refs">
            {exprRefs(doc.spec, "").map((r) => (
              <button key={r} className="small ref" title={t("insp.insert")}
                onClick={() => setMessage(`${g.message ?? ""}{${r}}`)}>{r}</button>))}
          </div>
        </div>)}
      {g.kind === "decision" && (
        <BranchesEditor id={id} doc={doc} update={update} select={select} val={val} />)}
      {decErrors.map((m, k) => <span key={k} className="field-err">{m}</span>)}
    </>
  );
}

/** The branches of a decision, each one a row: its label, its condition (checked in order; empty
 * = "otherwise"), where it goes and, for a stop, the message shown at the end. "+ Branch" adds a
 * branch with a new stop, so a decision is set up here without drawing links by hand. */
function BranchesEditor({ id, doc, update, select, val }: { id: string; doc: Doc; update: Update;
  select: Props["select"]; val: Validation | null }) {
  const { t, lang } = useI18n();
  const spec = doc.spec;
  const branches = (spec.edges ?? []).map((e, i) => ({ e, i })).filter(({ e }) => e.from === id);
  const [active, setActive] = useState<number | null>(null);  // the branch the reference chips go to
  const swap = (a: number, b: number) => update((d) => { const es = d.spec.edges!; [es[a], es[b]] = [es[b], es[a]]; });
  const setEdge = (i: number, key: "if" | "label", v: string) => update((d) => {
    const e = d.spec.edges![i]; if (v) e[key] = v; else delete e[key];
  });
  const setMessage = (gate: string, v: string) => update((d) => {
    const x = d.spec.gates![gate]; if (v) x.message = v; else delete x.message;
  });
  const errorsOf = (e: { from: string; to: string }) => (val?.errors ?? [])
    .filter((x) => x.code === "spec.edge_reason" && x.params?.edge === `${e.from}→${e.to}`)
    .map((x) => localizeParam(x.params?.reason, lang) ?? x.msg);
  const msgErrorsOf = (gate: string) => (val?.errors ?? [])
    .filter((x) => x.code === "spec.message_reason" && x.params?.gate === `'${gate}'`)
    .map((x) => localizeParam(x.params?.reason, lang) ?? x.msg);
  // a new branch: a new stop below the decision, linked with an empty condition to fill in
  const addBranch = () => update((d) => {
    const stop = uniqueName(d.spec, "stop");
    d.spec.gates = { ...d.spec.gates, [stop]: { kind: "stop", label: t("node.stop") } };
    const at = d.spec.layout?.[id] ?? { x: 0, y: 0 };
    const n = (d.spec.edges ?? []).filter((e) => e.from === id).length;
    d.spec.layout = { ...d.spec.layout, [stop]: { x: Math.round(at.x - 260 + n * 260), y: Math.round(at.y + 170) } };
    // before the "otherwise" branch (a branch after it would never be chosen)
    const es = [...(d.spec.edges ?? [])];
    const other = es.findIndex((e) => e.from === id && !e.if?.trim());
    es.splice(other >= 0 ? other : es.length, 0, { from: id, to: stop, if: "" });
    d.spec.edges = es;
  });
  const targets = [...Object.keys(spec.agents), ...Object.keys(spec.gates ?? {})].filter((n) => n !== id);
  const setTarget = (i: number, to: string) => update((d) => { d.spec.edges![i].to = to; });
  const refs = exprRefs(spec, "");
  return (
    <div className="field">
      <span className="field-label">{t("insp.branches")}</span>
      <span className="field-hint">{t("insp.branchesHint2")}</span>
      {branches.length === 0 && <span className="muted small">{t("insp.noBranches")}</span>}
      <ol className="branch-rows">
        {branches.map(({ e, i }, k) => {
          const stop = spec.gates?.[e.to]?.kind === "stop" ? spec.gates[e.to] : null;
          const errs = errorsOf(e);
          return (
            <li key={i} className="branch-row">
              <div className="row">
                <input className="branch-label" value={e.label ?? ""} maxLength={40} placeholder={t("insp.edgeLabelPlaceholder")}
                  title={t("insp.edgeLabel")} onChange={(ev) => setEdge(i, "label", ev.target.value)} />
                <span className="muted small">→</span>
                <select value={e.to} title={t("insp.branchTarget")} onChange={(ev) => setTarget(i, ev.target.value)}>
                  {targets.map((n) => <option key={n} value={n}>{n}</option>)}
                </select>
                <span className="branch-move">
                  <button className="icon" disabled={k === 0} title={t("insp.moveUp")} onClick={() => swap(i, branches[k - 1].i)}>↑</button>
                  <button className="icon" disabled={k === branches.length - 1} title={t("insp.moveDown")} onClick={() => swap(i, branches[k + 1].i)}>↓</button>
                  <button className="icon" title={t("insp.branchOpen")} onClick={() => select({ kind: "edge", index: i })}>⋯</button>
                </span>
              </div>
              <input className={`mono${errs.length ? " bad" : ""}`} value={e.if ?? ""} placeholder={t("insp.branchIfPlaceholder")}
                aria-label={t("insp.expression")} onFocus={() => setActive(i)} onChange={(ev) => setEdge(i, "if", ev.target.value)} />
              {errs.map((m, j) => <span key={j} className="field-err">{m}</span>)}
              {stop && <>
                <textarea rows={2} value={stop.message ?? ""} placeholder={t("insp.stopMessagePlaceholder")}
                  aria-label={t("insp.stopMessage")} onFocus={() => setActive(-1 - i)}
                  onChange={(ev) => setMessage(e.to, ev.target.value)} />
                {msgErrorsOf(e.to).map((m, j) => <span key={j} className="field-err">{m}</span>)}
              </>}
            </li>);
        })}
      </ol>
      <div className="row"><button className="small" onClick={addBranch}>{t("insp.branchAdd")}</button></div>
      {active !== null && (
        <div className="refs">
          {refs.map((r) => (
            <button key={r} className="small ref" title={t("insp.insert")} onClick={() => {
              const i = active >= 0 ? active : -1 - active;
              const e = spec.edges?.[i];
              if (!e) return;
              if (active >= 0) setEdge(i, "if", `${e.if ? `${e.if} ` : ""}${r}`);
              else setMessage(e.to, `${spec.gates?.[e.to]?.message ?? ""}{${r}}`);
            }}>{r}</button>))}
        </div>)}
    </div>
  );
}

function EdgePanel({ index, doc, val, update }: { index: number; doc: Doc; val: Validation | null; update: Update }) {
  const { t, lang } = useI18n();
  const e = doc.spec.edges![index];
  if (!e) return null;
  const tag = `edge ${e.from}→${e.to}: `;
  const fromDecision = doc.spec.gates?.[e.from]?.kind === "decision";  // a branch: `if` only
  // "edge a→b: <reason>": show the reason, in the UI language when it has a code
  const errors = (val?.errors ?? []).filter((x) => x.msg.startsWith(tag)).map((x) =>
    (x.code === "spec.edge_reason" ? localizeParam(x.params?.reason, lang) : null) ?? x.msg.slice(tag.length));
  return (
    <>
      <p className="edge-title"><code>{e.from}</code> → <code>{e.to}</code></p>
      <Field label={t("insp.edgeLabel")} hint={t(fromDecision ? "insp.edgeLabelHintBranch" : "insp.edgeLabelHint")}>
        <input value={e.label ?? ""} maxLength={40} placeholder={fromDecision ? t("insp.edgeLabelPlaceholder") : ""}
          onChange={(ev) => update((d) => { const x = d.spec.edges![index]; if (ev.target.value) x.label = ev.target.value; else delete x.label; })} />
      </Field>
      <div className="field">
        <span className="field-label">{t("insp.expression")}</span>
        <span className="field-hint">{t(fromDecision ? "insp.expressionHintBranch" : "insp.expressionHint")}</span>
        <ExprInput value={e.if ?? ""} refs={exprRefs(doc.spec, e.from)} errors={errors}
          onChange={(v) => update((d) => { d.spec.edges![index].if = v; })} />
      </div>
      {!fromDecision && <>
        <Field label={t("insp.condition")} hint={doc.spec.execution === "graph" ? t("insp.conditionHintGraph") : t("insp.conditionHint")}>
          <textarea rows={3} value={e.when ?? ""} onChange={(ev) => update((d) => { d.spec.edges![index].when = ev.target.value; })} />
        </Field>
        <p className="muted small">{t("insp.bothHint")}</p>
      </>}
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
  const taskRef = useRef<HTMLTextAreaElement>(null);
  const roots = catalog.workspace?.roots ?? [];
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
      {s.execution === "graph" && (
        <Field label={t("insp.retry")} hint={`${t("insp.retryHint")}${(() => {
          const writers = Object.entries(s.agents).filter(([, a]) => (a.tools ?? []).some((x) => WRITE_TOOL_NAMES.includes(x))).map(([n]) => n);
          return writers.length ? ` ${t("insp.retryWriters", { names: writers.join(", ") })}` : "";
        })()}`}>
          <div className="row">
            <NumberInput value={s.retry?.transient ?? 2} min={0} max={5} step={1} aria-label={t("insp.retryTimes")}
              onChange={(v) => set((x) => { x.retry = { ...x.retry, transient: v ?? 2 }; })} />
            <span className="small muted">{t("insp.retryWait")}</span>
            <NumberInput value={s.retry?.wait ?? 15} min={1} max={300} step={5} aria-label={t("insp.retryWait")}
              onChange={(v) => set((x) => { x.retry = { ...x.retry, wait: v ?? 15 }; })} />
          </div>
        </Field>)}
      <h3>{t("io.flowInputs")}</h3>
      {isTyped(s)
        ? <p className="muted small">{t("io.flowInputsHint")}</p>
        // no inputs and no legacy input of its own: each run only gets an id (no input to fill in)
        : !s.input && !s.task ? <p className="muted small">{t("io.noInputsHint")}</p>
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
        <textarea ref={taskRef} rows={3} value={s.task ?? ""} onChange={(e) => set((x) => { x.task = e.target.value; })} />
      </Field>
      <VarChips catalog={catalog} onInsert={(x) => set((y) => { y.task = insertAt(taskRef.current, s.task ?? "", x); })} />
      <Field label={t("insp.start")}>
        <select value={s.start} onChange={(e) => set((x) => { x.start = e.target.value; })}>
          {Object.keys(s.agents).map((a) => <option key={a}>{a}</option>)}
        </select>
      </Field>
      <Field label={t("insp.orchestratorPrompt")} hint={t("insp.orchestratorPromptHint")}>
        <PromptEditor owner={s.orchestrator ?? {}} defaultPath="prompts/orchestrator.md" doc={doc} update={update}
          pick={(d) => (d.spec.orchestrator = d.spec.orchestrator ?? {})} catalog={catalog} />
      </Field>
      {catalog.workspace && catalog.workspace.name !== "studio" && <>
        <h3>{t("ctx.flowTitle")}</h3>
        <label className="check">
          <input type="checkbox" checked={s.use_context !== false}
            onChange={(e) => set((x) => { if (e.target.checked) delete x.use_context; else x.use_context = false; })} />
          {t("ctx.useContext")}
        </label>
        <Field label={t("ctx.afterRun")} hint={t("ctx.afterRunHint")}>
          <select value={s.context_update ?? "suggest"} onChange={(e) => set((x) => {
            const v = e.target.value as "suggest" | "always" | "never";
            if (v === "suggest") delete x.context_update; else x.context_update = v;
          })}>
            {(["suggest", "always", "never"] as const).map((k) => <option key={k} value={k}>{t(`ctx.after.${k}`)}</option>)}
          </select>
        </Field>
      </>}
      <h3>{t("insp.writing")}</h3>
      <Field label={t("insp.autonomy")} hint={t("insp.autonomyHint")}>
        <select value={s.autonomy ?? "approve"} onChange={(e) => set((x) => { x.autonomy = e.target.value as FlowRaw["autonomy"]; })}>
          {AUTONOMY.map((k) => <option key={k} value={k}>{t(`autonomy.${k}`)}</option>)}
        </select>
      </Field>
      {s.autonomy === "auto" && <p className="field-err">{t("insp.autoWarning")}</p>}
      <label className="check">
        <input type="checkbox" checked={!!s.workspace} onChange={(e) => set((x) => {
          if (e.target.checked) x.workspace = roots.length ? { root: roots[0], environment: "dev" } : { path: "", environment: "dev" };
          else delete x.workspace;
        })} /> {t("insp.workspace")}
      </label>
      {s.workspace && <>
        {(roots.length > 0 || s.workspace.root) && (
          <Field label={t("insp.folderRoot")} hint={t("insp.folderRootHint")}>
            <select value={s.workspace.root ?? ""} onChange={(e) => set((x) => {
              const w = { ...x.workspace! };
              if (e.target.value) { w.root = e.target.value; delete w.path; } else { delete w.root; w.path = ""; }
              x.workspace = w;
            })}>
              <option value="">{t("insp.folderPathOption")}</option>
              {[...new Set([...roots, ...(s.workspace.root ? [s.workspace.root] : [])])].map((r) => <option key={r} value={r}>{`{roots.${r}}`}</option>)}
            </select>
          </Field>)}
        {!s.workspace.root && <Field label={t("insp.folder")} hint={t("insp.folderHint")}>
          <input className="mono" value={s.workspace.path ?? ""} onChange={(e) => set((x) => { x.workspace = { ...x.workspace!, path: e.target.value }; })} />
        </Field>}
        <div className="grid2">
          <Field label={t("insp.environment")} hint={catalog.workspace?.kind ? t("insp.environmentFromWs", { env: catalog.workspace.environment ?? "", kind: catalog.workspace.kind }) : undefined}>
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

/** The tools that write (the same as WRITE_TOOLS in builder/catalog.py): a step that ran one is
 * not retried by itself after a temporary error. */
const WRITE_TOOL_NAMES = ["workspace.edit", "workspace.write", "workspace.bash", "connections.http_write"];

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
      <datalist id="afs-flows">{(p.flows ?? []).filter((f) => f !== doc.spec.name).map((f) => <option key={f} value={f} />)}</datalist>
    </aside>
  );
}

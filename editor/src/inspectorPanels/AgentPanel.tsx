/** The Inspector of an agent node: model, tools, connectors, IO, prompt; action and subflow nodes. */
import { useEffect, useState } from "react";
import { api } from "../api";
import { CommitInput, Field, NumberInput } from "../fields";
import { Icon, ICON_IDS } from "../icons";
import { AgentInputsEditor, IOListEditor } from "../IOEditors";
import { artifactOwners, renameNode, SLUG, nodeNames } from "../model";
import { useI18n } from "../i18n";
import type { AgentRaw, ArtifactRaw, Catalog, Doc, FlowRaw } from "../types";
import { AUTONOMY, ModelNote, PromptEditor, type Props, csv } from "./shared";

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

export function AgentPanel({ id, doc, catalog, update, select, testAgent, focus, library, val, flows, openProviders }: { id: string; focus?: string } & Omit<Props, "sel" | "remove">) {
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
      <Field label={t("insp.description")} hint={t(doc.spec.execution === "graph" ? "insp.descriptionHintGraph" : "insp.descriptionHint")}>
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
          <ModelNote model={a.model && a.model !== "inherit" ? a.model : doc.spec.model} catalog={catalog} open={openProviders} />
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


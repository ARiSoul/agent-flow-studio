/** The Inspector of the flow itself: general fields, execution, autonomy, workspace, MCPs. */
import { useRef, useState } from "react";
import { api } from "../api";
import { ConvertDialog, type ConvertPlan } from "./ConvertDialog";
import { Field, NumberInput } from "../fields";
import { IOListEditor } from "../IOEditors";
import { isTyped, SLUG, tidy } from "../model";
import { useI18n } from "../i18n";
import type { Catalog, Doc, FlowRaw, McpRaw } from "../types";
import { AUTONOMY, ModelNote, PromptEditor, type Update, VarChips, WRITE_TOOL_NAMES, csv, insertAt, kv, lines, unkv } from "./shared";

/** Converts the draft to the graph engine after showing what works differently (nothing is
 * saved: the person saves the flow as always). */
function ConvertToGraph({ doc, update }: { doc: Doc; update: Update }) {
  const { t } = useI18n();
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const [plan, setPlan] = useState<(ConvertPlan & { spec: Doc["spec"] }) | null>(null);
  const open = async () => {
    setBusy(true); setErr(null);
    try { setPlan(await api.toGraph(doc.spec.name, tidy(doc))); }
    catch (e) { setErr(String((e as Error).message ?? e)); }
    finally { setBusy(false); }
  };
  const convert = () => {
    if (!plan) return;
    const spec = plan.spec;
    update((d) => { d.spec = { ...spec, layout: d.spec.layout } as Doc["spec"]; });
    setPlan(null);
  };
  return <div className="warn-box small">
    <p>{t("insp.convertHint")}</p>
    <button className="small" disabled={busy} onClick={open}>{t("insp.convertButton")}</button>
    {err && <p className="field-err">{err}</p>}
    {plan && <ConvertDialog plan={plan} onCancel={() => setPlan(null)} onConvert={convert} />}
  </div>;
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

export function FlowPanel({ doc, catalog, update, openProviders }: { doc: Doc; catalog: Catalog; update: Update; openProviders?: (focus?: string) => void }) {
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
        <Field label={t("insp.model")}>
          <input list="models" value={s.model ?? ""} placeholder={t("insp.modelDefault")}
          onChange={(e) => set((x) => { x.model = e.target.value || undefined; })} />
          <ModelNote model={s.model} catalog={catalog} open={openProviders} /></Field>
        <Field label={t("insp.maxTurns")}><input type="number" min={1} max={500} value={s.max_turns ?? 60}
          onChange={(e) => set((x) => { x.max_turns = Number(e.target.value) || undefined; })} /></Field>
      </div>
      <Field label={t("insp.execution")}>
        <select value={s.execution ?? "orchestrator"} onChange={(e) => set((x) => { x.execution = e.target.value as FlowRaw["execution"]; })}>
          <option value="graph">{t("insp.execGraph")} — {t("insp.execRecommended")}</option>
          <option value="orchestrator">{t("insp.execOrchestrator")}</option>
        </select>
      </Field>
      {s.execution !== "graph" && <ConvertToGraph doc={doc} update={update} />}
      <div className="exec-modes small" role="note">
        <div className={s.execution === "graph" ? "on" : ""}>
          <b>{t("insp.execGraph")}</b> — {t("insp.executionGraphHint")}
          <div className="muted">{t("insp.execGraphOnly")}</div>
        </div>
        <div className={s.execution !== "graph" ? "on" : ""}>
          <b>{t("insp.execOrchestrator")}</b> — {t("insp.executionOrchHint")}
          <div className="muted">{t("insp.execOrchWhen")}</div>
        </div>
      </div>
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
      {(() => {  // agents with their own autonomy do not follow the flow's: say so, and offer to clear it
        const own = Object.entries(s.agents).filter(([, a]) => a.autonomy && a.autonomy !== (s.autonomy ?? "approve"));
        if (!own.length) return null;
        return <div className="warn-box small">
          <p>{t("insp.ownAutonomy", { list: own.map(([n, a]) => `${n} (${t(`autonomy.${a.autonomy as "approve"}`)})`).join(", ") })}</p>
          <button className="small" onClick={() => set((x) => { for (const [n] of own) delete x.agents[n].autonomy; })}>
            {t("insp.ownAutonomyClear")}</button>
        </div>;
      })()}
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


// Editors for typed values: flow inputs, agent outputs, agent inputs and edge expressions.
import { useRef } from "react";
import { CommitInput, Field, ListInput, NumberInput } from "./fields";
import { useI18n } from "./i18n";
import {
  ancestors, flowInputs, inputKind, removeRef, renameRef, SLUG, sourceIO, uniqueKey,
} from "./model";
import { typeTag } from "./nodes";
import { IO_KINDS, type AgentInputRaw, type Catalog, type Doc, type FlowRaw, type IOKind, type IOTypeRaw } from "./types";

type Update = (fn: (d: Doc) => void) => void;

const HAS_RANGE: IOKind[] = ["number", "integer", "text", "markdown"];
const HAS_PATTERN: IOKind[] = ["text", "markdown"];

/** Keeps only the fields that the new type accepts (so a type change does not leave errors). */
function retype(t: IOTypeRaw, k: IOKind): void {
  t.type = k;
  if (k !== "choice") delete t.options; else t.options = t.options ?? [];
  if (k !== "file") delete t.accept;
  if (k !== "json") delete t.contract;
  if (k !== "markdown") delete t.draft;
  if (!HAS_PATTERN.includes(k)) delete t.pattern;
  if (!HAS_RANGE.includes(k)) { delete t.min; delete t.max; }
  delete t.default;
}

/** Default value of a flow input, by type (not for files or lists). */
function DefaultInput({ t, set }: { t: IOTypeRaw; set: (v: unknown) => void }) {
  const { t: tr } = useI18n();
  const k = t.type ?? "text";
  const v = t.default;
  if (k === "boolean")
    return (
      <select value={v === undefined || v === null ? "" : String(v)} onChange={(e) => set(e.target.value === "" ? null : e.target.value === "true")}>
        <option value="">{tr("io.noDefault")}</option><option value="true">true</option><option value="false">false</option>
      </select>);
  if (k === "choice")
    return (
      <select value={v === undefined || v === null ? "" : String(v)} onChange={(e) => set(e.target.value || null)}>
        <option value="">{tr("io.noDefault")}</option>
        {(t.options ?? []).map((o) => <option key={o}>{o}</option>)}
      </select>);
  if (k === "number" || k === "integer")
    return <NumberInput value={typeof v === "number" ? v : null} step={k === "integer" ? 1 : "any"} onChange={set} />;
  if (k === "json")
    return (
      <textarea className="mono" rows={2} key={JSON.stringify(v ?? null)} defaultValue={v === undefined || v === null ? "" : JSON.stringify(v)}
        onBlur={(e) => { try { set(e.target.value.trim() ? JSON.parse(e.target.value) : null); } catch { /* the field keeps the text */ } }} />);
  const type = k === "date" ? "date" : k === "datetime" ? "datetime-local" : "text";
  return <input type={type} value={v === undefined || v === null ? "" : String(v)} onChange={(e) => set(e.target.value || null)} />;
}

/** Rows of typed values: the flow inputs (root "input") or the outputs of an agent (root = agent). */
export function IOListEditor({ root, doc, catalog, update }: { root: string; doc: Doc; catalog: Catalog; update: Update }) {
  const { t } = useI18n();
  const flow = root === "input";
  const items: Record<string, IOTypeRaw> = (flow ? doc.spec.inputs : doc.spec.agents[root]?.outputs) ?? {};
  const pick = (d: Doc): Record<string, IOTypeRaw> => {
    if (flow) return (d.spec.inputs = d.spec.inputs ?? {});
    const a = d.spec.agents[root];
    return (a.outputs = a.outputs ?? {});
  };
  const patch = (name: string, fn: (x: IOTypeRaw) => void) => update((d) => fn(pick(d)[name]));
  const setOpt = <K extends keyof IOTypeRaw>(name: string, k: K, v: IOTypeRaw[K] | null) =>
    patch(name, (x) => { if (v === null || v === undefined || v === "" || (Array.isArray(v) && !v.length)) delete x[k]; else x[k] = v; });
  return (
    <div className="rows io-list">
      {Object.entries(items).map(([name, it]) => {
        const k = it.type ?? "text";
        return (
          <div key={name} className="io-row">
            <div className="row">
              <CommitInput className="mono" value={name} pattern={SLUG} onCommit={(v) => {
                if (v in items) return t("io.nameTaken");
                update((d) => renameRef(d.spec, root, name, v));
                return null;
              }} />
              <select value={k} onChange={(e) => patch(name, (x) => retype(x, e.target.value as IOKind))} title={t("io.type")}>
                {IO_KINDS.map((x) => <option key={x} value={x}>{t(`io.type.${x}`)}</option>)}
              </select>
              <button className="icon" title={t("insp.remove")} onClick={() => update((d) => removeRef(d.spec, root, name))}>×</button>
            </div>
            <div className="row io-flags">
              <label className="check inline"><input type="checkbox" checked={it.required !== false}
                onChange={(e) => setOpt(name, "required", e.target.checked ? null : false)} />{t("io.required")}</label>
              <label className="check inline" title={t("io.multipleTitle")}><input type="checkbox" checked={!!it.multiple}
                onChange={(e) => patch(name, (x) => { if (e.target.checked) { x.multiple = true; delete x.default; } else delete x.multiple; })} />{t("io.multiple")}</label>
              {k === "markdown" && !flow && (
                <label className="check inline" title={t("insp.draftTitle")}><input type="checkbox" checked={!!it.draft}
                  onChange={(e) => setOpt(name, "draft", e.target.checked || null)} />{t("insp.draft")}</label>)}
            </div>
            {k === "choice" && (
              <Field label={t("io.options")} hint={t("io.optionsHint")}>
                <ListInput value={it.options} placeholder="quick, deep" onCommit={(v) => setOpt(name, "options", v)} />
              </Field>)}
            {k === "json" && (
              <Field label={t("io.contract")}>
                <select value={it.contract ?? ""} onChange={(e) => setOpt(name, "contract", e.target.value || null)}>
                  <option value="">{t("insp.noContract")}</option>
                  {Object.keys(catalog.contracts).map((c) => <option key={c}>{c}</option>)}
                </select>
              </Field>)}
            <details className="io-more">
              <summary className="small muted">{t("io.more")}</summary>
              {flow && (
                <Field label={t("io.label")} hint={t("io.labelHint")}>
                  <input value={it.label ?? ""} onChange={(e) => setOpt(name, "label", e.target.value)} />
                </Field>)}
              <Field label={t("insp.description")}>
                <input value={it.description ?? ""} onChange={(e) => setOpt(name, "description", e.target.value)} />
              </Field>
              {HAS_RANGE.includes(k) && (
                <div className="grid2">
                  <Field label={k === "number" || k === "integer" ? t("io.min") : t("io.minLength")}>
                    <NumberInput value={it.min} onChange={(v) => setOpt(name, "min", v)} />
                  </Field>
                  <Field label={k === "number" || k === "integer" ? t("io.max") : t("io.maxLength")}>
                    <NumberInput value={it.max} onChange={(v) => setOpt(name, "max", v)} />
                  </Field>
                </div>)}
              {HAS_PATTERN.includes(k) && (
                <Field label={t("io.pattern")} hint={t("io.patternHint")}>
                  <input className="mono" value={it.pattern ?? ""} onChange={(e) => setOpt(name, "pattern", e.target.value)} />
                </Field>)}
              {k === "file" && (
                <Field label={t("io.accept")} hint={t("io.acceptHint")}>
                  <ListInput className="mono" value={it.accept} placeholder=".md, .txt" onCommit={(v) => setOpt(name, "accept", v)} />
                </Field>)}
              {flow && k !== "file" && !it.multiple && (
                <Field label={t("io.default")}>
                  <DefaultInput t={it} set={(v) => setOpt(name, "default", v as IOTypeRaw["default"])} />
                </Field>)}
              {flow && (
                <Field label={t("io.showIf")} hint={t("io.showIfHint")}>
                  <input className="mono" value={it.show_if ?? ""} placeholder={'input.source == "platform"'}
                    onChange={(e) => setOpt(name, "show_if", e.target.value || null)} />
                </Field>)}
            </details>
            {flow && it.show_if && <span className="muted small">{t("io.shownWhen", { expr: it.show_if })}</span>}
          </div>
        );
      })}
      <button className="small" onClick={() => update((d) => {
        const m = pick(d);
        m[uniqueKey(Object.keys(m), flow ? "input" : "output")] = { type: "text" };
      })}>{flow ? t("io.addInput") : t("io.addOutput")}</button>
    </div>
  );
}

/** Sources an agent input can take: flow inputs and the outputs of other agents. */
function sources(spec: FlowRaw, agent: string) {
  const before = spec.execution === "graph" ? ancestors(spec, agent) : null;
  const flowIn = Object.entries(flowInputs(spec)).map(([n, t]) => ({ ref: `input.${n}`, t }));
  const outs = Object.entries(spec.agents).filter(([an]) => an !== agent)
    .flatMap(([an, a]) => Object.entries(a.outputs ?? {}).map(([n, t]) => ({ ref: `${an}.${n}`, t, agent: an })));
  return {
    flowIn,
    earlier: outs.filter((o) => !before || before.has(o.agent)),
    later: before ? outs.filter((o) => !before.has(o.agent)) : [],
    // the outputs of the last finished run of the flow (this agent's own included)
    previous: Object.entries(spec.agents).flatMap(([an, a]) => Object.entries(a.outputs ?? {})
      .map(([n, t]) => ({ ref: `previous.${an}.${n}`, t }))),
  };
}

/** The inputs of an agent: name, source (from), optional type override, required, description. */
export function AgentInputsEditor({ id, doc, update, focus }: { id: string; doc: Doc; update: Update; focus?: string }) {
  const { t } = useI18n();
  const spec = doc.spec;
  const items = spec.agents[id]?.inputs ?? {};
  const src = sources(spec, id);
  const known = new Set([...src.flowIn, ...src.earlier, ...src.later, ...src.previous].map((s) => s.ref));
  const pick = (d: Doc) => { const a = d.spec.agents[id]; return (a.inputs = a.inputs ?? {}); };
  const patch = (name: string, fn: (x: AgentInputRaw) => void) => update((d) => fn(pick(d)[name]));
  const label = (ref: string, x: IOTypeRaw) => `${ref} · ${typeTag(x.type ?? "text", x.multiple)}`;
  return (
    <div className="rows io-list">
      {Object.entries(items).map(([name, it]) => {
        const srcT = sourceIO(spec, it.from);
        return (
          <div key={name} className={`io-row${focus === name ? " hl" : ""}`}
            ref={(el) => { if (el && focus === name) el.scrollIntoView({ block: "nearest" }); }}>
            <div className="row">
              <CommitInput className="mono" value={name} pattern={SLUG} onCommit={(v) => {
                if (v in items) return t("io.nameTaken");
                update((d) => {
                  const a = d.spec.agents[id];
                  a.inputs = Object.fromEntries(Object.entries(a.inputs ?? {}).map(([k, x]) => [k === name ? v : k, x]));
                });
                return null;
              }} />
              <select className="from" value={it.from ?? ""} title={t("io.from")}
                onChange={(e) => patch(name, (x) => { if (e.target.value) x.from = e.target.value; else delete x.from; })}>
                <option value="">{t("io.free")}</option>
                {it.from && !known.has(it.from) && <option value={it.from}>{it.from} ({t("io.missing")})</option>}
                <optgroup label={t("io.flowInputs")}>
                  {src.flowIn.map((s) => <option key={s.ref} value={s.ref}>{label(s.ref, s.t)}</option>)}
                </optgroup>
                {src.earlier.length > 0 && <optgroup label={t("io.agentOutputs")}>
                  {src.earlier.map((s) => <option key={s.ref} value={s.ref}>{label(s.ref, s.t)}</option>)}
                </optgroup>}
                {src.later.length > 0 && <optgroup label={t("io.notBefore")}>
                  {src.later.map((s) => <option key={s.ref} value={s.ref}>{label(s.ref, s.t)}</option>)}
                </optgroup>}
                {src.previous.length > 0 && <optgroup label={t("io.previousRun")}>
                  {src.previous.map((s) => <option key={s.ref} value={s.ref}>{label(s.ref, s.t)}</option>)}
                </optgroup>}
              </select>
              <button className="icon" title={t("insp.remove")} onClick={() => update((d) => { delete pick(d)[name]; })}>×</button>
            </div>
            <div className="row io-flags">
              <select value={it.type ?? ""} title={t("io.typeOverride")}
                onChange={(e) => patch(name, (x) => { if (e.target.value) x.type = e.target.value as IOKind; else delete x.type; })}>
                <option value="">{t("io.typeFromSource", { type: t(`io.type.${srcT?.type ?? "text"}`) })}</option>
                {IO_KINDS.map((x) => <option key={x} value={x}>{t(`io.type.${x}`)}</option>)}
              </select>
              <label className="check inline"><input type="checkbox" checked={it.required !== false}
                onChange={(e) => patch(name, (x) => { if (e.target.checked) delete x.required; else x.required = false; })} />{t("io.required")}</label>
            </div>
            <input placeholder={t("io.descriptionPlaceholder")} value={it.description ?? ""}
              onChange={(e) => patch(name, (x) => { if (e.target.value) x.description = e.target.value; else delete x.description; })} />
            {!it.from && <span className="field-hint">{t("io.freeHint")}</span>}
            {it.from && src.later.some((s) => s.ref === it.from) && <span className="field-err">{t("io.notBeforeHint")}</span>}
            <span className="field-hint">{t("io.resolved", { type: t(`io.type.${inputKind(spec, it)}`) })}</span>
          </div>
        );
      })}
      <button className="small" onClick={() => update((d) => { const m = pick(d); m[uniqueKey(Object.keys(m), "input")] = {}; })}>
        {t("io.addInput")}</button>
    </div>
  );
}

/** References an `if` expression can use, with the edge's source first. */
export function exprRefs(spec: FlowRaw, from: string): string[] {
  const agents = Object.entries(spec.agents).sort(([a], [b]) => Number(b === from) - Number(a === from));
  return [
    ...agents.flatMap(([an, a]) => Object.keys(a.outputs ?? {}).map((o) => `${an}.${o}`)),
    ...Object.keys(flowInputs(spec)).map((n) => `input.${n}`),
    ...Object.keys(spec.gates ?? {}).flatMap((g) => (spec.gates![g].kind === "human" ? [`${g}.decision`, `${g}.comment`] : [])),
    ...agents.flatMap(([an, a]) => Object.keys(a.outputs ?? {}).map((o) => `previous.${an}.${o}`)),
  ];
}

/** The `if` field of an edge: an expression, with the references to insert and its errors. */
export function ExprInput({ value, refs, errors, onChange }: {
  value: string; refs: string[]; errors: string[]; onChange: (v: string) => void;
}) {
  const { t } = useI18n();
  const box = useRef<HTMLTextAreaElement>(null);
  const insert = (text: string) => {
    const el = box.current;
    const a = el?.selectionStart ?? value.length, b = el?.selectionEnd ?? value.length;
    const before = value.slice(0, a), after = value.slice(b);
    const pad = before && !/\s$/.test(before) ? " " : "";
    onChange(`${before}${pad}${text}${after}`);
    requestAnimationFrame(() => { el?.focus(); const p = a + pad.length + text.length; el?.setSelectionRange(p, p); });
  };
  return (
    <>
      <textarea ref={box} className={`mono${errors.length ? " bad" : ""}`} rows={2} spellCheck={false} value={value}
        placeholder='planner.size > 1 and input.depth == "deep"' onChange={(e) => onChange(e.target.value)} />
      {errors.map((e, i) => <span key={i} className="field-err">{e}</span>)}
      <div className="refs">
        {refs.length === 0 && <span className="muted small">{t("insp.noRefs")}</span>}
        {refs.map((r) => <button key={r} className="small ref" onClick={() => insert(r)} title={t("insp.insert")}>{r}</button>)}
      </div>
      <span className="field-hint">{t("insp.exprHelp")}</span>
    </>
  );
}

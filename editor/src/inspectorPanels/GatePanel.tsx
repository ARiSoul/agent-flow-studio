/** The Inspector of a gate (human, stop, decision with its branches) and of a link. */
import { useState } from "react";
import { CommitInput, Field } from "../fields";
import { ExprInput, exprRefs } from "../IOEditors";
import { renameNode, SLUG, nodeNames, uniqueName } from "../model";
import { useI18n } from "../i18n";
import { localizeParam, localizeServer } from "../i18n/server";
import type { Doc, GateRaw, Validation } from "../types";
import { type Props, type Update } from "./shared";

export function GatePanel({ id, doc, update, select, val }: { id: string } & Omit<Props, "sel" | "remove" | "catalog">) {
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

export function EdgePanel({ index, doc, val, update }: { index: number; doc: Doc; val: Validation | null; update: Update }) {
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


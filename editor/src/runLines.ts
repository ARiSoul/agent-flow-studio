// One event of a run as a line of text (no React here, so it can be tested on its own): its
// class, its text and its kind for the log's filters. Errors are counted by cause: a failed step or
// a failed tool call (the session result and the end of the run repeat that failure).
import type { I18n } from "./i18n";
import { duration, kb, shortId, shortModel, usd, type Ev } from "./runSteps.ts";

const statusText = (i18n: Pick<I18n, "tOr">, s: unknown) => i18n.tOr(`run.status.${String(s)}`, String(s));

export type LogKind = "all" | "errors" | "text" | "tools" | "flow";

/** One event as a line of the log: its class, its text (without the step's name: `self`) and kind. */
export function line(e: Ev, i18n: Pick<I18n, "t" | "tOr" | "tn">, self = true): { cls: string; text: string; kind: LogKind } | null {
  if (e.sub) {  // an event of a subflow: the same line, tagged with the subflow's own step
    const l = line({ ...e, sub: undefined }, i18n, false);
    return l && { cls: `${l.cls} subev`, text: `↳ ${l.text}`, kind: l.kind };
  }
  const { t, tOr, tn } = i18n;
  const who = e.agent && !self ? `${e.agent}: ` : "";
  const mode = String(e.mode ?? "full");
  switch (e.type) {
    case "start": return { kind: "flow", cls: "info", text: `${t("run.ev.start", { flow: String(e.flow), input: String(e.input), mode: tOr(`run.mode.${mode}`, mode) })}${e.target ? ` ${e.target}` : ""}${e.execution === "graph" ? t("run.ev.graph") : ""}${e.workspace && e.workspace !== "studio" ? ` · ${t("run.ev.workspace", { ws: String(e.workspace), env: String(e.environment ?? "—") })}` : ""}${e.handoff ? ` · ${t("run.ev.handoff", { list: Object.entries(e.handoff as Record<string, { flow: string; run: string }>).map(([k, v]) => `${k} ← ${v.flow}/${v.run}`).join(", ") })}` : ""}` };
    case "context": return e.status === "running"
      ? { kind: "flow", cls: "deleg", text: t("run.ev.contextStart", { root: String(e.root), mode: tOr(`ctx.mode.${String(e.mode)}`, String(e.mode)) }) }
      : { kind: e.status === "done" ? "flow" : "errors", cls: e.status === "done" ? "ok" : "err", text: t(e.status === "done" ? "run.ev.contextDone" : "run.ev.contextError",
        { root: String(e.root), detail: String(e.summary ?? e.message ?? "") }) };
    case "subflow": return { kind: "flow", cls: "deleg", text: t("run.ev.subflow", { agent: String(e.agent), flow: String(e.flow), run: String(e.run) }) };
    case "delegate": return { kind: "flow", cls: "deleg", text: e.task === "follow-up" ? t("rp.ev.askMore")
      : e.retry ? t("rp.ev.startedAgain", { n: String(Number(e.retry) + 1) }) : t("rp.ev.started", { task: String(e.task ?? "") }) };
    case "retry": return { kind: "flow", cls: "gate", text: t("rp.ev.retry", { wait: duration(Number(e.wait ?? 0)), n: String(Number(e.attempt ?? 1) + 1),
      of: String(Number(e.max ?? 1) + 1) }) };
    case "retry_cancelled": return { kind: "flow", cls: "info", text: t("rp.ev.retryCancelled") };
    case "retry_skipped": return { kind: "flow", cls: "gate", text: t("rp.ev.retrySkipped") };
    case "turn": return { kind: "flow", cls: "info", text: t("rp.ev.turn", { after: String(e.after ?? "") }) };
    case "tool": return { kind: "tools", cls: "tool", text: `${who}${e.tool}${e.detail ? ` — ${e.detail}` : e.artifact ? ` (${e.artifact})` : ""}` };
    case "tool_error": return e.id ? null  // shown on its tool's line
      : { kind: "errors", cls: "err", text: e.error ? t("run.ev.toolErrorDetail", { who, tool: String(e.tool ?? "?"), error: String(e.error) }) : t("run.ev.toolError", { who }) };
    case "agent_done": return { kind: e.error ? "errors" : "flow", cls: e.error ? "err" : "ok",
      text: `${t(e.error ? "rp.ev.failed" : "rp.ev.done")}${e.error && e.message ? ` — ${e.message}` : ""}` };
    case "text": return { kind: "text", cls: "sub", text: String(e.text) };
    case "gate": return e.message ? { kind: "flow", cls: "result-msg", text: `■ ${e.label}: ${e.message}` }
      : { kind: "flow", cls: "gate", text: `⏸ ${e.label}` };
    case "decided": return { kind: "flow", cls: "edge", text: t(e.to ? "run.ev.decided" : "run.ev.decidedNone", { node: String(e.node), to: String(e.to ?? "") }) };
    case "approval": return { kind: "flow", cls: "gate", text: t("run.ev.approval", { agent: String(e.agent), summary: String(e.summary) }) };
    case "approval_result": return { kind: e.approved ? "flow" : "errors", cls: e.approved ? "ok" : "err", text: `${t(e.approved ? "run.ev.approved" : "run.ev.rejected")}: ${e.summary}${e.comment ? ` — ${e.comment}` : ""}` };
    case "resume": return { kind: "flow", cls: "info", text: t("run.ev.resume", { keep: ((e.reuse as string[]) ?? []).join(", ") || "—", redo: ((e.redo as string[]) ?? []).join(", ") || "—" }) };
    case "continue": return { kind: "flow", cls: "info", text: t(e.how === "full" ? "run.ev.continueFull" : e.how === "auto" ? "run.ev.continueAuto" : "run.ev.continueStep") };
    case "ask": return { kind: "text", cls: "you", text: `${t("run.ev.ask", { who: e.agent ? String(e.agent) : t("run.askFlow") })}\n${e.text}` };
    case "decision": return { kind: "flow", cls: "info", text: `${t("run.ev.decision", { gate: String(e.gate), decision: tOr(`run.decision.${String(e.decision)}`, String(e.decision)) })}${e.comment ? ` — ${e.comment}` : ""}` };
    case "result": return { kind: "tools", cls: e.is_error ? "err" : "info",
      text: `${t("rp.ev.session", { session: shortId(String(e.session_id ?? "")), turns: tn("rp.turns", Number(e.turns ?? 0)), cost: usd(Number(e.cost ?? 0)) })}${e.model ? ` · ${shortModel(String(e.model))}` : ""}${e.denials ? tn("run.ev.denials", Number(e.denials)) : ""}` };
    case "skipped": return { kind: "flow", cls: "info", text: t("rp.ev.skipped") };
    case "follow_up": return { kind: "flow", cls: "info", text: t("rp.ev.followUp", { names: ((e.missing as string[]) ?? []).join(", ") }) };
    case "edge": {
      const by = String(e.by ?? "always");
      return { kind: "flow", cls: e.fired ? "edge" : "edge off", text: t(e.fired ? "rp.ev.edge" : "rp.ev.edgeNot", { to: String(e.to), by: tOr(`run.by.${by}`, by) }) };
    }
    case "outputs": return { kind: "flow", cls: "ok", text: t("rp.ev.outputs", { names: (Array.isArray(e.names) ? e.names : []).join(", ") || "—" }) };
    case "end": return { kind: "flow", cls: e.status === "error" ? "err" : "info", text: `${t("run.ev.end", { status: statusText(i18n, e.status) })}${e.message ? ` — ${e.message}` : ""}${e.note ? ` (${e.note})` : ""}` };
    default: return null;
  }
}

/** The text of a tool call's outcome: "1.2 KB · 0.3 s" or its error. */
export function outcomeText(o: Ev | undefined, t: I18n["t"]): { text: string; bad: boolean } | null {
  if (!o) return null;
  const ms = typeof o.ms === "number" ? duration(o.ms / 1000) : "";
  if (o.type === "tool_error") return { text: `${t("rp.toolFailed")}${ms ? ` · ${ms}` : ""}: ${String(o.error ?? "")}`, bad: true };
  return { text: [typeof o.size === "number" ? kb(o.size) : "", ms].filter(Boolean).join(" · "), bad: false };
}

/** The links a step follows (or not) right after it ends, as ONE line: "→ a · → b · ↛ c (if x)". */
export function foldLinks<X extends { e: Ev; l: { cls: string; text: string; kind: LogKind }; node: string | undefined }>(lines: X[]): X[] {
  const out: X[] = [];
  for (const x of lines) {
    const last = out[out.length - 1];
    if (x.e.type === "edge" && last?.e.type === "edge" && last.node === x.node) {
      const off = last.l.cls.includes("off") && x.l.cls.includes("off");
      out[out.length - 1] = { ...last, l: { ...last.l, cls: off ? "edge off" : "edge", text: `${last.l.text} · ${x.l.text}` } };
    } else out.push(x);
  }
  return out;
}

/** The failures that were retried by themselves (an agent_done error and its session's error
 * result, followed by a `retry` of the same step): the log shows them as warnings, not errors. */
export function retriedFailures(events: Ev[]): Set<number> {
  const out = new Set<number>();
  const open: Record<string, number[]> = {};  // step → its failure events since its last start
  for (const e of events) {
    const a = typeof e.agent === "string" ? e.agent : "";
    if (!a || e.sub) continue;
    if (e.type === "delegate") open[a] = [];
    else if ((e.type === "agent_done" && e.error) || (e.type === "result" && e.is_error)) (open[a] ??= []).push(e.i);
    else if (e.type === "retry") { for (const i of open[a] ?? []) out.add(i); open[a] = []; }
  }
  return out;
}

/** The cause of a temporary error in a few words, cut at a word ("API error: overloaded (529)"). */
export function shortCause(text: string, max = 90): string {
  const one = text.replace(/^the session ended with an error \(/i, "").replace(/\)$/, "").replace(/\s+/g, " ").trim();
  if (one.length <= max) return one;
  const cut = one.slice(0, max);
  return `${cut.slice(0, Math.max(cut.lastIndexOf(" "), max * 0.6))}…`;
}

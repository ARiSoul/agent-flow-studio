// The steps of a run, built from its events: one row per node of the flow (agents, gates,
// decisions) in the order of the graph, with its state, timing, cost, tokens, its attempts and its
// own events. The Run panel shows them as a pipeline view; the flat log stays available.
import type { FlowRaw } from "./types";

export type Ev = { i: number; type: string; ts?: number; [k: string]: unknown };

export type StepState = "pending" | "running" | "retrying" | "done" | "failed" | "waiting" | "skipped" | "interrupted";
export type StepKind = "agent" | "action" | "subflow" | "human" | "stop" | "decision";
export type Tokens = { in?: number; out?: number; cache_read?: number; cache_write?: number };

/** One run of a step (a loop, a resume or "run from step" runs it again). */
export interface Attempt { n: number; state: StepState; start?: number; end?: number; cost: number; error?: string }

export interface Step {
  node: string;
  kind: StepKind;
  level: number;  // distance from the start: steps on the same level can run in parallel
  state: StepState;
  start?: number;  // seconds (epoch) of the last attempt
  end?: number;
  cost: number;  // all attempts
  turns: number;
  tokens: Tokens;
  model?: string;  // the model that answered (from the session result)
  session?: string;
  attempts: Attempt[];
  events: Ev[];  // its own events, in order (all attempts)
  error?: string;
  outputs: string[];
  /** A human gate: what the person decided, with their comment (and how long the gate waited). */
  decision?: { decision: string; comment: string; waited?: number };
  /** While it runs: tokens written so far, the size of the last request and the estimated cost so
   * far (from each API message; the session's result replaces them with the real figures). */
  live?: { out: number; context: number; est: number };
  /** It failed with a temporary error and runs again by itself at `at` (seconds, epoch). */
  retry?: { attempt: number; max: number; at: number; reason: string };
  /** Its last failure was a temporary API error (busy, a limit). */
  transient?: boolean;
  /** How many times it ran again by itself after a temporary error (it may then have succeeded). */
  retries: number;
  /** A temporary error was not retried because the attempt had asked to write. */
  wroteNoRetry?: boolean;
  /** The time of its last event (a step that runs with no event for long may be stuck). */
  last?: number;
}

/** The nodes in the order of the graph (from the start, breadth first along forward links) with
 * their level; nodes no link reaches come last, in the flow's order. */
export function graphOrder(spec: FlowRaw): { node: string; level: number }[] {
  const all = [...Object.keys(spec.agents), ...Object.keys(spec.gates ?? {})];
  const next: Record<string, string[]> = {};
  for (const e of spec.edges ?? []) (next[e.from] ??= []).push(e.to);
  const level: Record<string, number> = {};
  const order: string[] = [];
  const queue: string[] = spec.start && all.includes(spec.start) ? [spec.start] : [];
  if (queue.length) level[queue[0]] = 0;
  while (queue.length) {
    const n = queue.shift()!;
    order.push(n);
    for (const m of next[n] ?? []) {
      if (m in level || !all.includes(m)) continue;  // the first (shortest) way in decides
      level[m] = level[n] + 1;
      queue.push(m);
    }
  }
  const rest = all.filter((n) => !(n in level));
  const last = order.length ? Math.max(...order.map((n) => level[n])) + 1 : 0;
  return [...order.map((n) => ({ node: n, level: level[n] })), ...rest.map((n, i) => ({ node: n, level: last + i }))];
}

function kindOf(spec: FlowRaw, node: string): StepKind {
  const a = spec.agents[node];
  if (a) return a.action ? "action" : a.subflow ? "subflow" : "agent";
  const g = spec.gates?.[node];
  return g?.kind === "decision" ? "decision" : g?.kind === "stop" ? "stop" : "human";
}

/** The node an event belongs to (undefined: an event of the run itself). */
export function nodeOf(e: Ev): string | undefined {
  if (typeof e.sub === "string") return e.sub.startsWith("context:") ? undefined : e.sub;
  if (e.type === "edge") return typeof e.from === "string" ? e.from : undefined;
  const n = e.agent ?? e.node ?? e.gate;
  return typeof n === "string" && n ? n : undefined;
}

const addTokens = (a: Tokens, b: unknown) => {
  if (!b || typeof b !== "object") return;
  for (const [k, v] of Object.entries(b as Tokens)) {
    if (typeof v === "number") a[k as keyof Tokens] = (a[k as keyof Tokens] ?? 0) + v;
  }
};

export function stepsOf(spec: FlowRaw, events: Ev[]): Step[] {
  const order = graphOrder(spec);
  const steps: Record<string, Step> = Object.fromEntries(order.map(({ node, level }) => [node, {
    node, kind: kindOf(spec, node), level, state: "pending" as StepState, cost: 0, turns: 0, tokens: {},
    attempts: [], events: [], outputs: [], retries: 0,
  }]));
  const cur = (s: Step) => s.attempts[s.attempts.length - 1];
  const begin = (s: Step, ts?: number, state: StepState = "running") => {
    s.attempts.push({ n: s.attempts.length + 1, state, start: ts, cost: 0 });
    s.state = state; s.start = ts; s.end = undefined; s.error = undefined;
  };
  const finish = (s: Step, state: StepState, ts?: number, error?: string) => {
    s.state = state; s.end = ts; s.error = error;
    const a = cur(s);
    if (a) { a.state = state; a.end = ts; a.error = error; }
  };
  for (const e of events) {
    if (e.type === "decision" && typeof e.gate === "string" && steps[e.gate]) {
      steps[e.gate].decision = { decision: String(e.decision ?? ""), comment: String(e.comment ?? ""),
        ...(typeof e.waited === "number" ? { waited: e.waited } : {}) };
    }
    if (e.type === "resume") {  // the nodes it repeats start over (their old events stay)
      for (const n of (e.redo as string[] | undefined) ?? []) if (steps[n]) steps[n].state = "pending";
      continue;
    }
    if (e.type === "end") {  // a stop cancels a running node without an agent_done event
      for (const s of Object.values(steps)) {
        if (s.state === "running") finish(s, "interrupted", e.ts);
        if (s.state === "retrying") { s.state = "failed"; s.retry = undefined; }
      }
      continue;
    }
    const n = nodeOf(e);
    const s = n ? steps[n] : undefined;
    if (!s) continue;
    if (e.type === "usage") {  // live tokens of the running session (not a line of the log)
      const tk = (e.tokens ?? {}) as Tokens;
      s.live = { out: (s.live?.out ?? 0) + (tk.out ?? 0), context: (tk.in ?? 0) + (tk.cache_read ?? 0) + (tk.cache_write ?? 0),
        est: (s.live?.est ?? 0) + Number(e.est ?? 0) };
      s.last = e.ts;
      continue;
    }
    s.events.push(e);
    if (e.ts) s.last = e.ts;
    if (e.sub) continue;  // inside a subflow: listed under its node, no state change
    switch (e.type) {
      case "delegate":
        if (e.task !== "follow-up") { begin(s, e.ts); s.retry = undefined; s.wroteNoRetry = undefined; }  // "ask for more" is not an attempt
        if (e.retry) s.retries += 1;
        break;
      case "agent_done":
        finish(s, e.error ? "failed" : "done", e.ts, e.error ? String(e.message ?? "") : undefined);
        s.transient = !!e.error && (e.transient === true || TRANSIENT.test(String(e.message ?? "")));
        s.live = undefined;
        break;
      case "retry":  // waits, then runs again by itself
        s.retry = { attempt: Number(e.attempt ?? 1), max: Number(e.max ?? 1), at: Number(e.at ?? 0), reason: String(e.reason ?? "") };
        s.state = "retrying";
        break;
      case "retry_cancelled": s.retry = undefined; s.state = "failed"; break;
      case "retry_skipped": s.wroteNoRetry = true; break;
      case "result": {
        s.live = undefined;  // the session's own totals replace the live count
        const c = Number(e.cost ?? 0);
        s.cost += c; s.turns += Number(e.turns ?? 0);
        const a = cur(s); if (a) a.cost += c;
        addTokens(s.tokens, e.tokens);
        if (typeof e.model === "string") s.model = e.model;
        if (typeof e.session_id === "string") s.session = e.session_id;
        break;
      }
      case "outputs": s.outputs = Array.isArray(e.names) ? e.names.map(String) : []; break;
      case "skipped": s.state = "skipped"; break;
      case "gate": begin(s, e.ts, s.kind === "stop" ? "done" : "waiting"); if (s.kind === "stop") finish(s, "done", e.ts); break;
      case "decision": finish(s, "done", e.ts); break;
      case "decided": begin(s, e.ts); finish(s, "done", e.ts); break;
    }
  }
  return order.map(({ node }) => steps[node]);
}

/** The steps as rows. Two or more steps on the same level form a group: `parallel` when every
 * link into them always fires (a fan-out), `choice` when a decision or a condition picks among
 * them (only some of them run). */
export type Group = { kind: "single" | "parallel" | "choice"; steps: Step[] };
export function groups(steps: Step[], spec: FlowRaw): Group[] {
  const out: Group[] = [];
  for (const s of steps) {
    const last = out[out.length - 1];
    if (last && last.steps[0].level === s.level) last.steps.push(s); else out.push({ kind: "single", steps: [s] });
  }
  for (const g of out) {
    if (g.steps.length < 2) continue;
    const names = new Set(g.steps.map((x) => x.node));
    const into = (spec.edges ?? []).filter((e) => names.has(e.to));
    const chosen = into.some((e) => e.if || e.when || spec.gates?.[e.from]?.kind === "decision");
    g.kind = chosen ? "choice" : "parallel";
  }
  return out;
}

export type NodeRun = "active" | "done" | "error" | "gate" | "skipped" | "stopped";
/** State of a link in a run: followed, not followed (its condition did not hold), or its `if`
 * expression failed. Key: `${from}>${to}`. */
export type EdgeRun = "followed" | "not" | "error";
export const edgeKey = (from: string, to: string) => `${from}>${to}`;
/** The system step after a run that updates the workspace context (drawn on the canvas). */
export const CONTEXT_NODE = "__workspace_context";

/** State of each node from the events (for the canvas). In graph mode several agents run at once. */
export function traceOf(events: Ev[]): Record<string, NodeRun> {
  const s: Record<string, NodeRun> = {};
  let graph = false;
  for (const e of events) {
    if (e.sub) continue;  // inside a subflow: not a node of this canvas
    const a = e.agent as string | undefined;
    if (e.type === "context") s[CONTEXT_NODE] = e.status === "running" ? "active" : e.status === "done" ? "done" : "error";
    if (e.type === "start") graph = e.execution === "graph";
    if (e.type === "delegate" && a && e.task !== "follow-up") {
      if (!graph) for (const k of Object.keys(s)) if (s[k] === "active") s[k] = "done";
      s[a] = "active";
    }
    if (e.type === "agent_done" && a) s[a] = e.error ? "error" : "done";
    if (e.type === "retry" && a) s[a] = "active";  // it runs again by itself
    if (e.type === "agent_blocked" && a) delete s[a];  // denied by step-by-step mode
    if (e.type === "skipped" && e.node) s[e.node as string] = "skipped";
    if (e.type === "gate") s[e.gate as string] = "gate";
    if (e.type === "decision") s[e.gate as string] = "done";
    if (e.type === "decided" && e.node) s[e.node as string] = "done";  // a decision node
    // a stop cancels a running agent without an agent_done event: it would look "running" forever
    if (e.type === "end") for (const k of Object.keys(s)) if (s[k] === "active") s[k] = "stopped";
    if (e.type === "continue" || e.type === "decision") {  // a resumed run starts agents again
      for (const k of Object.keys(s)) if (s[k] === "stopped") delete s[k];
    }
    // a resume: the failed/interrupted nodes and what depends on them start over
    if (e.type === "resume") for (const k of (e.redo as string[] | undefined) ?? []) delete s[k];
  }
  return s;
}

/** State of each link from the events. Graph mode: the engine's `edge` events. Orchestrator mode
 * (no edge events): a link counts as followed when its target starts (delegate or gate) right after
 * its source finished; at the end, the other links out of the nodes that ran were not followed. */
export function edgeTraceOf(events: Ev[], links: { from: string; to: string }[]): Record<string, EdgeRun> {
  const s: Record<string, EdgeRun> = {};
  const exists = new Set(links.map((l) => edgeKey(l.from, l.to)));
  let graph = false;
  let last: string | null = null;  // orchestrator: the node that finished last
  const ran = new Set<string>();
  const follow = (to: string) => {
    if (last && exists.has(edgeKey(last, to))) s[edgeKey(last, to)] = "followed";
  };
  for (const e of events) {
    if (e.sub) continue;  // inside a subflow
    const a = e.agent as string | undefined;
    if (e.type === "start") { graph = e.execution === "graph"; continue; }
    if (e.type === "resume") {  // the links out of the nodes that start over are decided again
      const redo = new Set(((e.redo as string[] | undefined) ?? []));
      for (const l of links) if (redo.has(l.from)) delete s[edgeKey(l.from, l.to)];
      continue;
    }
    if (graph) {
      if (e.type === "edge" && e.from && e.to) {
        const k = edgeKey(String(e.from), String(e.to));
        s[k] = e.error ? "error" : e.fired ? "followed" : "not";
      }
      continue;
    }
    if (e.type === "delegate" && a) { follow(a); ran.add(a); }
    if (e.type === "agent_done" && a && !e.error) last = a;
    if (e.type === "gate" && e.gate) { follow(String(e.gate)); ran.add(String(e.gate)); }
    if (e.type === "decision" && e.gate) last = String(e.gate);
    if (e.type === "end" && e.status === "done") {
      for (const l of links) {
        const k = edgeKey(l.from, l.to);
        if (ran.has(l.from) && !s[k]) s[k] = "not";
      }
    }
  }
  return s;
}

/** What the run cost so far: every session result (agents and the orchestrator), all attempts. */
export const costOf = (events: Ev[]) => events.reduce((k, e) => k + (e.type === "result" ? Number(e.cost ?? 0) : 0), 0);

/** The estimated cost of the sessions still going (their results are not in yet). */
export const liveCostOf = (steps: Step[]) => steps.reduce((k, s) => k + (s.state === "running" ? s.live?.est ?? 0 : 0), 0);

/** "claude-sonnet-5-5" → "sonnet-5-5" (the log is narrow). */
export const shortModel = (m: string) => m.replace(/^claude-/, "");
/** The first 8 characters of a session or run id (the full id is in the title and in copy). */
export const shortId = (id: string) => (id.length > 12 ? `${id.slice(0, 8)}…` : id);

/** A tool call joined with its result (size and time) or its error, by the call's id. */
export function toolOutcome(events: Ev[]): Record<string, Ev> {
  const out: Record<string, Ev> = {};
  for (const e of events) if ((e.type === "tool_result" || e.type === "tool_error") && typeof e.id === "string") out[e.id] = e;
  return out;
}

/** "1:05", "12 s", "0.4 s". */
export function duration(seconds: number | undefined): string {
  if (seconds === undefined || !isFinite(seconds) || seconds < 0) return "";
  if (seconds < 10) return `${seconds.toFixed(1)} s`;
  if (seconds < 60) return `${Math.round(seconds)} s`;
  const m = Math.floor(seconds / 60), s = Math.round(seconds % 60);
  return m < 60 ? `${m}:${String(s).padStart(2, "0")}` : `${Math.floor(m / 60)} h ${m % 60} min`;
}

/** The one way the panel writes a cost: "$0.024". */
export const usd = (n: number | undefined) => `$${(n ?? 0).toFixed(3)}`;

/** "1.2k", "340", "1.4M". */
export function count(n: number | undefined): string {
  if (!n) return "0";
  return n >= 1e6 ? `${(n / 1e6).toFixed(1)}M` : n >= 1e3 ? `${(n / 1e3).toFixed(1)}k` : String(n);
}

/** Splits what the event stream sent so far into events (one per line; a bad line is skipped)
 * and the rest of an unfinished line. */
export function parseLines(buf: string): { events: Ev[]; rest: string } {
  const events: Ev[] = [];
  let nl: number;
  while ((nl = buf.indexOf("\n")) >= 0) {
    const raw = buf.slice(0, nl).trim();
    buf = buf.slice(nl + 1);
    if (!raw) continue;
    try { events.push(JSON.parse(raw) as Ev); } catch { /* skipped */ }
  }
  return { events, rest: buf };
}

/** A failure of the API that usually goes away by itself (busy, a rate limit, 5xx, network); the
 * same test as `engine.is_transient` (a tool's own timeout is not one). */
export const TRANSIENT = /api error:?\s*\(?(429|5\d\d)\b|api error:?\s*(overloaded|connection)|overloaded_error|rate_limit_error|\boverloaded \(529\)/i;

export const kb = (n: number) => (n < 1024 ? `${n} B` : n < 1048576 ? `${(n / 1024).toFixed(1)} KB` : `${(n / 1048576).toFixed(1)} MB`);

/** A word diff of two texts: runs of words kept, added or removed (null when too long to diff). */
export type DiffPart = { op: "same" | "add" | "del"; text: string };
export function wordDiff(a: string, b: string, max = 600): DiffPart[] | null {
  const x = a.split(/(\s+)/), y = b.split(/(\s+)/);
  if (x.length > max || y.length > max) return null;
  const n = x.length, m = y.length;
  const L: number[][] = Array.from({ length: n + 1 }, () => new Array<number>(m + 1).fill(0));
  for (let i = n - 1; i >= 0; i--) for (let j = m - 1; j >= 0; j--) {
    L[i][j] = x[i] === y[j] ? L[i + 1][j + 1] + 1 : Math.max(L[i + 1][j], L[i][j + 1]);
  }
  const out: DiffPart[] = [];
  const push = (op: DiffPart["op"], text: string) => {
    const last = out[out.length - 1];
    if (last && last.op === op) last.text += text; else out.push({ op, text });
  };
  let i = 0, j = 0;
  while (i < n && j < m) {
    if (x[i] === y[j]) { push("same", x[i]); i++; j++; }
    else if (L[i + 1][j] >= L[i][j + 1]) push("del", x[i++]);
    else push("add", y[j++]);
  }
  while (i < n) push("del", x[i++]);
  while (j < m) push("add", y[j++]);
  return out;
}

/** Seconds the run waited for the person at human gates (an open gate counts until `until`). */
export function waitedOf(events: Ev[], until?: number): number {
  let total = 0;
  const open: Record<string, number> = {};
  for (const e of events) {
    if (e.type === "gate" && e.kind === "human" && typeof e.ts === "number") open[String(e.gate)] = e.ts;
    else if (e.type === "decision" && String(e.gate) in open && typeof e.ts === "number") { total += e.ts - open[String(e.gate)]; delete open[String(e.gate)]; }
  }
  if (until !== undefined) for (const t of Object.values(open)) total += Math.max(0, until - t);
  return total;
}

// Pure operations on the flow (each one receives a copy and changes it).
import dagre from "@dagrejs/dagre";
import type { AgentInputRaw, AgentRaw, Doc, FlowRaw, GateRaw, IOKind, IOTypeRaw } from "./types";

export const SLUG = /^[a-z][a-z0-9_]{0,39}$/;
export const NODE_W = 240;
export const NODE_H = 96;
export const PORT_H = 18;  // height of one port row (keep in sync with .port in styles.css)
export const LEGACY_INPUT = "input";  // the one implicit input of flows without `inputs`

export const clone = <T,>(v: T): T => structuredClone(v);

export function nodeNames(spec: FlowRaw): string[] {
  return [...Object.keys(spec.agents), ...Object.keys(spec.gates ?? {})];
}

export function uniqueName(spec: FlowRaw, base: string): string {
  const used = new Set([...nodeNames(spec), ...Object.keys(spec.mcp_servers ?? {})]);
  for (let i = 1; ; i++) if (!used.has(`${base}_${i}`)) return `${base}_${i}`;
}

/** `base`, or `base_2`, `base_3`… when it is already in `used`. */
export function uniqueKey(used: Iterable<string>, base: string): string {
  const s = new Set(used);
  if (!s.has(base)) return base;
  for (let i = 2; ; i++) if (!s.has(`${base}_${i}`)) return `${base}_${i}`;
}

const rekey = <V,>(obj: Record<string, V> | undefined, from: string, to: string) =>
  obj && Object.fromEntries(Object.entries(obj).map(([k, v]) => [k === from ? to : k, v]));

const esc = (s: string) => s.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
const STRINGS = /("(?:[^"\\]|\\.)*"|'(?:[^'\\]|\\.)*')/;

/** Replaces a reference prefix in an expression (`old.` → `new.`), outside string literals. */
export function renameInExpr(text: string, from: string, to: string): string {
  const re = new RegExp(`(?<![A-Za-z0-9_.])${esc(from)}(?=\\.)`, "g");
  return text.split(STRINGS).map((part, i) => (i % 2 ? part : part.replace(re, to))).join("");
}

/** Agent inputs of the flow, as [agent, input name, input]. */
function allInputs(spec: FlowRaw): [string, string, AgentInputRaw][] {
  return Object.entries(spec.agents).flatMap(([an, a]) =>
    Object.entries(a.inputs ?? {}).map(([n, i]) => [an, n, i] as [string, string, AgentInputRaw]));
}

/** Renames while keeping the key order and updating links, data refs, conditions, start and layout. */
export function renameNode(spec: FlowRaw, from: string, to: string): void {
  if (from in spec.agents) spec.agents = rekey(spec.agents, from, to)!;
  if (spec.gates && from in spec.gates) spec.gates = rekey(spec.gates, from, to);
  if (spec.layout) spec.layout = rekey(spec.layout, from, to);
  if (spec.start === from) spec.start = to;
  for (const e of spec.edges ?? []) {
    if (e.from === from) e.from = to;
    if (e.to === from) e.to = to;
    if (e.if) e.if = renameInExpr(e.if, from, to);
  }
  for (const [, , i] of allInputs(spec))
    if (i.from?.startsWith(`${from}.`)) i.from = `${to}.${i.from.slice(from.length + 1)}`;
}

/** Renames a flow input (root "input") or an agent output (root = agent) and its references. */
export function renameRef(spec: FlowRaw, root: string, from: string, to: string): void {
  if (root === "input") spec.inputs = rekey(spec.inputs, from, to);
  else if (spec.agents[root]) spec.agents[root].outputs = rekey(spec.agents[root].outputs, from, to);
  for (const [, , i] of allInputs(spec)) if (i.from === `${root}.${from}`) i.from = `${root}.${to}`;
  const re = new RegExp(`(?<![A-Za-z0-9_.])${esc(root)}\\.${esc(from)}(?![A-Za-z0-9_])`, "g");
  for (const e of spec.edges ?? [])
    if (e.if) e.if = e.if.split(STRINGS).map((p, k) => (k % 2 ? p : p.replace(re, `${root}.${to}`))).join("");
  if (root === "input" && spec.task) spec.task = spec.task.split(`{${from}}`).join(`{${to}}`);
}

/** Removes a flow input or an agent output; inputs fed by it become free inputs. */
export function removeRef(spec: FlowRaw, root: string, name: string): void {
  if (root === "input") delete spec.inputs?.[name];
  else delete spec.agents[root]?.outputs?.[name];
  for (const [, , i] of allInputs(spec)) if (i.from === `${root}.${name}`) delete i.from;
}

export function removeNode(spec: FlowRaw, id: string): void {
  delete spec.agents[id];
  if (spec.gates) delete spec.gates[id];
  if (spec.layout) delete spec.layout[id];
  spec.edges = (spec.edges ?? []).filter((e) => e.from !== id && e.to !== id);
  for (const [, , i] of allInputs(spec)) if (i.from?.startsWith(`${id}.`)) delete i.from;
}

export function newAgent(description: string, prompt: string): AgentRaw {
  return { description, prompt, tools: [] };
}

export function newGate(kind: GateRaw["kind"], label: string): GateRaw {
  return { kind, label };
}

/** Artifacts written in the flow → agent that writes them. */
export function artifactOwners(spec: FlowRaw): Map<string, string> {
  const m = new Map<string, string>();
  for (const [an, a] of Object.entries(spec.agents))
    for (const w of a.writes ?? []) m.set(w.name, an);
  return m;
}

/** Typed flow inputs; a legacy flow has one text input `input` (also the run id). */
export function flowInputs(spec: FlowRaw): Record<string, IOTypeRaw> {
  if (spec.inputs && Object.keys(spec.inputs).length) return spec.inputs;
  return { [LEGACY_INPUT]: { type: "text", label: spec.input?.label ?? "Input", pattern: spec.input?.pattern } };
}

export const isTyped = (spec: FlowRaw) => Object.keys(spec.inputs ?? {}).length > 0;

/** The value type behind a `from` reference (null when it does not exist). */
export function sourceIO(spec: FlowRaw, ref: string | null | undefined): IOTypeRaw | null {
  if (!ref || !ref.includes(".")) return null;
  const [root, name] = [ref.slice(0, ref.indexOf(".")), ref.slice(ref.indexOf(".") + 1)];
  if (root === "input") return flowInputs(spec)[name] ?? null;
  return spec.agents[root]?.outputs?.[name] ?? null;
}

/** Resolved type of an agent input: its own, else its source's, else text. */
export function inputKind(spec: FlowRaw, i: AgentInputRaw): IOKind {
  return i.type || sourceIO(spec, i.from)?.type || "text";
}

/** Nodes with a path to `node` (edges into start are loops and do not count), as in spec.py. */
export function ancestors(spec: FlowRaw, node: string): Set<string> {
  const into = new Map<string, string[]>();
  for (const e of spec.edges ?? []) if (e.to !== spec.start) into.set(e.to, [...(into.get(e.to) ?? []), e.from]);
  const seen = new Set<string>();
  const q = [...(into.get(node) ?? [])];
  while (q.length) {
    const n = q.shift()!;
    if (!seen.has(n)) { seen.add(n); q.push(...(into.get(n) ?? [])); }
  }
  return seen;
}

/** Height of a node on the canvas (it grows with its ports). */
export function nodeHeight(spec: FlowRaw, id: string): number {
  const a = spec.agents[id];
  if (!a) return NODE_H;
  const rows = Math.max(Object.keys(a.inputs ?? {}).length + 1, Object.keys(a.outputs ?? {}).length);
  return NODE_H + rows * PORT_H + 24;  // + the ports block's border and padding
}

/** Automatic layout (top to bottom). Only moves the given nodes (all by default). */
export function autoLayout(spec: FlowRaw, only?: Set<string>): void {
  const g = new dagre.graphlib.Graph();
  g.setGraph({ rankdir: "TB", nodesep: 50, ranksep: 70 });
  g.setDefaultEdgeLabel(() => ({}));
  for (const n of nodeNames(spec)) g.setNode(n, { width: NODE_W, height: nodeHeight(spec, n) });
  for (const e of spec.edges ?? []) if (e.to !== spec.start) g.setEdge(e.from, e.to);
  dagre.layout(g);
  spec.layout = spec.layout ?? {};
  for (const n of nodeNames(spec)) {
    if (only && !only.has(n)) continue;
    const p = g.node(n);
    spec.layout[n] = { x: Math.round(p.x - NODE_W / 2), y: Math.round(p.y - nodeHeight(spec, n) / 2) };
  }
}

/** Ensures a position for every node (the missing ones come from the automatic layout). */
export function ensureLayout(doc: Doc): Doc {
  const missing = nodeNames(doc.spec).filter((n) => !doc.spec.layout?.[n]);
  if (!missing.length) return doc;
  const d = clone(doc);
  autoLayout(d.spec, Object.keys(d.spec.layout ?? {}).length ? new Set(missing) : undefined);
  return d;
}

const empty = (v: unknown) => v === undefined || v === null || v === "" || (Array.isArray(v) && v.length === 0)
  || (Number.isNaN(v as number));

/** Drops the fields of a typed value that are empty or at their default. */
function tidyIO(t: IOTypeRaw): void {
  for (const k of ["label", "description", "default", "min", "max", "pattern", "options", "accept", "contract"] as const)
    if (empty(t[k])) delete t[k];
  if (t.required === true) delete t.required;
  if (!t.multiple) delete t.multiple;
  if (!t.draft) delete t.draft;
  if (!t.type) t.type = "text";
}

/** Removes empty fields so the YAML stays clean (the server ignores default values). */
export function tidy(doc: Doc): Doc {
  const d = clone(doc);
  for (const a of Object.values(d.spec.agents)) {
    for (const k of ["tools", "skills", "reads", "writes", "connections", "write_connections"] as const)
      if (Array.isArray(a[k]) && a[k]!.length === 0) delete a[k];
    if (a.model === "" || a.model === null) delete a.model;
    if (!a.autonomy) delete a.autonomy;
    if (a.max_turns === null || Number.isNaN(a.max_turns)) delete a.max_turns;
    for (const i of Object.values(a.inputs ?? {})) {
      if (!i.from) delete i.from;
      if (!i.type) delete i.type;
      if (!i.description) delete i.description;
      if (i.required === true) delete i.required;
    }
    for (const o of Object.values(a.outputs ?? {})) tidyIO(o);
    if (a.inputs && !Object.keys(a.inputs).length) delete a.inputs;
    if (a.outputs && !Object.keys(a.outputs).length) delete a.outputs;
    if (!a.join || a.join === "all") delete a.join;
    if (a.max_runs === 3 || a.max_runs === null || Number.isNaN(a.max_runs)) delete a.max_runs;
  }
  for (const t of Object.values(d.spec.inputs ?? {})) tidyIO(t);
  if (d.spec.inputs && !Object.keys(d.spec.inputs).length) delete d.spec.inputs;
  for (const e of d.spec.edges ?? []) if (!e.if?.trim()) delete e.if;
  if (!d.spec.execution || d.spec.execution === "orchestrator") delete d.spec.execution;
  if (d.spec.max_parallel === 4 || d.spec.max_parallel === null || Number.isNaN(d.spec.max_parallel)) delete d.spec.max_parallel;
  if (d.spec.model === "" || d.spec.model === null) delete d.spec.model;
  return d;
}

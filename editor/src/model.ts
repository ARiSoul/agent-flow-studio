// Operações puras sobre o flow (cada uma recebe uma cópia e altera-a).
import dagre from "@dagrejs/dagre";
import type { AgentRaw, Doc, FlowRaw, GateRaw } from "./types";

export const SLUG = /^[a-z][a-z0-9_]{0,39}$/;
export const NODE_W = 240;
export const NODE_H = 96;

export const clone = <T,>(v: T): T => structuredClone(v);

export function nodeNames(spec: FlowRaw): string[] {
  return [...Object.keys(spec.agents), ...Object.keys(spec.gates ?? {})];
}

export function uniqueName(spec: FlowRaw, base: string): string {
  const used = new Set([...nodeNames(spec), ...Object.keys(spec.mcp_servers ?? {})]);
  for (let i = 1; ; i++) if (!used.has(`${base}_${i}`)) return `${base}_${i}`;
}

/** Renomeia mantendo a ordem das chaves e atualizando ligações, start e layout. */
export function renameNode(spec: FlowRaw, from: string, to: string): void {
  const rekey = <V,>(obj: Record<string, V> | undefined) =>
    obj && Object.fromEntries(Object.entries(obj).map(([k, v]) => [k === from ? to : k, v]));
  if (from in spec.agents) spec.agents = rekey(spec.agents)!;
  if (spec.gates && from in spec.gates) spec.gates = rekey(spec.gates);
  if (spec.layout) spec.layout = rekey(spec.layout);
  if (spec.start === from) spec.start = to;
  for (const e of spec.edges ?? []) {
    if (e.from === from) e.from = to;
    if (e.to === from) e.to = to;
  }
}

export function removeNode(spec: FlowRaw, id: string): void {
  delete spec.agents[id];
  if (spec.gates) delete spec.gates[id];
  if (spec.layout) delete spec.layout[id];
  spec.edges = (spec.edges ?? []).filter((e) => e.from !== id && e.to !== id);
}

export function newAgent(): AgentRaw {
  return { description: "Novo agente.", prompt: "Descreve aqui o que este agente faz.", tools: [] };
}

export function newGate(kind: GateRaw["kind"]): GateRaw {
  return { kind, label: kind === "human" ? "Aprovação humana" : "Paragem" };
}

/** Artefactos escritos no flow → agente que os escreve. */
export function artifactOwners(spec: FlowRaw): Map<string, string> {
  const m = new Map<string, string>();
  for (const [an, a] of Object.entries(spec.agents))
    for (const w of a.writes ?? []) m.set(w.name, an);
  return m;
}

/** Layout automático (de cima para baixo). Só move os nós indicados (por defeito todos). */
export function autoLayout(spec: FlowRaw, only?: Set<string>): void {
  const g = new dagre.graphlib.Graph();
  g.setGraph({ rankdir: "TB", nodesep: 50, ranksep: 70 });
  g.setDefaultEdgeLabel(() => ({}));
  for (const n of nodeNames(spec)) g.setNode(n, { width: NODE_W, height: NODE_H });
  for (const e of spec.edges ?? []) if (e.to !== spec.start) g.setEdge(e.from, e.to);
  dagre.layout(g);
  spec.layout = spec.layout ?? {};
  for (const n of nodeNames(spec)) {
    if (only && !only.has(n)) continue;
    const p = g.node(n);
    spec.layout[n] = { x: Math.round(p.x - NODE_W / 2), y: Math.round(p.y - NODE_H / 2) };
  }
}

/** Garante posição para todos os nós (os que faltam vêm do layout automático). */
export function ensureLayout(doc: Doc): Doc {
  const missing = nodeNames(doc.spec).filter((n) => !doc.spec.layout?.[n]);
  if (!missing.length) return doc;
  const d = clone(doc);
  autoLayout(d.spec, Object.keys(d.spec.layout ?? {}).length ? new Set(missing) : undefined);
  return d;
}

/** Remove campos vazios para o YAML ficar limpo (o servidor ignora valores por defeito). */
export function tidy(doc: Doc): Doc {
  const d = clone(doc);
  for (const a of Object.values(d.spec.agents)) {
    for (const k of ["tools", "skills", "reads", "writes", "connections", "write_connections"] as const)
      if (Array.isArray(a[k]) && a[k]!.length === 0) delete a[k];
    if (a.model === "" || a.model === null) delete a.model;
    if (!a.autonomy) delete a.autonomy;
    if (a.max_turns === null || Number.isNaN(a.max_turns)) delete a.max_turns;
  }
  if (d.spec.model === "" || d.spec.model === null) delete d.spec.model;
  return d;
}

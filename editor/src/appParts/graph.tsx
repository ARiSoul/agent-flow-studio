/** The canvas graph (React Flow nodes and links), derived from the flow: the flow is the single
 * source of truth; run state, validation errors and data links are drawn on it. */
import { useMemo } from "react";
import { MarkerType, type Edge, type Node } from "@xyflow/react";
import { CONTEXT_NODE, edgeKey } from "../RunPanel";
import { inputKind, nodeSize, savedSize, sourceIO } from "../model";
import { type Port } from "../nodes";
import { dataEdgeId } from "./shared";
import type { EditorCtx } from "./shared";

export function useCanvasGraph(c: Pick<EditorCtx, "catalog" | "dims" | "doc" | "nodeStyle" | "sel" | "showData" | "t" | "trace" | "val">) {
  const { catalog, dims, doc, nodeStyle, sel, showData, t, trace, val } = c;
  // ---- React Flow graph (derived from the flow; the flow is the single source of truth)
  const errNodes = useMemo(() => new Set((val?.errors ?? []).flatMap((e) => e.nodes)), [val]);
  const nodes: Node[] = useMemo(() => {
    if (!doc) return [];
    const s = doc.spec, pos = (id: string) => { const p = s.layout?.[id]; return { x: p?.x ?? 0, y: p?.y ?? 0 }; };
    // a size the person set (resize handles of the selected node); not for avatars
    const size = (id: string) => {
      const S = savedSize(s, id, nodeStyle);
      if (!S) return {};
      return { width: S.w, height: nodeSize(s, id, nodeStyle).h, className: "sized" };  // never hides the ports
    };
    return [
      ...Object.entries(s.agents).map(([id, a]) => {
        const ins: Port[] = Object.entries(a.inputs ?? {}).map(([n, i]) => ({
          name: n, type: inputKind(s, i), multiple: !!sourceIO(s, i.from)?.multiple, from: i.from,
        }));
        const outs: Port[] = Object.entries(a.outputs ?? {}).map(([n, o]) => ({
          name: n, type: o.type ?? "text", multiple: !!o.multiple,
        }));
        return {
          id, type: "agent", position: pos(id), measured: dims[id], ...size(id),
          selected: sel?.kind === "node" && sel.id === id,
          data: { name: id, agent: a, start: s.start === id, error: errNodes.has(id), run: trace.nodes[id], ins, outs,
            style: nodeStyle, lib: a.origin ? val?.origins?.[id] : undefined,
            model: a.action || a.subflow ? null
              : a.model && a.model !== "inherit" ? { name: a.model, from: "agent" as const }
              : s.model ? { name: s.model, from: "flow" as const }
              : { name: catalog?.default_model ?? null, from: "studio" as const } },
        };
      }),
      ...Object.entries(s.gates ?? {}).map(([id, g]) => ({
        id, type: "gate", position: pos(id), measured: dims[id], ...size(id),
        selected: sel?.kind === "node" && sel.id === id,
        data: { name: id, gate: g, error: errNodes.has(id), run: trace.nodes[id], style: nodeStyle },
      })),
      // "always" in a workspace: the context update is the last (system) step of every run
      ...(catalog?.workspace && catalog.workspace.name !== "studio" && s.context_update === "always" ? [(() => {
        const ps = Object.values(s.layout ?? {});
        const x = ps.length ? Math.min(...ps.map((p) => p.x)) : 0;
        const y = ps.length ? Math.max(...ps.map((p) => p.y)) + 220 : 0;
        return { id: CONTEXT_NODE, type: "context", position: { x, y }, draggable: false, selectable: false,
          deletable: false, connectable: false,
          data: { run: trace.nodes[CONTEXT_NODE], label: t("ctx.nodeLabel"), hint: t("ctx.nodeHint"), badge: t("ctx.nodeBadge") } };
      })()] : []),
    ];
  }, [doc, sel, errNodes, dims, trace, nodeStyle, val, catalog, t]);
  const edges: Edge[] = useMemo(() => {
    if (!doc) return [];
    const s = doc.spec;
    const control: Edge[] = (s.edges ?? []).map((e, i) => {
      // run state of the link: followed (green; moving while its target runs), not followed (faded)
      const run = trace.edges[edgeKey(e.from, e.to)];
      // a branch label ("yes", "no") reads better than the expression; the expression stays in the panel
      const cond = e.label?.trim() || [e.if?.trim(), e.when?.trim()].filter(Boolean).join(" · ");
      const fromDecision = s.gates?.[e.from]?.kind === "decision";
      const mark = run === "followed" ? "✓" : run === "not" ? "✗" : run === "error" ? "!" : "";
      return {
        id: `e${i}`, source: e.from, target: e.to, sourceHandle: "c", targetHandle: "c",  // the control points (not a data port)
        label: [mark, cond].filter(Boolean).join(" ") || undefined, labelBgPadding: [4, 2] as [number, number], labelBgBorderRadius: 3,
        selected: sel?.kind === "edge" && sel.index === i, type: "smoothstep",
        markerEnd: { type: MarkerType.ArrowClosed },
        animated: run === "followed" && trace.nodes[e.to] === "active",
        ariaLabel: run ? t(`run.edge.${run}`) : undefined,
        className: [e.to === s.start || (e.from in (s.gates ?? {}) && !fromDecision) ? "back" : "",
          fromDecision ? (e.if?.trim() ? "branch" : "branch else") : "", e.if?.trim() ? "cond" : "",
          run ? `run-${run}` : ""].join(" ").trim(),
      };
    });
    if (!showData) return control;
    const data: Edge[] = [];
    for (const [an, a] of Object.entries(s.agents))
      for (const [n, i] of Object.entries(a.inputs ?? {})) {
        const ref = i.from ?? "";
        const root = ref.slice(0, ref.indexOf("."));
        const out = ref.slice(ref.indexOf(".") + 1);
        if (!ref.includes(".") || root === "input" || !s.agents[root]?.outputs?.[out]) continue;
        data.push({
          id: dataEdgeId(an, n), source: root, target: an, sourceHandle: `out:${out}`, targetHandle: `in:${n}`,
          className: "data", type: "default", selected: sel?.kind === "data" && sel.agent === an && sel.input === n,
        });
      }
    return [...data, ...control];  // labelled links on top: no data link crosses a label
  }, [doc, sel, showData, trace, t]);
  return { errNodes, nodes, edges };
}

"""Graph helpers shared by the compiler (orchestrator prompt) and the graph engine.

- Back edges: an edge that closes a loop. A depth-first walk from `start` (edges in the order of
  the flow) marks an edge as a back edge when its target is still on the walk's stack, i.e. it
  goes back to a node the source descends from. Every edge into `start` is a back edge.
- Forward edges are all the others: they decide when a node is ready.
- Parallel groups: agents at the same depth of the forward graph (longest path from `start`,
  data dependencies included) have no path between them, so they can run at the same time.
"""
from __future__ import annotations

from collections import deque
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .spec import FlowSpec


def back_edges(spec: FlowSpec) -> set[int]:
    """Indexes (in `spec.edges`) of the edges that close a loop."""
    out: dict[str, list[int]] = {}
    for i, e in enumerate(spec.edges):
        out.setdefault(e.from_, []).append(i)
    back: set[int] = set()
    on_stack: set[str] = set()
    visited: set[str] = set()

    # iterative DFS (flows are small, but keep it safe from the recursion limit)
    stack: list[tuple[str, int]] = [(spec.start, 0)]
    on_stack.add(spec.start)
    visited.add(spec.start)
    while stack:
        node, k = stack[-1]
        edges = out.get(node, [])
        if k >= len(edges):
            stack.pop()
            on_stack.discard(node)
            continue
        stack[-1] = (node, k + 1)
        i = edges[k]
        to = spec.edges[i].to
        if to in on_stack:
            back.add(i)
        elif to not in visited:
            visited.add(to)
            on_stack.add(to)
            stack.append((to, 0))
    for i, e in enumerate(spec.edges):  # also edges outside the walk (unreachable sources)
        if e.to == spec.start:
            back.add(i)
    return back


def forward_reach(spec: FlowSpec, node: str, back: set[int] | None = None) -> set[str]:
    """`node` and every node it reaches through forward edges."""
    back = back_edges(spec) if back is None else back
    out: dict[str, list[str]] = {}
    for i, e in enumerate(spec.edges):
        if i not in back:
            out.setdefault(e.from_, []).append(e.to)
    seen, q = {node}, deque([node])
    while q:
        for nxt in out.get(q.popleft(), []):
            if nxt not in seen:
                seen.add(nxt)
                q.append(nxt)
    return seen


def depths(spec: FlowSpec, back: set[int] | None = None) -> dict[str, int]:
    """Longest forward path from `start` to each reachable node (agents and gates). An input
    `from` another agent counts as an edge too (the consumer runs after its source)."""
    back = back_edges(spec) if back is None else back
    preds: dict[str, set[str]] = {}
    for i, e in enumerate(spec.edges):
        if i not in back:
            preds.setdefault(e.to, set()).add(e.from_)
    reach = forward_reach(spec, spec.start, back)
    for name, a in spec.agents.items():
        for inp in a.inputs.values():
            root = inp.from_.split(".", 1)[0] if inp.from_ else None
            if root in spec.agents and root != name and root in reach:
                preds.setdefault(name, set()).add(root)
    depth: dict[str, int] = {}
    visiting: set[str] = set()

    def walk(n: str) -> int:
        if n in depth:
            return depth[n]
        if n in visiting:  # a data dependency against the edges: ignore it
            return 0
        visiting.add(n)
        ps = [p for p in preds.get(n, ()) if p in reach and n != spec.start]
        d = 1 + max((walk(p) for p in ps), default=-1)
        visiting.discard(n)
        depth[n] = d
        return d

    for n in sorted(reach):
        walk(n)
    return depth


def parallel_groups(spec: FlowSpec) -> list[list[str]]:
    """Groups (2+ agents) that can run at the same time, in flow order."""
    d = depths(spec)
    order = {n: i for i, n in enumerate(spec.agent_order())}
    by_depth: dict[int, list[str]] = {}
    for n, k in d.items():
        if n in spec.agents:
            by_depth.setdefault(k, []).append(n)
    groups = [sorted(g, key=lambda n: order.get(n, 0)) for _, g in sorted(by_depth.items())]
    return [g for g in groups if len(g) > 1]

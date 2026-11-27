"""From the orchestrator to the graph engine: what changes when a flow switches `execution`.

The orchestrator (an LLM) is for short flows: it reads the graph as instructions and passes
context between agents in its own words. The graph engine runs each agent in its own session and
gives it only what the flow declares, so a flow that relied on the orchestrator's words needs
typed inputs and outputs (or artifacts). `graph_notes` says where, before the person converts;
the conversion itself only sets `execution: graph` in the draft (the person saves it).
"""
from __future__ import annotations

from typing import Any

from ..messages import Msg, msg
from .spec import FlowSpec

ORCH_MAX_AGENTS = 3  # the orchestrator is meant for flows this short (a validation warning above)
# conditions in words that only say "go on": the graph engine would ask its router for nothing
TRIVIAL_WHEN = frozenset({"finished", "done", "always", "next", "then", "after", "when finished",
                          "when done", "on finish", "on success", "completed", "when complete"})


def trivial_when(text: str) -> bool:
    return text.strip().strip(".").lower() in TRIVIAL_WHEN


def _agent_after(spec: FlowSpec) -> dict[str, list[str]]:
    """agent → the agents that come right after it (through gates too)."""
    nxt: dict[str, list[str]] = {}
    for e in spec.edges:
        nxt.setdefault(e.from_, []).append(e.to)
    out: dict[str, list[str]] = {}
    for n in spec.agents:
        seen, stack, found = set(), list(nxt.get(n, [])), []
        while stack:
            x = stack.pop()
            if x in seen or x == spec.start or x == n:  # its own loop does not count
                continue
            seen.add(x)
            if x in spec.agents:
                found.append(x)
            else:  # a gate: look past it
                stack.extend(nxt.get(x, []))
        out[n] = found
    return out


def graph_notes(spec: FlowSpec) -> list[tuple[str, Msg, list[str]]]:
    """(level "warn" | "info", message, nodes) for each thing that works differently in the graph
    engine. Warnings mean an agent would get less than it got from the orchestrator."""
    notes: list[tuple[str, Msg, list[str]]] = []
    model_agents = {n: a for n, a in spec.agents.items() if not a.runs_itself}
    after = _agent_after(spec)
    for n, a in model_agents.items():
        if n != spec.start and not a.inputs and not a.reads:
            notes.append(("warn", msg(
                "convert.no_inputs",
                "agent {agent} gets only the flow task: in the orchestrator, the orchestrator told "
                "it what earlier agents found. Give it inputs from their outputs (or let it read "
                "their artifacts)", agent=n), [n]))
        if after.get(n) and not a.outputs and not a.writes:
            notes.append(("warn", msg(
                "convert.no_outputs",
                "agent {agent} saves no outputs: the agents after it ({next}) cannot get its "
                "results. Give it typed outputs (or artifacts)",
                agent=n, next=", ".join(after[n])), [n]))
    for e in spec.edges:
        if trivial_when(e.when or ""):
            notes.append(("info", msg(
                "convert.when_cleared",
                "the link {src} → {dst} said only \"{when}\": it is cleared (it always goes on)",
                src=e.from_, dst=e.to, when=e.when.strip()), [e.from_, e.to]))
        elif (e.when or "").strip() and not (e.if_ or "").strip():
            notes.append(("info", msg(
                "convert.when",
                "the link {src} → {dst} has a condition in words (\"{when}\"): the graph engine "
                "asks a small model each time; an `if` expression is exact and costs nothing",
                src=e.from_, dst=e.to, when=e.when.strip()), [e.from_, e.to]))
    if spec.orchestrator.prompt or spec.orchestrator.prompt_file:
        notes.append(("info", msg(
            "convert.orch_prompt",
            "the orchestrator prompt is not used by the graph engine: put the rules that matter "
            "into the agents' prompts"), []))
    return notes


def orchestrator_limit(spec: FlowSpec) -> Msg | None:
    """A warning when an orchestrator flow is longer than the orchestrator is meant for."""
    if spec.execution == "graph":
        return None
    n = sum(1 for a in spec.agents.values() if not a.runs_itself)
    if n <= ORCH_MAX_AGENTS:
        return None
    return msg("validate.orch_large",
               "the orchestrator is for short flows (up to {max} agents) and this one has {n}: "
               "convert it to the graph engine (Flow properties) to resume, retry and pause its "
               "runs", max=ORCH_MAX_AGENTS, n=n)


def to_graph(raw: dict[str, Any]) -> dict[str, Any]:
    """The draft with `execution: graph`; a condition in words that only says "go on" is cleared
    (the graph engine would ask its router model for it every time). Nothing else changes."""
    edges = []
    for e in raw.get("edges") or []:
        if isinstance(e, dict) and isinstance(e.get("when"), str) and trivial_when(e["when"]):
            e = {k: v for k, v in e.items() if k != "when"}
        edges.append(e)
    return {**raw, "execution": "graph", **({"edges": edges} if "edges" in raw else {})}

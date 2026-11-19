"""Graph engine (`execution: graph`): the studio runs the flow graph itself, without an LLM
orchestrator.

- Each agent runs as its own SDK session (`compile_agent`): its prompt, its tools only, a guard
  hook with a fixed identity and the shared write policy of the run (approvals, autonomy).
- A node is ready when all its incoming forward edges are decided (back edges excluded, see
  `builder/graph.py`). If at least one fired, it runs (`join: all`); with `join: any` it runs at
  the first edge that fires. If none fired, it is skipped and the skip propagates.
- Ready agents run at the same time, up to `max_parallel` (asyncio tasks).
- Edges: `if` is evaluated here (`builder/expr.py`) with the saved inputs/outputs/decisions;
  `when` is asked to a small router (one call per node, no tools); an edge with both needs both;
  an edge with neither always fires.
- A back edge that fires (a loop) runs that part of the graph again: the loop target and the
  nodes it reaches are reset (nodes that are still running are left alone), limited by the
  `max_runs` of every agent in that part. The loop wins over forward edges fired by the same node.
  Each agent's outputs are cleared right before it runs, so a loop target can still read the
  outputs of the node that sent it back.
- Required outputs missing after a session: one follow-up message in the same session; if they
  are still missing, the node fails and the run ends with an error (after running nodes finish).
- Gates: a human gate pauses the run (no new nodes start; running ones finish); the decision
  (`<gate>.decision` / `.comment`) feeds the conditions of its edges. A stop gate ends the run.
- Test modes: `step` runs ONE agent node and pauses (gates and skips around it are processed at
  once); `until` runs, in parallel, only the target and the nodes that lead to it, then pauses;
  `agent` runs only the target (inputs from the saved outputs or the given values) and does not
  follow its edges.
"""
from __future__ import annotations

import asyncio
import json
import re
from collections import deque
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Literal, Protocol

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ResultMessage,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
    query,
)

from .builder import expr
from .builder.compile import DISALLOWED, CompiledGraph
from .builder.graph import back_edges, forward_reach
from .hooks import DELEGATION_TOOLS, make_guard, make_hooks
from .tools import io as io_tool

QueryFn = Callable[..., AsyncIterator[Any]]
# route_fn(node, data, conditions) -> (one bool per condition, cost in USD)
RouteFn = Callable[[str, dict[str, Any], list[str]], Awaitable[tuple[list[bool], float]]]
Emit = Callable[[dict[str, Any]], None]
EngineMode = Literal["full", "step", "until", "agent"]

ROUTER_MODEL = "haiku"
ROUTER_NAME = "_router"  # identity of the router session in the audit log
ROUTER_PROMPT = (
    "You check conditions for one step of an automated flow. You get the step's outputs (JSON) "
    "and a numbered list of conditions in plain language. For each condition, decide whether it "
    "holds, using only the given data; when you cannot tell, it does not hold. Answer ONLY with "
    'JSON, with one true/false per condition in the same order: {"holds": [true, false]}'
)
MAX_ROUTER_DATA = 20_000
MAX_VALUE_IN_PROMPT = 4_000
FOLLOW_UP = ("You finished without saving these required outputs: {names}. Save each one now "
             "with `set_output` (run_id `{run_id}`, agent `{agent}`), then finish.")


class Host(Protocol):
    """What the engine updates on the run (the editor's `Run`, or the CLI's state)."""

    cost: float
    session_id: str | None
    done_agents: set[str]


@dataclass
class Outcome:
    status: Literal["done", "gate", "paused", "error"]
    gate: dict[str, Any] | None = None
    message: str = ""


class RouteError(ValueError):
    pass


# --- router (`when` conditions) ----------------------------------------------------------------
def router_options(model: str = ROUTER_MODEL) -> ClaudeAgentOptions:
    """No tools at all (and a hook that denies any call), no user settings, one turn."""
    return ClaudeAgentOptions(
        system_prompt=ROUTER_PROMPT,
        tools=[],
        allowed_tools=[],
        disallowed_tools=[*DISALLOWED, *sorted(DELEGATION_TOOLS)],
        mcp_servers={},
        permission_mode="dontAsk",
        setting_sources=[],
        hooks=make_hooks(make_guard([], fixed_agent=ROUTER_NAME)),
        max_turns=1,
        model=model,
    )


def router_message(node: str, data: dict[str, Any], conditions: list[str]) -> str:
    text = json.dumps(data, ensure_ascii=False, default=str)
    if len(text) > MAX_ROUTER_DATA:
        text = text[:MAX_ROUTER_DATA] + "…(cut)"
    lines = [f"Step: `{node}`", "Data:", text, "", "Conditions:"]
    lines += [f"{i}. {c}" for i, c in enumerate(conditions, 1)]
    return "\n".join(lines)


def parse_route(text: str, n: int) -> list[bool]:
    """{"holds": [bool × n]} somewhere in the answer (the last valid object wins)."""
    candidates = [text.strip(), *reversed(re.findall(r"\{[^{}]*\}", text or ""))]
    for c in candidates:
        try:
            data = json.loads(c)
        except ValueError:
            continue
        holds = data.get("holds") if isinstance(data, dict) else None
        if isinstance(holds, list) and len(holds) == n and all(isinstance(h, bool) for h in holds):
            return holds
    raise RouteError(f"the router did not answer {{\"holds\": [{n} × true/false]}}")


def make_llm_router(query_fn: QueryFn = query, model: str = ROUTER_MODEL) -> RouteFn:
    async def route(node: str, data: dict[str, Any], conditions: list[str]
                    ) -> tuple[list[bool], float]:
        text, cost = "", 0.0
        async for msg in query_fn(prompt=router_message(node, data, conditions),
                                  options=router_options(model)):
            if isinstance(msg, AssistantMessage):
                text += "".join(b.text for b in msg.content if isinstance(b, TextBlock))
            elif isinstance(msg, ResultMessage):
                cost += msg.total_cost_usd or 0.0
                text = msg.result or text
        return parse_route(text, len(conditions)), cost

    return route


# --- engine ------------------------------------------------------------------------------------
def _short_tool(name: str) -> str:
    return name.split("__")[-1] if name.startswith("mcp__") else name


def _show(v: Any) -> tuple[str, bool]:
    text = v if isinstance(v, str) else json.dumps(v, ensure_ascii=False, default=str)
    if len(text) > MAX_VALUE_IN_PROMPT:
        return text[:MAX_VALUE_IN_PROMPT] + "…", True
    return text, False


@dataclass
class _Result:
    ok: bool
    message: str = ""
    evals: list[tuple[int, bool, str, str | None]] | None = None  # (edge, fired, by, error)


class GraphEngine:
    """State of one graph run. The runner keeps it on the `Run`, so `continue_` and `decide`
    resume where it stopped. `run()` returns when nothing more can start (end, gate, pause)."""

    def __init__(self, compiled: CompiledGraph, run_id: str, *, host: Host, emit: Emit,
                 query_fn: QueryFn = query, route_fn: RouteFn | None = None,
                 mode: EngineMode = "full", target: str | None = None, instruction: str = ""):
        spec = compiled.spec
        if mode in ("until", "agent") and target not in spec.agents:
            raise ValueError(f"agent {target!r} does not exist in the flow")
        self.compiled, self.spec, self.run_id = compiled, spec, run_id
        self.host, self.emit = host, emit
        self._query = query_fn
        self._route = route_fn or make_llm_router(query_fn)
        self.mode: EngineMode = mode
        self.target = target
        self.instruction = instruction.strip()
        self.back = back_edges(spec)
        self.fwd_in: dict[str, list[int]] = {}
        for i, e in enumerate(spec.edges):
            if i not in self.back:
                self.fwd_in.setdefault(e.to, []).append(i)
        self.state: dict[str, str] = {n: "waiting" for n in [*spec.agents, *spec.gates]}
        self.decided: dict[int, bool] = {}
        self.runs: dict[str, int] = dict.fromkeys(spec.agents, 0)
        self.ready: list[str] = []
        self.tasks: dict[asyncio.Task[_Result], str] = {}
        self.pending_gates: list[dict[str, Any]] = []
        self.stop_gate: dict[str, Any] | None = None
        self.failure: str | None = None
        self.sessions: dict[str, str] = {}
        self.summaries: dict[str, str] = {}
        self.reasons: dict[str, list[str]] = {}
        self.step_budget: int | None = None
        self.until_set = self._leads_to(target) if mode == "until" and target else set()
        self.started = False

    # --- public
    def set_mode(self, mode: EngineMode) -> None:
        """continue_: "step" (one more node) or "full" (to the end)."""
        self.mode = mode

    async def run(self) -> Outcome:
        self.compiled.activate()
        self.step_budget = 1 if self.mode == "step" else None
        if not self.started:
            self.started = True
            first = self.target if self.mode == "agent" else self.spec.start
            assert first is not None
            self._make_ready(first)
        try:
            while True:
                self._launch()
                if not self.tasks:
                    break
                done, _ = await asyncio.wait(set(self.tasks),
                                             return_when=asyncio.FIRST_COMPLETED)
                for t in done:
                    node = self.tasks.pop(t)
                    self._finish(node, t.result())
        except asyncio.CancelledError:
            await self.cancel()
            raise
        return self._outcome()

    async def resolve_gate(self, gate: str) -> None:
        """After a human decision (already in io `_gates.json`): follow the gate's edges."""
        self.pending_gates = [g for g in self.pending_gates if g["gate"] != gate]
        self.state[gate] = "done"
        self._apply(gate, await self._evaluate(gate))

    async def cancel(self) -> None:
        for t in self.tasks:
            t.cancel()
        if self.tasks:
            await asyncio.gather(*self.tasks, return_exceptions=True)
        self.tasks.clear()

    # --- scheduling
    def _leads_to(self, target: str) -> set[str]:
        """`target` and the nodes with a forward path to it."""
        into: dict[str, list[str]] = {}
        for i, e in enumerate(self.spec.edges):
            if i not in self.back:
                into.setdefault(e.to, []).append(e.from_)
        seen, q = {target}, deque([target])
        while q:
            for p in into.get(q.popleft(), []):
                if p not in seen:
                    seen.add(p)
                    q.append(p)
        return seen

    def _launch(self) -> None:
        if self.failure or self.stop_gate or self.pending_gates:
            return  # running nodes finish; nothing new starts
        if (self.mode == "until" and self.target
                and self.state[self.target] in ("done", "skipped", "failed")):
            return
        i = 0
        while i < len(self.ready) and len(self.tasks) < self.spec.max_parallel:
            if self.step_budget is not None and self.step_budget <= 0:
                return
            node = self.ready[i]
            if self.mode == "until" and node not in self.until_set:
                i += 1
                continue
            self.ready.pop(i)
            if self.step_budget is not None:
                self.step_budget -= 1
            self.state[node] = "running"
            self.runs[node] += 1
            self.tasks[asyncio.create_task(self._agent(node))] = node

    def _make_ready(self, node: str) -> None:
        if node in self.spec.gates:
            self._reach_gate(node)
        elif self.state[node] == "waiting":
            self.state[node] = "ready"
            self.ready.append(node)

    def _reach_gate(self, gate: str) -> None:
        g = self.spec.gates[gate]
        info = {"gate": gate, "kind": g.kind, "label": g.label,
                "reason": "; ".join(self.reasons.get(gate, []))[:500]}
        self.emit({"type": "gate", **info})
        if g.kind == "stop":
            self.state[gate] = "done"
            self.stop_gate = self.stop_gate or info
        else:
            self.state[gate] = "gate"
            self.pending_gates.append(info)

    def _check(self, node: str) -> None:
        if self.state.get(node) != "waiting":
            return  # already ready, running, done… (`join: any`: later edges change nothing)
        vals = [self.decided.get(i) for i in self.fwd_in.get(node, [])]
        a = self.spec.agents.get(node)
        if a is not None and a.join == "any" and any(v is True for v in vals):
            self._make_ready(node)
        elif all(v is not None for v in vals):
            if any(vals):
                self._make_ready(node)
            else:
                self._skip(node)

    def _skip(self, node: str) -> None:
        self.state[node] = "skipped"
        self.emit({"type": "skipped", "node": node})
        for i, e in enumerate(self.spec.edges):
            if e.from_ == node and i not in self.back:
                self.decided[i] = False
                self._check(e.to)

    def _finish(self, node: str, res: _Result) -> None:
        if not res.ok:
            self.state[node] = "failed"
            self.failure = self.failure or f"agent {node}: {res.message}"
            return
        self.state[node] = "done"
        self.host.done_agents.add(node)
        if self.mode != "agent":
            self._apply(node, res.evals or [])

    def _apply(self, node: str, evals: list[tuple[int, bool, str, str | None]]) -> None:
        """Emits the edge events, then applies forward edges, then loops (a loop wins)."""
        loops: list[int] = []
        forward: list[tuple[int, bool]] = []
        for i, fired, by, error in evals:
            e = self.spec.edges[i]
            ev: dict[str, Any] = {"type": "edge", "from": e.from_, "to": e.to, "fired": fired,
                                  "by": by}
            if error:
                ev["error"] = error
            if i in self.back:
                if fired and not self._loop_allowed(i):
                    ev["fired"], ev["limit"] = False, True
                elif fired:
                    loops.append(i)
            else:
                forward.append((i, fired))
            self.emit(ev)
        for i, fired in forward:
            e = self.spec.edges[i]
            self.decided[i] = fired
            if fired:
                self.reasons.setdefault(e.to, []).append(
                    f"{node}: {e.if_ or e.when or 'next'}")
            self._check(e.to)
        for i in loops:
            self._loop(i)

    def _loop_allowed(self, i: int) -> bool:
        e = self.spec.edges[i]
        part = forward_reach(self.spec, e.to, self.back)
        over = sorted(a for a in part if a in self.spec.agents
                      and self.runs[a] >= self.spec.agents[a].max_runs)
        if over:
            self.emit({"type": "loop_limit", "from": e.from_, "to": e.to, "agents": over})
            return False
        return True

    def _loop(self, i: int) -> None:
        e = self.spec.edges[i]
        part = forward_reach(self.spec, e.to, self.back)
        for n in part:
            if self.state[n] == "running":
                continue
            self.state[n] = "waiting"
            if n in self.ready:
                self.ready.remove(n)
            self.pending_gates = [g for g in self.pending_gates if g["gate"] != n]
            self.host.done_agents.discard(n)
            self.reasons.pop(n, None)
        for j, ed in enumerate(self.spec.edges):
            if j not in self.back and ed.from_ in part and ed.to in part:
                self.decided.pop(j, None)
        self.emit({"type": "loop", "from": e.from_, "to": e.to, "nodes": sorted(part)})
        self.reasons[e.to] = [f"{e.from_}: {e.if_ or e.when or 'loop'}"]
        self._make_ready(e.to)

    def _outcome(self) -> Outcome:
        if self.failure:
            return Outcome("error", message=self.failure)
        if self.stop_gate:
            return Outcome("done", gate=self.stop_gate)
        if self.pending_gates:
            return Outcome("gate", gate=self.pending_gates[0])
        if self.mode in ("step", "until") and self.ready:
            return Outcome("paused")
        return Outcome("done")

    # --- one agent
    async def _agent(self, name: str) -> _Result:
        a = self.spec.agents[name]
        self.emit({"type": "delegate", "agent": name, "task": a.description[:120]})
        try:
            values, missing = io_tool.resolve_inputs(self.spec, self.run_id, name)
            if missing:
                return self._failed(name, "required inputs without a value: "
                                    + ", ".join(missing))
            io_tool.clear_outputs(self.run_id, name)
            sid, error = await self._session(name, self._message(name, values),
                                             self.compiled.agent_options(name))
            if error:
                return self._failed(name, error)
            lacking = self._missing_outputs(name)
            if lacking and sid:
                self.emit({"type": "follow_up", "agent": name, "missing": lacking})
                prompt = FOLLOW_UP.format(names=", ".join(f"`{n}`" for n in lacking),
                                          run_id=self.run_id, agent=name)
                _, error = await self._session(name, prompt,
                                               self.compiled.agent_options(name, resume=sid))
                if error:
                    return self._failed(name, error)
                lacking = self._missing_outputs(name)
            if lacking:
                return self._failed(name, "required outputs not saved: " + ", ".join(lacking))
            if a.outputs:
                self.emit({"type": "outputs", "agent": name,
                           "names": sorted(io_tool.outputs(self.run_id, name))})
            self.emit({"type": "agent_done", "agent": name, "error": False})
            evals = [] if self.mode == "agent" else await self._evaluate(name)
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001 — the node fails, the run reports it
            return self._failed(name, f"{type(e).__name__}: {e}")
        return _Result(True, evals=evals)

    def _failed(self, name: str, message: str) -> _Result:
        self.emit({"type": "agent_done", "agent": name, "error": True, "message": message[:500]})
        return _Result(False, message)

    def _missing_outputs(self, name: str) -> list[str]:
        saved = io_tool.outputs(self.run_id, name)
        return [n for n, t in self.spec.agents[name].outputs.items()
                if t.required and saved.get(n) in (None, "", [])]

    def _flow_task(self) -> str:
        text = self.spec.task.replace("{run_id}", self.run_id).replace("{input}", self.run_id)
        values = io_tool.flow_inputs(self.run_id)
        for n in self.spec.inputs:
            text = text.replace("{" + n + "}", _show(values.get(n, ""))[0][:200])
        return text

    def _message(self, name: str, values: dict[str, Any]) -> str:
        a = self.spec.agents[name]
        lines = [f"You are the agent `{name}` of the flow `{self.spec.name}`.",
                 f"Run id: `{self.run_id}` (pass it as `run_id` to the tools"
                 + (f", with `agent: \"{name}\"` to the io tools" if a.inputs or a.outputs
                    else "") + ").",
                 f"Flow task: {self._flow_task()}"]
        if values:
            lines.append("\nYour inputs:")
            cut = False
            for n, v in values.items():
                text, was_cut = _show(v)
                cut = cut or was_cut
                lines.append(f"- `{n}`: {text}")
            if cut:
                lines.append("(Long values are cut here: call `get_inputs` for the full values.)")
        if a.outputs:
            outs = [f"`{n}`" + ("" if t.required else " (optional)") for n, t in a.outputs.items()]
            lines.append("\nBefore you finish, save your outputs with `set_output`: "
                         + ", ".join(outs) + ".")
        if self.instruction:
            lines += ["", "Instruction for this test run:", self.instruction]
        return "\n".join(lines)

    async def _session(self, name: str, prompt: str, options: ClaudeAgentOptions
                       ) -> tuple[str | None, str | None]:
        """One SDK session (or a resumed one). Returns (session_id, error)."""
        sid: str | None = None
        error: str | None = None
        async for msg in self._query(prompt=prompt, options=options):
            if isinstance(msg, AssistantMessage):
                for b in msg.content:
                    if isinstance(b, TextBlock) and b.text.strip():
                        self.summaries[name] = b.text
                        self.emit({"type": "text", "agent": name, "text": b.text})
                    elif isinstance(b, ToolUseBlock):
                        ev: dict[str, Any] = {"type": "tool", "agent": name,
                                              "tool": _short_tool(b.name)}
                        if _short_tool(b.name) in ("save_artifact", "read_artifact"):
                            ev["artifact"] = str(b.input.get("name", ""))[:80]
                        self.emit(ev)
            elif isinstance(msg, UserMessage) and isinstance(msg.content, list):
                for b in msg.content:
                    if isinstance(b, ToolResultBlock) and b.is_error:
                        self.emit({"type": "tool_error", "agent": name})
            elif isinstance(msg, ResultMessage):
                sid = msg.session_id
                self.sessions[name] = sid
                self.host.session_id = sid
                self.host.cost += msg.total_cost_usd or 0.0
                if msg.result:
                    self.summaries[name] = msg.result
                self.emit({"type": "result", "agent": name, "session_id": sid,
                           "cost": msg.total_cost_usd, "turns": msg.num_turns,
                           "is_error": msg.is_error, "subtype": msg.subtype,
                           "denials": len(msg.permission_denials or [])})
                if msg.is_error:
                    detail = "; ".join(msg.errors or []) or msg.subtype
                    error = f"the session ended with an error ({detail})"
        return sid, error

    # --- conditions
    async def _evaluate(self, node: str) -> list[tuple[int, bool, str, str | None]]:
        idxs = [i for i, e in enumerate(self.spec.edges) if e.from_ == node]
        if not idxs:
            return []
        scope = io_tool.scope(self.spec, self.run_id)
        res: dict[int, list[Any]] = {}
        ask: list[int] = []
        for i in idxs:
            e = self.spec.edges[i]
            by = ("both" if e.if_ and e.when else "if" if e.if_
                  else "when" if e.when else "always")
            holds, error = True, None
            if e.if_:
                try:
                    holds = expr.evaluate(e.if_, scope)
                except expr.ExprError as x:
                    holds, error = False, str(x)
            if e.when and holds:
                ask.append(i)
            res[i] = [holds, by, error]
        if ask:
            conditions = [self.spec.edges[i].when for i in ask]
            if node in self.spec.agents:
                data = {"outputs": scope.get(node, {}),
                        "summary": self.summaries.get(node, "")[:MAX_VALUE_IN_PROMPT]}
            else:
                data = dict(scope.get(node, {}))
            try:
                holds_list, cost = await self._route(node, data, conditions)
                if len(holds_list) != len(conditions):
                    raise RouteError(f"{len(holds_list)} answers for {len(conditions)} conditions")
                self.host.cost += cost or 0.0
            except asyncio.CancelledError:
                raise
            except Exception as x:  # noqa: BLE001 — a router failure: the conditions do not hold
                self.emit({"type": "route_error", "node": node, "message": str(x)[:300]})
                holds_list = [False] * len(conditions)
            for i, h in zip(ask, holds_list, strict=True):
                res[i][0] = bool(h)
        return [(i, bool(res[i][0]), str(res[i][1]), res[i][2]) for i in idxs]

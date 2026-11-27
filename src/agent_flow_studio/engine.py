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
import contextlib
import json
import re
import time
from collections import deque
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
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

from . import pricing
from .builder import expr
from .builder.catalog import WRITE_TOOLS
from .builder.compile import (
    DISALLOWED,
    CompiledGraph,
    compile_graph,
    decide_branch,
    gate_message,
    load_spec,
)
from .builder.graph import back_edges, forward_reach
from .builder.spec import LEGACY_INPUT
from .builder.types import check_value
from .claude_sessions import mark_ours
from .config import settings
from .connections import actions as conn_actions
from .connections.client import ConnectorError
from .connections.web import pick
from .hooks import DELEGATION_TOOLS, make_guard, make_hooks
from .tools import io as io_tool
from .trace import error_text, tool_detail

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
AFTER_PAUSE = ("The person paused you in the middle of your step and now lets you go on. "
               "Continue your step from where you stopped: do not redo work you already finished "
               "(files you wrote, outputs you saved). The run id is `{run_id}`; before you finish, "
               "save your outputs with `set_output` (agent `{agent}`).")

FOLLOW_UP = ("You finished without saving these required outputs: {names}. Save each one now "
             "with `set_output` (run_id `{run_id}`, agent `{agent}`), then finish.")


ASK = ("The person who ran this flow asks you, about your work in this run (run id `{run_id}`):"
       "\n\n{message}\n\nDo what they ask within your own step and its rules. If it changes your "
       "outputs, save them again with `set_output` (run_id `{run_id}`, agent `{agent}`); then say "
       "in a few lines what you changed.")


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
def usage_tokens(usage: Any) -> dict[str, int]:
    """Tokens of a session from the SDK's usage: in, out, cache read, cache write (non-zero)."""
    if not isinstance(usage, dict):
        return {}
    keys = {"in": "input_tokens", "out": "output_tokens", "cache_read": "cache_read_input_tokens",
            "cache_write": "cache_creation_input_tokens"}
    out = {}
    for k, src in keys.items():
        v = usage.get(src)
        if isinstance(v, (int, float)) and v > 0:
            out[k] = int(v)
    return out


# A temporary failure of the model API (busy, rate limit, 5xx, network), in the forms the SDK and
# the API give them: it usually goes away by itself, so a graph step that ends with one runs again
# (FlowSpec.retry). A bare number ("HTTP 500 from a connector", "line 504"), a connector's own
# connection error or a tool's timeout is not one. Same test as TRANSIENT in editor/src/runSteps.ts.
_TRANSIENT = re.compile(r"api error:?\s*\(?(429|5\d\d)\b|api error:?\s*(overloaded|connection)"
                        r"|overloaded_error|rate_limit_error|\boverloaded \(529\)",
                        re.IGNORECASE)


def is_transient(text: str) -> bool:
    return bool(_TRANSIENT.search(text or ""))


def _result_size(content: Any) -> int:
    """The size in characters of a tool result (text blocks), without keeping it."""
    if isinstance(content, str):
        return len(content)
    if isinstance(content, list):
        return sum(len(str(b.get("text") or "")) for b in content if isinstance(b, dict))
    return 0


def _short_tool(name: str) -> str:
    return name.split("__")[-1] if name.startswith("mcp__") else name


def _show(v: Any) -> tuple[str, bool]:
    text = v if isinstance(v, str) else json.dumps(v, ensure_ascii=False, default=str)
    if len(text) > MAX_VALUE_IN_PROMPT:
        return text[:MAX_VALUE_IN_PROMPT] + "…", True
    return text, False


@dataclass
class _SubHost:
    """The host of a nested run (subflow): its cost goes to the parent when it ends."""
    cost: float = 0.0
    session_id: str | None = None
    done_agents: set[str] = field(default_factory=set)


def _sub_run_id(parent: str, node: str) -> str:
    """runs/<parent>--<node>: the child run's own folder (run ids are at most 40 characters)."""
    return f"{parent[:24]}--{node[:14]}"


@dataclass
class _Result:
    ok: bool
    message: str = ""
    evals: list[tuple[int, bool, str, str | None]] | None = None  # (edge, fired, by, error)
    transient: bool = False  # a temporary API error: worth running again


def _audit_action(agent: str, connector: str, action: str, params: list[str]) -> None:
    """An action node in runs/audit.jsonl, like a tool call (names only, never the values)."""
    settings.runs_dir.mkdir(parents=True, exist_ok=True)
    with (settings.runs_dir / "audit.jsonl").open("a", encoding="utf-8") as f:
        f.write(json.dumps({"ts": time.time(), "tool": "action_node", "agent": agent,
                            "input": {"connection": connector, "action": action,
                                      "params": params}}, ensure_ascii=False) + "\n")


class GraphEngine:
    """State of one graph run. The runner keeps it on the `Run`, so `continue_` and `decide`
    resume where it stopped. `run()` returns when nothing more can start (end, gate, pause)."""

    def __init__(self, compiled: CompiledGraph, run_id: str, *, host: Host, emit: Emit,
                 query_fn: QueryFn = query, route_fn: RouteFn | None = None,
                 mode: EngineMode = "full", target: str | None = None, instruction: str = "",
                 nested: bool = False):
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
        self.unapplied: list[str] = []  # finished nodes whose links a stop left undecided
        self.failure: str | None = None
        self.sessions: dict[str, str] = {}
        self.summaries: dict[str, str] = {}
        self.reasons: dict[str, list[str]] = {}
        self.step_budget: int | None = None
        self.until_set = self._leads_to(target) if mode == "until" and target else set()
        self.started = False
        self.nested = nested  # a subflow: its flow is registered by run id, not made active
        self.retry_waits: dict[str, tuple[asyncio.Event, list[str]]] = {}  # node: (wake, choice)
        self.wrote: set[str] = set()  # agents whose current attempt ran a write tool (not denied)
        # pause: the session each running agent works in now; the nodes asked to pause; the paused
        # nodes with the session they go on in (None: it had no session yet, it starts again)
        self.live_sessions: dict[str, str] = {}
        self.pausing: set[str] = set()
        self.paused: dict[str, str | None] = {}
        self.resuming: dict[str, str | None] = {}  # paused nodes going on now

    # --- public
    def set_mode(self, mode: EngineMode) -> None:
        """continue_: "step" (one more node) or "full" (to the end)."""
        self.mode = mode

    async def run(self) -> Outcome:
        if not self.nested:
            self.compiled.activate()
        self.step_budget = 1 if self.mode == "step" else None
        if not self.started:
            self.started = True
            first = self.target if self.mode == "agent" else self.spec.start
            assert first is not None
            self._make_ready(first)
        for node in list(self.paused):  # go on after a pause: they resume their own session
            self.state[node] = "ready"
            if node not in self.ready:
                self.ready.insert(0, node)
        self.resuming, self.paused = dict(self.paused), {}
        try:
            unapplied, self.unapplied = self.unapplied, []
            for n in unapplied:  # see prepare_resume
                self.host.done_agents.add(n)
                self._apply(n, await self._evaluate(n))
            while True:
                self._launch()
                if not self.tasks:
                    break
                done, _ = await asyncio.wait(set(self.tasks),
                                             return_when=asyncio.FIRST_COMPLETED)
                for t in done:
                    node = self.tasks.pop(t)
                    if t.cancelled() and node in self.pausing:
                        self._pause_node(node)
                        continue
                    self._finish(node, t.result())
        except asyncio.CancelledError:
            await self.cancel()
            raise
        return self._outcome()

    # --- resume after an error or a stop
    def resume_plan(self, recorded: dict[str, str] | None = None,
                    reuse_changed: bool = False, start_at: str | None = None) -> dict[str, Any]:
        """What a resume would do: `reuse` (finished nodes whose work is kept), `redo` (the failed
        or interrupted nodes, the ones changed since they ran unless `reuse_changed`, and
        everything after them), `changed` (finished nodes whose definition or prompt is not the
        one they ran with: `recorded` = node → fingerprint saved when it finished) and `gates`
        (approvals that will be asked again because what they approved is redone).
        `start_at`: "run from here": that node and everything after it run again, the rest keeps
        its work (whatever the state of the run, even done)."""
        if start_at is not None:
            base, changed = {start_at}, []
        else:
            base = {n for n, st in self.state.items()
                    if n in self.spec.agents and st in ("failed", "running")}
            changed = sorted(n for n, fp in (recorded or {}).items()
                             if n in self.spec.agents and self.state.get(n) == "done"
                             and fp != self.compiled.fingerprint(n))
        redo = set(base) | (set() if reuse_changed else set(changed))
        reset: set[str] = set()
        for n in redo:
            reset |= forward_reach(self.spec, n, self.back)
        unreached = {n for n in self.spec.agents if self.state[n] in ("waiting", "ready")}
        reuse = sorted(n for n in self.spec.agents
                       if self.state[n] == "done" and n not in reset)
        return {"reuse": reuse,
                "redo": sorted((reset & set(self.spec.agents)) | (base & set(self.spec.agents))),
                "changed": changed, "unreached": sorted(unreached - reset),
                "gates": sorted(g for g in self.spec.gates
                                if g in reset and self.state[g] in ("done", "gate")
                                and self.spec.gates[g].kind == "human")}

    def prepare_resume(self, recorded: dict[str, str] | None = None,
                       reuse_changed: bool = False, start_at: str | None = None) -> dict[str, Any]:
        """Makes this (finished with an error, or stopped) run ready to go on: keeps the work of
        the finished nodes whose result still holds and sets the rest to run again; then `run()`
        starts from there. Returns the plan."""
        plan = self.resume_plan(recorded, reuse_changed, start_at)
        reset: set[str] = set()
        redo_roots = ([start_at] if start_at is not None else
                      [n for n in plan["redo"]
                       if self.state[n] in ("failed", "running") or n in plan["changed"]])
        for n in redo_roots:
            reset |= forward_reach(self.spec, n, self.back)
        for n in self.state:  # nodes that were ready but not started when it failed
            if self.state[n] == "ready":
                self.state[n] = "waiting"
        self.ready.clear()
        for n in sorted(reset):
            if self.state[n] in ("failed", "running") and n in self.runs:
                self.runs[n] = max(0, self.runs[n] - 1)  # the failed attempt does not count
            self.state[n] = "waiting"
            self.host.done_agents.discard(n)
            self.reasons.pop(n, None)
        self.pending_gates = [g for g in self.pending_gates if g["gate"] not in reset]
        for j, e in enumerate(self.spec.edges):
            if j not in self.back and e.from_ in reset:
                self.decided.pop(j, None)
        if start_at is not None:  # a gate it was waiting at is no longer waited for
            for g in self.pending_gates:
                if self.state.get(g["gate"]) == "gate":
                    self.state[g["gate"]] = "done"
            self.pending_gates = []
        self.failure, self.stop_gate = None, None
        self.mode = "full"
        self.started = True
        # a stop can come after an agent finished but before its links were decided: decide them
        # first when the run goes on, or what comes after it would wait for ever
        self.unapplied = [n for n in self.spec.agents
                          if self.state[n] == "done" and n not in reset
                          and any(self.decided.get(j) is None
                                  for j, e in enumerate(self.spec.edges)
                                  if e.from_ == n and j not in self.back)]
        if start_at is not None:  # whatever came before: the chosen node starts now
            self._make_ready(start_at)
        for n in [*self.spec.agents, *self.spec.gates]:  # start what has all its inputs decided
            if self.state[n] != "waiting":
                continue
            if n == self.spec.start and not self.fwd_in.get(n):
                self._make_ready(n)
            else:
                self._check(n)
        return plan

    async def resolve_gate(self, gate: str) -> None:
        """After a human decision (already in io `_gates.json`): follow the gate's edges."""
        self.pending_gates = [g for g in self.pending_gates if g["gate"] != gate]
        self.state[gate] = "done"
        evals = await self._evaluate(gate)
        decision = io_tool.scope(self.spec, self.run_id).get(gate, {}).get("decision")
        if decision == "answer" and evals and not any(fired for _, fired, _, _ in evals):
            # "answer and continue" matches no link out of the gate: asking again beats ending the
            # run with everything after the gate skipped (a reject without a link does end it)
            self._ask_again(gate)
            return
        self._apply(gate, evals)

    def _ask_again(self, gate: str) -> None:
        decision = "answer"
        tos = ", ".join(self.spec.edges[i].to + (f" [{self.spec.edges[i].if_}]"
                                                 if self.spec.edges[i].if_ else "")
                        for i, e in enumerate(self.spec.edges) if e.from_ == gate)
        g = self.spec.gates[gate]
        info = {"gate": gate, "kind": g.kind, "label": g.label,
                "reason": (f"The decision {decision!r} matches none of the links out of this gate "
                           f"({tos}). Approve or reject, or add a link for that decision.")[:500]}
        self.emit({"type": "gate", **info})
        self.state[gate] = "gate"
        self.pending_gates.append(info)

    def asked_agents(self) -> list[str]:
        """The agents of this run that can be asked something now (they ran in a session; an
        action node has none), the last one to finish first."""
        return list(reversed(list(self.sessions)))

    async def ask(self, name: str, message: str) -> bool:
        """After a node ran: the person asks its agent for more (improve or change its output).
        The same session is resumed, with the same tools, guard and policy. True when it ended
        without an error."""
        sid = self.sessions.get(name)
        if not sid:
            raise ValueError(f"agent {name!r} has no session in this run")
        self.compiled.activate()
        self.emit({"type": "delegate", "agent": name, "task": "follow-up"})
        prompt = ASK.format(run_id=self.run_id, agent=name, message=message)
        _, error = await self._session(name, prompt, self.compiled.agent_options(name, resume=sid))
        if error:
            self.emit({"type": "agent_done", "agent": name, "error": True,
                       "message": error[:500]})
            return False
        if self.spec.agents[name].outputs:
            self.emit({"type": "outputs", "agent": name,
                       "names": sorted(io_tool.outputs(self.run_id, name))})
        self.emit({"type": "agent_done", "agent": name, "error": False})
        return True

    def retry_control(self, node: str | None, action: Literal["now", "cancel"]) -> list[str]:
        """While steps wait to run again after a temporary error: run them now, or cancel the
        retry (the step fails with its error). `node` None = every waiting step."""
        hit = [n for n in self.retry_waits if node is None or n == node]
        for n in hit:
            wake, choice = self.retry_waits[n]
            choice[0] = action
            wake.set()
        return hit

    async def pause(self, nodes: list[str] | None = None) -> list[str]:
        """Pauses running agents (all, or `nodes`): their sessions stop where they are; the
        other branches go on, and the run stops `paused` when nothing more runs. Going on
        (`run()` again) resumes each paused agent in its own session."""
        hit = [node for t, node in self.tasks.items() if nodes is None or node in nodes]
        for t, node in list(self.tasks.items()):
            if node in hit and self.spec.agents.get(node) is not None \
                    and self.spec.agents[node].action is None and self.spec.agents[node].subflow is None:
                self.pausing.add(node)
                t.cancel()
        return [n for n in hit if n in self.pausing]

    def _pause_node(self, node: str) -> None:
        self.pausing.discard(node)
        self.state[node] = "paused"
        self.runs[node] = max(0, self.runs[node] - 1)  # the attempt is not over: not a run
        sid = self.live_sessions.get(node)
        self.paused[node] = sid
        self.emit({"type": "paused", "agent": node, **({"session": sid} if sid else {})})

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
        if node in self.spec.gates and self.spec.gates[node].kind == "decision":
            self._decide(node)
        elif node in self.spec.gates:
            self._reach_gate(node)
        elif self.state[node] == "waiting":
            self.state[node] = "ready"
            self.ready.append(node)

    def _decide(self, node: str) -> None:
        """A decision: no session; the first branch whose `if` holds (or "otherwise") fires."""
        self.state[node] = "done"
        chosen, errors = decide_branch(self.spec, node, io_tool.scope(self.spec, self.run_id))
        evals = [(i, i == chosen, "if" if e.if_ else "else", errors.get(i))
                 for i, e in enumerate(self.spec.edges) if e.from_ == node]
        self.emit({"type": "decided", "node": node,
                   "to": self.spec.edges[chosen].to if chosen is not None else None})
        self._apply(node, evals)

    def _reach_gate(self, gate: str) -> None:
        g = self.spec.gates[gate]
        info = {"gate": gate, "kind": g.kind, "label": g.label,
                "reason": "; ".join(self.reasons.get(gate, []))[:500]}
        message = gate_message(self.spec, gate, self.run_id)
        if message:
            info["message"] = message
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
        if self.paused:
            return Outcome("paused", message="paused by the person: " + ", ".join(sorted(self.paused)))
        if self.mode in ("step", "until") and self.ready:
            return Outcome("paused")
        return Outcome("done")

    # --- one agent
    async def _agent(self, name: str) -> _Result:
        a = self.spec.agents[name]
        after_pause = name in self.resuming
        self.emit({"type": "delegate", "agent": name, "task": a.description[:120],
                   **({"after_pause": True} if after_pause else {})})
        if a.action is not None:
            return await self._action_node(name)
        if a.subflow is not None:
            return await self._subflow_node(name)
        res = await self._agent_once(name)
        policy, tries = self.spec.retry, 0
        while not res.ok and res.transient and tries < policy.transient:
            if name in self.wrote:  # it may have written already: running it again is the person's call
                self.emit({"type": "retry_skipped", "agent": name, "why": "wrote"})
                break
            tries += 1
            wait = min(policy.wait * 2 ** (tries - 1), 300)
            if not await self._retry_wait(name, tries, policy.transient, wait, res.message):
                break
            self.emit({"type": "delegate", "agent": name, "task": a.description[:120],
                       "retry": tries})
            res = await self._agent_once(name)
        return res

    async def _retry_wait(self, name: str, attempt: int, most: int, wait: int,
                          reason: str) -> bool:
        """Waits before a step runs again; False when the person cancelled the retry."""
        wake, choice = asyncio.Event(), ["wait"]
        self.retry_waits[name] = (wake, choice)
        self.emit({"type": "retry", "agent": name, "attempt": attempt, "max": most,
                   "wait": wait, "at": time.time() + wait, "reason": reason[:300]})
        try:
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(wake.wait(), wait)
        finally:
            self.retry_waits.pop(name, None)
        if choice[0] == "cancel":
            self.emit({"type": "retry_cancelled", "agent": name})
            return False
        return True

    async def _agent_once(self, name: str) -> _Result:
        a = self.spec.agents[name]
        self.wrote.discard(name)
        resume_sid = self.resuming.pop(name, None)
        try:
            values, missing = io_tool.resolve_inputs(self.spec, self.run_id, name)
            if missing:
                return self._failed(name, "required inputs without a value: "
                                    + ", ".join(missing))
            if resume_sid:  # after a pause: the same session goes on; its outputs so far stay
                sid, error = await self._session(
                    name, AFTER_PAUSE.format(run_id=self.run_id, agent=name),
                    self.compiled.agent_options(name, resume=resume_sid))
            else:
                io_tool.clear_outputs(self.run_id, name)
                sid, error = await self._session(name, self._message(name, values),
                                                 self.compiled.agent_options(name))
            if error:
                return self._failed(name, error, transient=is_transient(error))
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
            self.emit({"type": "agent_done", "agent": name, "error": False,
                       "fp": self.compiled.fingerprint(name)})
            evals = [] if self.mode == "agent" else await self._evaluate(name)
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001 — the node fails, the run reports it
            return self._failed(name, f"{type(e).__name__}: {e}", transient=is_transient(str(e)))
        return _Result(True, evals=evals)

    async def _action_node(self, name: str) -> _Result:
        """An action node: the studio runs the connector action itself (no model, no session)
        and saves the values of its result as the node's typed outputs."""
        a = self.spec.agents[name]
        call = a.action
        assert call is not None
        try:
            scope = io_tool.scope(self.spec, self.run_id)
            params = {k: expr.render(v, scope) for k, v in call.params.items()}
            data = {"connection": call.connector, "action": call.name, "params": params}
            self.emit({"type": "tool", "agent": name, "tool": "run_action",
                       "detail": tool_detail("run_action", data)})
            _audit_action(name, call.connector, call.name, list(params))
            io_tool.clear_outputs(self.run_id, name)
            try:
                r = await conn_actions.run(call.connector, call.name, params)
            except (conn_actions.ActionError, ConnectorError) as e:
                self.emit({"type": "tool_error", "agent": name, "tool": "run_action",
                           "error": error_text(str(e))})
                return self._failed(name, str(e))
            result = r.get("values") if "values" in r else r.get("body")
            for out in a.outputs:
                m = call.outputs.get(out)
                raw = pick(result, (m.from_ if m and m.from_ else out)) \
                    if isinstance(result, dict | list) else None
                if m and m.match and raw is not None:
                    found = re.search(m.match, str(raw))
                    raw = (found.group(1) if found and found.groups() else
                           found.group(0) if found else None)
                if raw is None or raw == "":
                    continue
                try:
                    io_tool.store_output(self.spec, self.run_id, name, out, raw)
                except ValueError as e:
                    return self._failed(name, f"output {out}: {e} (value: {str(raw)[:80]!r})")
            lacking = self._missing_outputs(name)
            if lacking:
                return self._failed(name, "the action result has no value for: "
                                    + ", ".join(lacking))
            if a.outputs:
                self.emit({"type": "outputs", "agent": name,
                           "names": sorted(io_tool.outputs(self.run_id, name))})
            self.emit({"type": "agent_done", "agent": name, "error": False,
                       "fp": self.compiled.fingerprint(name)})
            evals = [] if self.mode == "agent" else await self._evaluate(name)
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001 — the node fails, the run reports it
            return self._failed(name, f"{type(e).__name__}: {e}")
        return _Result(True, evals=evals)

    async def _subflow_node(self, name: str) -> _Result:
        """A subflow node: the child flow runs as one step, in its own run folder
        (runs/<parent>--<node>), with the same approvals as this run; its events come tagged with
        `sub`, and the mapped outputs of the child become the node's outputs."""
        a = self.spec.agents[name]
        call = a.subflow
        assert call is not None
        rid = _sub_run_id(self.run_id, name)
        child: CompiledGraph | None = None
        try:
            spec, flow_dir = load_spec(call.flow, base=self.compiled.flow_dir.parent)
            child = compile_graph(spec, flow_dir, configure_artifacts=False)
            if child.workspace is not None and child.workspace != self.compiled.workspace:
                return self._failed(name, f"flow {call.flow} works in another folder "
                                    f"({child.workspace}); a subflow uses the parent's")
            child.policy.approver = self.compiled.policy.approver  # one person approves
            scope = io_tool.scope(self.spec, self.run_id)
            values: dict[str, Any] = {}
            for k, text in call.inputs.items():
                parts = expr.template_parts(text)
                whole = len(parts) == 1 and text.strip() == "{" + parts[0] + "}"
                values[k] = expr.value(parts[0], scope) if whole else expr.render(text, scope)
            clean: dict[str, Any] = {}
            for k, t in spec.inputs.items():
                v = values.get(k, t.default)
                if v is None or v == "":
                    continue
                try:
                    clean[k] = check_value(t, v, run_dir=io_tool.run_dir(rid))
                except ValueError as e:
                    return self._failed(name, f"subflow input {k}: {e}")
            if not spec.inputs:  # a legacy flow: its one input is the run id
                clean = {LEGACY_INPUT: rid}
            io_tool.clear_outputs(self.run_id, name)
            io_tool.save_flow_inputs(rid, clean)
            child.register(rid)
            self.emit({"type": "subflow", "agent": name, "flow": call.flow, "run": rid})
            host = _SubHost()
            sub = GraphEngine(child, rid, host=host, query_fn=self._query, route_fn=self._route,
                              emit=lambda ev: self.emit({**ev, "sub": (
                                  f"{name}/{ev['sub']}" if ev.get("sub") else name)}),
                              nested=True)
            try:
                outcome = await sub.run()
            finally:
                self.host.cost += host.cost
            if outcome.status == "error":
                return self._failed(name, f"subflow {call.flow}: {outcome.message}")
            if outcome.status != "done":
                return self._failed(name, f"subflow {call.flow} stopped at {outcome.status}")
            for out, m in call.outputs.items():
                if m.from_ == "message":
                    raw = (outcome.gate or {}).get("message")
                else:
                    ca, co = m.from_.split(".", 1)
                    raw = io_tool.outputs(rid, ca).get(co)
                if raw is None or raw == "" or raw == []:
                    continue
                try:
                    io_tool.store_output(self.spec, self.run_id, name, out, raw)
                except ValueError as e:
                    return self._failed(name, f"output {out}: {e}")
            lacking = self._missing_outputs(name)
            if lacking:
                return self._failed(name, f"subflow {call.flow} gave no value for: "
                                    + ", ".join(lacking))
            if a.outputs:
                self.emit({"type": "outputs", "agent": name,
                           "names": sorted(io_tool.outputs(self.run_id, name))})
            self.emit({"type": "agent_done", "agent": name, "error": False,
                       "fp": self.compiled.fingerprint(name)})
            evals = [] if self.mode == "agent" else await self._evaluate(name)
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001 — the node fails, the run reports it
            return self._failed(name, f"{type(e).__name__}: {e}")
        finally:
            if child is not None:
                child.release(rid)
        return _Result(True, evals=evals)

    def _failed(self, name: str, message: str, transient: bool = False) -> _Result:
        ev: dict[str, Any] = {"type": "agent_done", "agent": name, "error": True,
                              "message": message[:500]}
        if transient:
            ev["transient"] = True
        self.emit(ev)
        return _Result(False, message, transient=transient)

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
        tools: dict[str, str] = {}  # tool_use_id → tool (to name the one that failed)
        started: dict[str, float] = {}  # tool_use_id → when it was called
        model: str | None = None  # the model that answered (from its messages)
        seen: set[str] = set()  # API messages already counted
        writes: set[str] = set()  # ids of write tool calls (they count once they ran, not denied)
        async for msg in self._query(prompt=prompt, options=options):
            msg_sid = getattr(msg, "session_id", None)
            if isinstance(msg_sid, str) and msg_sid:
                self.live_sessions[name] = msg_sid  # a pause can go on in this session
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
                        detail = tool_detail(b.name, b.input)
                        if detail:
                            ev["detail"] = detail
                        ev["id"] = b.id
                        if getattr(msg, "session_id", None):  # to read the call later, on demand
                            ev["session"] = msg.session_id
                        tools[b.id] = _short_tool(b.name)
                        if b.name in WRITE_TOOLS:
                            writes.add(b.id)
                        started[b.id] = time.time()
                        self.emit(ev)
                if getattr(msg, "model", None):
                    model = msg.model
                live = usage_tokens(getattr(msg, "usage", None))
                mid = getattr(msg, "message_id", None)
                if live and msg.parent_tool_use_id is None and (mid is None or mid not in seen):
                    if mid:  # one API message can come as several (one per block)
                        seen.add(mid)
                    uev: dict[str, Any] = {"type": "usage", "agent": name, "tokens": live}
                    est = pricing.estimate(model, live)
                    if est is not None:  # a live estimate; the session's result has the cost
                        uev["est"] = est
                    self.emit(uev)
            elif isinstance(msg, UserMessage) and isinstance(msg.content, list):
                for b in msg.content:
                    if not isinstance(b, ToolResultBlock):
                        continue
                    took = time.time() - started.pop(b.tool_use_id, time.time())
                    if b.is_error:
                        self.emit({"type": "tool_error", "agent": name,
                                   "tool": tools.get(b.tool_use_id), "id": b.tool_use_id,
                                   "ms": round(took * 1000), "error": error_text(b.content)})
                    else:  # only its size and time: never the content (it can hold anything)
                        if b.tool_use_id in writes:  # a write that ran: no retry by itself
                            self.wrote.add(name)
                        self.emit({"type": "tool_result", "agent": name, "id": b.tool_use_id,
                                   "tool": tools.get(b.tool_use_id), "ms": round(took * 1000),
                                   "size": _result_size(b.content)})
            elif isinstance(msg, ResultMessage):
                sid = msg.session_id
                mark_ours(sid, getattr(options, "cwd", None))
                self.sessions[name] = sid
                self.host.session_id = sid
                # a gateway's models have no price the CLI knows: unknown, never a guess
                cost = None if name in self.compiled.unpriced else msg.total_cost_usd
                self.host.cost += cost or 0.0
                if msg.result:
                    self.summaries[name] = msg.result
                ev: dict[str, Any] = {
                    "type": "result", "agent": name, "session_id": sid,
                    "cost": cost, "turns": msg.num_turns, "is_error": msg.is_error,
                    "subtype": msg.subtype, "denials": len(msg.permission_denials or [])}
                if model:
                    ev["model"] = model
                tokens = usage_tokens(getattr(msg, "usage", None))
                if tokens:
                    ev["tokens"] = tokens
                if msg.is_error:
                    ev["message"] = ("; ".join(msg.errors or []) or msg.result or "")[:500]
                self.emit(ev)
                if msg.is_error:
                    detail = "; ".join(msg.errors or []) or (msg.result or "")[:300] or msg.subtype
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

"""Flow runs from the editor: trace as events and decisions at gates.

One run at a time (the `artifacts` module is configured by the active flow). Events are kept in
memory and in `runs/<input>/events.jsonl`; human decisions in `runs/<input>/decisions.jsonl`.
A decision **runs nothing**: it is recorded and, if the gate has outgoing edges, the run resumes
(same session) so that the orchestrator follows the graph. Drafts stay drafts.
Graph flows (`execution: graph`) run through `engine.GraphEngine` instead: the engine state lives
on the `Run`, and continue/decide resume it (the decision feeds the conditions of the gate's edges).
Events do not keep tool arguments (they can contain personal data), only the tool name.
"""
from __future__ import annotations

import asyncio
import base64
import binascii
import json
import re
import shutil
import time
import uuid
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Literal

from claude_agent_sdk import (
    AssistantMessage,
    ResultMessage,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
    query,
)

from ..builder import expr, load_flow
from ..builder.catalog import CONTRACTS
from ..builder.compile import GATE_TOOL, CompiledFlow, gate_message, load_graph
from ..builder.spec import LEGACY_INPUT, FlowSpec
from ..builder.types import IOType, check_value
from ..config import settings
from ..engine import GraphEngine, RouteFn
from ..hooks import DELEGATION_TOOLS
from ..messages import msg as m_
from ..messages import reason, to_json
from ..policy import ApprovalRequest
from ..tools import io as io_tool
from ..tools.guards import safe_artifact_path
from ..trace import error_text, tool_detail

Status = Literal["running", "gate", "paused", "done", "error", "stopped"]
Mode = Literal["full", "step", "until", "agent"]
STEP_HINT = ("\n\n(Step-by-step test mode: delegate to one agent at a time. After each agent, "
             "finish with a short summary and wait for the instruction to continue.)")
CONTINUE = ("Continue the flow from where you stopped: delegate to the next agent, following the "
            "graph and the artifacts already saved.")
AGENT_TEST = ("You are a TEST orchestrator. Delegate once to the subagent `{agent}` with the "
              "given task, pass it the run id `{input}` (to read and save artifacts) and finish "
              "with a short summary of the result. Do not delegate to any other agent and do not "
              "do the work yourself.")
# Text of the policy's step-by-step denials (PT before the translation, EN after it).
STEP_DENIAL = re.compile(r"\bmod[eo]\b", re.IGNORECASE)
Decision = Literal["approve", "reject", "answer"]
RUN_ID = re.compile(r"^[A-Za-z0-9_-]{1,40}$")
# The Claude Code CLI (used by the SDK) is not signed in, or its session expired: the editor shows
# the steps to sign in again instead of the raw error.
AUTH_ERROR = re.compile(
    r"failed to authenticate|oauth (session|token)|not logged in|please run /login"
    r"|invalid (x-)?api[ -]?key|authentication_error|could not be refreshed", re.IGNORECASE)


def error_hint(message: str) -> str | None:
    """'auth' when a run or agent error is a sign-in problem of the Claude Code CLI."""
    return "auth" if AUTH_ERROR.search(message or "") else None
MAX_UPLOAD = 20 * 1024 * 1024  # all files of one run, after base64 decoding
_FILE_NAME = re.compile(r"^[A-Za-z0-9 _.()-]{1,100}$")
QueryFn = Callable[..., AsyncIterator[Any]]


@dataclass
class Run:
    id: str
    flow: str
    input: str
    base: Path | None = None
    status: Status = "running"
    events: list[dict[str, Any]] = field(default_factory=list)
    session_id: str | None = None
    gate: dict[str, Any] | None = None
    cost: float = 0.0
    started: float = field(default_factory=time.time)
    task: asyncio.Task[None] | None = None
    mode: Mode = "full"
    target: str | None = None  # until/agent mode
    done_agents: set[str] = field(default_factory=set)
    tool_names: dict[str, str] = field(default_factory=dict)  # tool_use_id → tool (for errors)
    pending: dict[str, tuple[ApprovalRequest, asyncio.Future[tuple[bool, str]]]] = field(
        default_factory=dict)
    engine: GraphEngine | None = None  # graph mode: the engine state (continue/decide resume it)

    def summary(self) -> dict[str, Any]:
        out = {"id": self.id, "flow": self.flow, "input": self.input, "status": self.status,
               "session_id": self.session_id, "gate": self.gate, "cost": round(self.cost, 4),
               "started": self.started, "events": len(self.events),
               "pending_approvals": list(self.pending), "mode": self.mode,
               "target": self.target, "done_agents": sorted(self.done_agents)}
        if self.engine is not None:
            out["execution"] = "graph"
            out["nodes"] = dict(self.engine.state)
        return out


class RunError(Exception):
    def __init__(self, status: int, msg: str, detail: Any = None):
        super().__init__(msg)
        self.status = status
        self.detail = detail if detail is not None else msg


def new_run_id(flow: str) -> str:
    return f"{flow[:20]}-{time.strftime('%Y%m%d-%H%M%S')}"


def _write_upload(rd: Path, name: str, f: Any, total: list[int]) -> dict[str, Any]:
    """{"name", "content_b64"} → runs/<id>/inputs/<input>/<file>; returns the file reference."""
    if not isinstance(f, dict) or "content_b64" not in f:
        raise ValueError("a file is {name, content_b64}")
    fname = str(f.get("name") or "file").strip()
    if not _FILE_NAME.fullmatch(fname) or ".." in fname:
        raise ValueError(f"invalid file name {fname!r}")
    try:
        data = base64.b64decode(str(f["content_b64"]), validate=True)
    except (binascii.Error, ValueError):
        raise ValueError(f"{fname}: invalid base64") from None
    total[0] += len(data)
    if total[0] > MAX_UPLOAD:
        raise ValueError(f"files larger than {MAX_UPLOAD // (1024 * 1024)} MB in total")
    target = rd / "inputs" / name / fname
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(data)
    return {"name": fname, "path": f"inputs/{name}/{fname}"}


def input_visible(t: IOType, values: dict[str, Any]) -> bool:
    """show_if on the values of the inputs before it (an expression that fails: shown)."""
    if not t.show_if:
        return True
    try:
        return expr.evaluate(t.show_if, {"input": values})
    except expr.ExprError:
        return True


def _codes(errors: dict[str, str]) -> dict[str, Any]:
    """{field: {code, params}} for the messages that have a code (the editor translates them)."""
    return {k: j for k, v in errors.items() if (j := to_json(v)) is not None}


def prepare_inputs(spec: FlowSpec, run_id: str, raw: dict[str, Any], *,
                   partial: bool = False) -> dict[str, Any]:
    """Validates the typed flow inputs (writing uploaded files first). RunError 422 with
    {field: reason} when something is wrong. Nothing is kept when a value fails.
    partial (agent test): nothing is required, and the values already saved in the run stay
    unless a new value replaces them."""
    rd = io_tool.run_dir(run_id)
    fresh = not rd.exists()
    unknown = set(raw) - set(spec.flow_inputs())
    errors: dict[str, str] = {k: m_("run.not_flow_input", "not an input of this flow")
                              for k in unknown}
    values: dict[str, Any] = dict(io_tool.flow_inputs(run_id)) if partial and not fresh else {}
    total = [0]
    for name, t in spec.flow_inputs().items():
        v = raw.get(name)
        if not input_visible(t, values):  # hidden by show_if: no value, not required
            values.pop(name, None)
            continue
        if partial:
            if v in (None, "", []) and (name in values or t.default is None):
                continue  # keep the saved value, if there is one
            t = t.model_copy(update={"required": False})
        try:
            if t.type == "file" and v not in (None, "", []):
                items = v if isinstance(v, list) else [v]
                refs = [_write_upload(rd, name, f, total) for f in items]
                v = refs if t.multiple else refs[0]
            clean = check_value(t, v, run_dir=rd, contracts=CONTRACTS)
        except ValueError as e:
            errors[name] = reason(e)
            continue
        if clean is not None and clean != []:
            values[name] = clean
    if errors:
        if fresh:  # a new run folder: leave nothing behind
            shutil.rmtree(rd, ignore_errors=True)
        raise RunError(422, "invalid inputs: " + "; ".join(f"{k}: {v}" for k, v in errors.items()),
                       {"inputs": {k: str(v) for k, v in errors.items()}, "codes": _codes(errors)})
    return values


def check_given(spec: FlowSpec, run_id: str, agent: str, given: dict[str, Any]) -> dict[str, Any]:
    """Values given to one agent in an agent test: each must be one of its inputs and match the
    type of that input (the type of its source when it has one). RunError 422 {"given": …}."""
    a = spec.agents[agent]
    rd = io_tool.run_dir(run_id)
    errors: dict[str, str] = {}
    out: dict[str, Any] = {}  # (errors hold Msg: English text + code for the editor)
    for name, v in given.items():
        i = a.inputs.get(name)
        if i is None:
            errors[name] = m_("run.not_agent_input", "not an input of agent {agent}",
                               agent=repr(agent))
            continue
        src = spec.source_io(i.from_) if i.from_ else None
        t = src if src is not None and (not i.type or i.type == src.type) else None
        t = (t or IOType(type=i.type or "text")).model_copy(update={"required": False,
                                                                    "default": None})
        try:
            clean = check_value(t, v, run_dir=rd, contracts=CONTRACTS)
        except ValueError as e:
            errors[name] = reason(e)
            continue
        if clean is not None and clean != []:
            out[name] = clean
    if errors:
        raise RunError(422, "invalid agent inputs: "
                       + "; ".join(f"{k}: {v}" for k, v in errors.items()),
                       {"given": {k: str(v) for k, v in errors.items()}, "given_codes": _codes(errors)})
    return out


def _text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return " ".join(str(c.get("text", "")) if isinstance(c, dict) else str(c) for c in content)
    return str(content or "")


def _short(name: str) -> str:
    return name.split("__")[-1] if name.startswith("mcp__") else name


class Runner:
    def __init__(self, base: Path | None = None, query_fn: QueryFn = query,
                 route_fn: RouteFn | None = None):
        """query_fn: SDK sessions (the orchestrator, or each agent in graph mode); route_fn: the
        router of `when` conditions in graph mode (default: a small model call via query_fn)."""
        self.base = base
        self.runs: dict[str, Run] = {}
        self._query = query_fn
        self._route = route_fn

    # --- state
    def active(self) -> Run | None:
        return next((r for r in self.runs.values() if r.status == "running"), None)

    def get(self, run_id: str) -> Run:
        if run_id not in self.runs:
            raise RunError(404, "run does not exist")
        return self.runs[run_id]

    def _emit(self, run: Run, ev: dict[str, Any]) -> None:
        ev = {"i": len(run.events), "ts": time.time(), **ev}
        if ev.get("type") in ("end", "agent_done", "result") and isinstance(ev.get("message"), str):
            hint = error_hint(ev["message"])
            if hint:
                ev["hint"] = hint
        run.events.append(ev)
        if ev.get("type") == "end" and ev.get("status") == "done" and run.mode != "agent":
            try:  # its outputs become previous.<agent>.<output> for the next run of the flow
                io_tool.save_previous(run.flow, run.input)
            except (OSError, ValueError):
                pass
        try:
            path = safe_artifact_path(settings.runs_dir, run.input, "events.jsonl")
            path.parent.mkdir(parents=True, exist_ok=True)
            disk = {k: v for k, v in ev.items() if k != "detail"}  # contents only in memory
            with path.open("a", encoding="utf-8") as f:
                f.write(json.dumps({"run": run.id, **disk}, ensure_ascii=False) + "\n")
        except (OSError, ValueError):
            pass  # the in-memory trace goes on; the file is only history

    # --- start and resume
    def start(self, flow: str, run_input: str | None = None, mode: Mode = "full",
              target: str | None = None, instruction: str = "", *, run_id: str | None = None,
              inputs: dict[str, Any] | None = None, given: dict[str, Any] | None = None) -> Run:
        """full: the whole flow · step: one agent at a time · until: runs step by step until
        `target` finishes · agent: only the `target` agent, isolated (with the saved outputs).

        Legacy flows: `run_input` is the one input and also the run id. Typed flows: `inputs`
        (files as {name, content_b64}) and `run_id` (default: <flow>-<date>-<time>); `given`:
        free inputs of the target agent in agent mode."""
        if self.active():
            raise RunError(409, "a run is already in progress; wait or stop it")
        compiled = load_flow(flow, base=self.base)  # validates flow and input before starting
        spec = compiled.spec
        if mode in ("until", "agent") and target not in spec.agents:
            raise RunError(400, f"agent {target!r} does not exist in the flow")
        if spec.inputs:
            rid = (run_id or "").strip() or new_run_id(flow)
            if not RUN_ID.fullmatch(rid):
                raise RunError(422, "invalid run id: letters, digits, - or _ (up to 40)",
                               {"run_id": "letters, digits, - or _ (up to 40)"})
            values = prepare_inputs(spec, rid, inputs or {}, partial=mode == "agent")
        else:
            if run_input is None:
                run_input = str((inputs or {}).get(LEGACY_INPUT, ""))
            if not run_input.strip():  # a flow without inputs: the input is just the run id
                run_input = (run_id or "").strip() or new_run_id(flow)
            compiled.task(run_input)  # pattern check (FlowError)
            rid, values = run_input, {LEGACY_INPUT: run_input}
        if mode == "agent" and target:
            # the test values replace those of an earlier test of this agent (none = {})
            io_tool.save_given(rid, target, check_given(spec, rid, target, given or {}))
        else:
            io_tool.clear_given(rid, list(spec.agents))  # a full run uses the real outputs
        io_tool.save_flow_inputs(rid, values)
        run = Run(uuid.uuid4().hex[:12], flow, rid, self.base, mode=mode,
                  target=target if mode in ("until", "agent") else None)
        if spec.execution == "graph":
            graph = load_graph(flow, base=self.base)
            graph.policy.approver = self._approver(run)
            run.engine = GraphEngine(graph, rid, host=run, emit=lambda ev: self._emit(run, ev),
                                     query_fn=self._query, route_fn=self._route, mode=mode,
                                     target=run.target, instruction=instruction)
            self.runs[run.id] = run
            self._emit(run, {"type": "start", "flow": flow, "input": rid, "mode": mode,
                             "target": run.target, "mock": settings.mock,
                             "model": graph.model, "execution": spec.execution})
            self._spawn(run, self._drive_graph(run))
            return run
        prompt = self.first_prompt(compiled, rid, values, mode, instruction)
        self.runs[run.id] = run
        self._emit(run, {"type": "start", "flow": flow, "input": rid, "mode": mode,
                         "target": run.target, "mock": settings.mock,
                         "model": compiled.options.model, "execution": spec.execution})
        self._launch(run, prompt, None)
        return run

    @staticmethod
    def first_prompt(compiled: CompiledFlow, run_id: str, values: dict[str, Any], mode: Mode,
                     instruction: str) -> str:
        if mode == "agent":
            return instruction.strip() or f"Run your step of the flow for {run_id}."
        prompt = compiled.task_for(run_id, values)
        return prompt + STEP_HINT if mode in ("step", "until") else prompt

    def continue_(self, run_id: str, how: Literal["step", "full"]) -> Run:
        """Resumes a paused run: one more step, or up to the end."""
        run = self.get(run_id)
        if run.status != "paused":
            raise RunError(409, "the run is not paused")
        if self.active():
            raise RunError(409, "a run is already in progress")
        run.mode, run.status = how, "running"
        self._emit(run, {"type": "continue", "how": how})
        if run.engine is not None:
            run.engine.set_mode(how)
            self._spawn(run, self._drive_graph(run))
            return run
        self._launch(run, CONTINUE + (STEP_HINT if how == "step" else ""), run.session_id)
        return run

    def decide(self, run_id: str, decision: Decision, comment: str) -> Run:
        run = self.get(run_id)
        if run.status != "gate" or not run.gate:
            raise RunError(409, "the run is not stopped at a gate")
        if run.gate["kind"] != "human":
            raise RunError(409, "a stop gate cannot be approved; fix the cause and run again")
        if self.active():
            raise RunError(409, "a run is already in progress")
        gate = run.gate
        record = {"ts": time.time(), "run": run.id, "session_id": run.session_id,
                  "gate": gate["gate"], "decision": decision, "comment": comment}
        path = safe_artifact_path(settings.runs_dir, run.input, "decisions.jsonl")
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
        self._emit(run, {"type": "decision", "gate": gate["gate"], "decision": decision,
                         "comment": comment})
        io_tool.record_gate(run.input, gate["gate"], decision, comment)  # for `if` conditions
        if run.engine is not None:  # graph mode: the engine follows the gate's edges
            run.status, run.gate = "running", None
            self._spawn(run, self._drive_graph(run, gate=gate["gate"]))
            return run
        compiled = load_flow(run.flow, resume=run.session_id, base=run.base)
        if not compiled.spec.outgoing(gate["gate"]):
            run.status, run.gate = "done", None
            self._emit(run, {"type": "end", "status": "done",
                             "note": "decision recorded; the gate has no next steps"})
            return run
        word = {"approve": "APPROVED", "reject": "REJECTED", "answer": "ANSWERED"}[decision]
        prompt = (f"Human decision at gate `{gate['gate']}` ({gate['label']}): {word}."
                  + (f"\nAnalyst comment/answer:\n{comment}" if comment else "")
                  + "\nContinue the flow from this gate, following the graph. Do not apply "
                    "fixes: drafts stay drafts.")
        run.status, run.gate = "running", None
        self._launch(run, prompt, run.session_id)
        return run

    def _launch(self, run: Run, prompt: str, resume: str | None) -> None:
        self._spawn(run, self._drive(run, prompt, resume))

    def _spawn(self, run: Run, coro: Any) -> None:
        def finished(task: asyncio.Task[None]) -> None:
            # cancelled before it started: the driver never handled the CancelledError
            if task.cancelled() and run.status == "running":
                run.status = "stopped"
                self._emit(run, {"type": "end", "status": "stopped"})

        run.task = asyncio.create_task(coro)
        run.task.add_done_callback(finished)

    def _log_decision(self, run: Run, record: dict[str, Any]) -> None:
        path = safe_artifact_path(settings.runs_dir, run.input, "decisions.jsonl")
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps({"ts": time.time(), "run": run.id, **record},
                               ensure_ascii=False) + "\n")

    def _approver(self, run: Run):  # type: ignore[no-untyped-def]
        async def ask(req: ApprovalRequest) -> tuple[bool, str]:
            fut: asyncio.Future[tuple[bool, str]] = asyncio.get_running_loop().create_future()
            run.pending[req.id] = (req, fut)
            self._emit(run, {"type": "approval", "id": req.id, "agent": req.agent,
                             "tool": req.tool, "environment": req.environment,
                             "summary": req.summary, "detail": req.detail})
            try:
                return await fut
            finally:
                run.pending.pop(req.id, None)

        return ask

    def approve(self, run_id: str, approval_id: str, ok: bool, comment: str) -> Run:
        run = self.get(run_id)
        item = run.pending.get(approval_id)
        if item is None:
            raise RunError(404, "approval request does not exist or was already decided")
        req, fut = item
        self._log_decision(run, {"type": "write", "agent": req.agent, "tool": req.tool,
                                 "environment": req.environment, "summary": req.summary,
                                 "decision": "approve" if ok else "reject", "comment": comment})
        self._emit(run, {"type": "approval_result", "id": approval_id, "approved": ok,
                         "summary": req.summary, "comment": comment})
        if not fut.done():
            fut.set_result((ok, comment))
        return run

    def stop(self, run_id: str) -> Run:
        run = self.get(run_id)
        for _, fut in list(run.pending.values()):
            if not fut.done():
                fut.cancel()
        if run.task and not run.task.done():
            run.task.cancel()
        elif run.status in ("paused", "gate"):  # no task running: close the run
            run.status = "stopped"
            self._emit(run, {"type": "end", "status": "stopped"})
        return run

    # --- execution
    async def _segment(self, run: Run, prompt: str, resume: str | None) -> int:
        """One call to the SDK. Returns how many agents finished in this segment."""
        compiled = load_flow(run.flow, resume=resume, base=run.base)
        options = compiled.options
        policy = compiled.policy
        assert policy is not None
        policy.approver = self._approver(run)
        if run.mode == "agent":
            assert run.target is not None
            policy.limit_segment(1, frozenset({run.target}))
            options = replace(options, agents={run.target: options.agents[run.target]},
                              system_prompt=AGENT_TEST.format(agent=run.target, input=run.input))
        elif run.mode in ("step", "until"):
            policy.limit_segment(1)
        before = len(run.done_agents)
        delegations: dict[str, str] = {}  # tool_use_id → subagent
        async for msg in self._query(prompt=prompt, options=options):
            self._on_message(run, msg, delegations, compiled.spec)
        return len(run.done_agents) - before

    async def _drive(self, run: Run, prompt: str, resume: str | None) -> None:
        try:
            progressed = await self._segment(run, prompt, resume)
            # "until agent": go on one step at a time until the target finishes
            while (run.mode == "until" and run.status == "running" and not run.gate
                   and progressed and run.target not in run.done_agents and run.session_id):
                self._emit(run, {"type": "continue", "how": "auto"})
                progressed = await self._segment(run, CONTINUE + STEP_HINT, run.session_id)
            if run.status == "running":
                if run.gate:
                    run.status = "gate"
                elif run.mode in ("step", "until") and progressed:
                    run.status = "paused"  # there may be more steps: the human decides
                else:
                    run.status = "done"
            if run.gate and run.gate["kind"] == "stop":
                run.status = "done"
            self._emit(run, {"type": "end", "status": run.status, "gate": run.gate})
        except asyncio.CancelledError:
            run.status = "stopped"
            self._emit(run, {"type": "end", "status": "stopped"})
        except Exception as e:  # noqa: BLE001 — report in the trace instead of losing the run
            run.status = "error"
            self._emit(run, {"type": "end", "status": "error", "message": f"{type(e).__name__}: {e}"})

    async def _drive_graph(self, run: Run, gate: str | None = None) -> None:
        """Graph mode: runs the engine until it stops (end, gate, pause or error)."""
        engine = run.engine
        assert engine is not None
        try:
            if gate is not None:
                await engine.resolve_gate(gate)
            out = await engine.run()
            run.status, run.gate = out.status, out.gate
            ev: dict[str, Any] = {"type": "end", "status": out.status, "gate": out.gate}
            if out.message:
                ev["message"] = out.message
            self._emit(run, ev)
        except asyncio.CancelledError:
            run.status = "stopped"
            self._emit(run, {"type": "end", "status": "stopped"})
        except Exception as e:  # noqa: BLE001 — report in the trace instead of losing the run
            run.status = "error"
            self._emit(run, {"type": "end", "status": "error",
                             "message": f"{type(e).__name__}: {e}"})

    def _on_message(self, run: Run, msg: Any, delegations: dict[str, str], spec: FlowSpec) -> None:
        if isinstance(msg, AssistantMessage):
            agent = delegations.get(msg.parent_tool_use_id or "")
            for b in msg.content:
                if isinstance(b, TextBlock) and b.text.strip():
                    self._emit(run, {"type": "text", "agent": agent, "text": b.text})
                elif isinstance(b, ToolUseBlock):
                    if b.name in DELEGATION_TOOLS and agent is None:
                        sub = str(b.input.get("subagent_type", "?"))
                        delegations[b.id] = sub
                        self._emit(run, {"type": "delegate", "agent": sub,
                                         "task": str(b.input.get("description", ""))[:120]})
                    elif b.name == GATE_TOOL:
                        name = str(b.input.get("gate", ""))
                        g = spec.gates.get(name)
                        if g:
                            run.gate = {"gate": name, "kind": g.kind, "label": g.label,
                                        "reason": str(b.input.get("reason", ""))[:500]}
                            message = gate_message(spec, name, run.input)
                            if message:
                                run.gate["message"] = message
                            self._emit(run, {"type": "gate", **run.gate})
                    else:
                        ev: dict[str, Any] = {"type": "tool", "agent": agent, "tool": _short(b.name)}
                        if _short(b.name) in ("save_artifact", "read_artifact"):
                            ev["artifact"] = str(b.input.get("name", ""))[:80]
                        detail = tool_detail(b.name, b.input)
                        if detail:
                            ev["detail"] = detail
                        run.tool_names[b.id] = _short(b.name)
                        self._emit(run, ev)
        elif isinstance(msg, UserMessage) and isinstance(msg.content, list):
            for b in msg.content:
                if isinstance(b, ToolResultBlock) and b.tool_use_id in delegations:
                    sub = delegations[b.tool_use_id]
                    if b.is_error and STEP_DENIAL.search(_text(b.content)):
                        # denied by the step-by-step control: the agent did not run
                        self._emit(run, {"type": "agent_blocked", "agent": sub})
                        continue
                    if not b.is_error:
                        run.done_agents.add(sub)
                    self._emit(run, {"type": "agent_done", "agent": sub,
                                     "error": bool(b.is_error)})
                elif isinstance(b, ToolResultBlock) and b.is_error:
                    agent = delegations.get(msg.parent_tool_use_id or "")
                    self._emit(run, {"type": "tool_error", "agent": agent,
                                     "tool": run.tool_names.get(b.tool_use_id),
                                     "error": error_text(b.content)})
        elif isinstance(msg, ResultMessage):
            run.session_id = msg.session_id
            run.cost += msg.total_cost_usd or 0.0
            ev: dict[str, Any] = {"type": "result", "session_id": msg.session_id,
                                  "cost": msg.total_cost_usd, "turns": msg.num_turns,
                                  "is_error": msg.is_error, "subtype": msg.subtype,
                                  "denials": len(msg.permission_denials or [])}
            if msg.is_error:  # the reason, so that a sign-in problem can be explained
                ev["message"] = ("; ".join(msg.errors or []) or msg.result or "")[:500]
            self._emit(run, ev)
            if msg.is_error:
                run.status = "error"

    async def stream(self, run_id: str, after: int = 0) -> AsyncIterator[dict[str, Any]]:
        """Events from `after`, until the run leaves 'running' (gate, end or error)."""
        run = self.get(run_id)
        i = max(0, after)
        while True:
            while i < len(run.events):
                yield run.events[i]
                i += 1
            if run.status != "running":
                yield {"type": "state", **run.summary()}
                return
            await asyncio.sleep(0.25)


def list_artifacts(run_input: str) -> list[dict[str, Any]]:
    base = safe_artifact_path(settings.runs_dir, run_input, "x").parent
    if not base.is_dir():
        return []
    return [{"name": p.name, "size": p.stat().st_size} for p in sorted(base.iterdir())
            if p.is_file() and p.suffix in (".json", ".md", ".jsonl")]


def read_artifact_text(run_input: str, name: str, limit: int = 200_000) -> str:
    path = safe_artifact_path(settings.runs_dir, run_input, name)
    if not path.is_file() or path.suffix not in (".json", ".md", ".jsonl"):
        raise FileNotFoundError(name)
    return path.read_text(encoding="utf-8")[:limit]

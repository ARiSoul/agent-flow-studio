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

from .. import claude_sessions, context, workspaces
from ..builder import FlowError, expr, load_flow, load_spec
from ..builder.catalog import CONTRACTS
from ..builder.compile import GATE_TOOL, CompiledFlow, compile_graph, gate_message, load_graph
from ..builder.spec import LEGACY_INPUT, FlowSpec
from ..builder.types import IOType, check_value
from ..builder.types import compatible as types_compatible
from ..config import settings
from ..engine import GraphEngine, RouteFn
from ..hooks import DELEGATION_TOOLS
from ..messages import msg as m_
from ..messages import reason, to_json
from ..policy import ApprovalRequest
from ..tools import io as io_tool
from ..tools.guards import safe_artifact_path
from ..trace import error_text, tool_detail
from . import uploads

Status = Literal["running", "gate", "paused", "done", "error", "stopped"]
Mode = Literal["full", "step", "until", "agent", "from"]
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
MAX_UPLOAD = 20 * 1024 * 1024  # files sent inside the request (larger ones: uploads.py)
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
    workspace: str = "studio"  # the studio workspace and environment of the run (all its life)
    environment: str | None = None
    # the workspace context after a finished run: "suggest" | "always" | "never" (None: studio)
    context_update: str | None = None
    context_done: bool = False  # the "always" step ran (once per run)

    def summary(self) -> dict[str, Any]:
        out = {"id": self.id, "flow": self.flow, "input": self.input, "status": self.status,
               "workspace": self.workspace, "environment": self.environment,
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
    """{"name", "content_b64"} or {"name", "upload"} (a staged file, uploads.py) →
    runs/<id>/inputs/<input>/<file>; returns the file reference."""
    if not isinstance(f, dict) or ("content_b64" not in f and "upload" not in f):
        raise ValueError("a file is {name, content_b64} or {name, upload}")
    fname = str(f.get("name") or "file").strip()
    if not _FILE_NAME.fullmatch(fname) or ".." in fname:
        raise ValueError(f"invalid file name {fname!r}")
    if "upload" in f:
        uploads.take(f["upload"], rd / "inputs" / name / fname)
        return {"name": fname, "path": f"inputs/{name}/{fname}"}
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
              inputs: dict[str, Any] | None = None, given: dict[str, Any] | None = None,
              handoff: dict[str, dict[str, str]] | None = None) -> Run:
        """full: the whole flow · step: one agent at a time · until: runs step by step until
        `target` finishes · agent: only the `target` agent, isolated (with the saved outputs).

        Legacy flows: `run_input` is the one input and also the run id. Typed flows: `inputs`
        (files as {name, content_b64}) and `run_id` (default: <flow>-<date>-<time>); `given`:
        free inputs of the target agent in agent mode."""
        if self.active():
            raise RunError(409, "a run is already in progress; wait or stop it")
        if mode == "from":  # an earlier run of this id, from `target` on
            return self.run_from((run_id or run_input or "").strip(), target or "", given or {})
        ws, env = workspaces.current(), workspaces.current_env()
        base = self.flows_base()
        compiled = load_flow(flow, base=base)  # validates flow and input before starting
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
        handoff = {k: v for k, v in (handoff or {}).items() if k in values}
        io_tool.save_handoff(rid, handoff)  # provenance: input -> flow, run, agent, output
        run = Run(uuid.uuid4().hex[:12], flow, rid, base, mode=mode,
                  target=target if mode in ("until", "agent") else None,
                  workspace=ws.name, environment=env,
                  context_update=None if ws.studio else spec.context_update)
        if spec.execution == "graph":
            graph = load_graph(flow, base=base)
            graph.policy.approver = self._approver(run)
            run.engine = GraphEngine(graph, rid, host=run, emit=lambda ev: self._emit(run, ev),
                                     query_fn=self._query, route_fn=self._route, mode=mode,
                                     target=run.target, instruction=instruction)
            self.runs[run.id] = run
            self._emit(run, {"type": "start", "flow": flow, "input": rid, "mode": mode,
                             "target": run.target, "mock": settings.mock,
                             "model": graph.model, "execution": spec.execution,
                             "workspace": ws.name, "environment": env,
                             **({"handoff": handoff} if handoff else {})})
            self._spawn(run, self._drive_graph(run))
            return run
        prompt = self.first_prompt(compiled, rid, values, mode, instruction)
        self.runs[run.id] = run
        self._emit(run, {"type": "start", "flow": flow, "input": rid, "mode": mode,
                         "target": run.target, "mock": settings.mock,
                         "model": compiled.options.model, "execution": spec.execution,
                         "workspace": ws.name, "environment": env,
                         **({"handoff": handoff} if handoff else {})})
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

    def run_from(self, run_input: str, node: str, given: dict[str, Any] | None = None) -> Run:
        """"Run from here": goes on with a saved run (whatever state it ended in, even done) from
        `node`: that node and everything after it run again, the earlier steps keep their work.
        `given`: values for the node's inputs that replace what its sources saved (the rest keep
        coming from the saved outputs). Same run folder; the log gets a `resume` event."""
        if not RUN_ID.fullmatch(run_input or ""):
            raise RunError(400, "give the id of an earlier run of this flow to start from")
        try:
            run = self.open_past(run_input)
        except RunError as e:
            if e.status == 404:
                raise RunError(404, "there is no earlier run with this id to start from") from e
            raise
        if run.status == "running":
            raise RunError(409, "this run is still running")
        if self.active():
            raise RunError(409, "a run is already in progress")
        if run.engine is None:
            raise RunError(409, "only graph runs can start from a step")
        if node not in run.engine.state:
            raise RunError(400, f"{node!r} is not a step of the flow")
        spec = run.engine.spec
        if given and node in spec.agents:
            clean = check_given(spec, run.input, node, given)
        else:
            clean = {}
        engine = self._fresh_engine(run)
        io_tool.clear_given(run.input, list(spec.agents))  # the real outputs, but for `given`
        if clean:
            io_tool.save_given(run.input, node, clean)
        with self._in(run):
            plan = engine.prepare_resume(_recorded_fingerprints(run.events), True, start_at=node)
        run.engine, run.mode, run.status, run.gate = engine, "full", "running", None
        self._emit(run, {"type": "resume", "from": node, "reuse": plan["reuse"],
                         "redo": plan["redo"], "changed": [], "given": sorted(clean)})
        self._spawn(run, self._drive_graph(run))
        return run

    def rename(self, run_input: str, title: str) -> str:
        """A name for a saved run (the folder and id stay): shown in the list of past runs."""
        title = " ".join(title.split())[:80]
        path = safe_artifact_path(settings.runs_dir, run_input, "run_meta.json")
        if not _last_run_events(path.parent / "events.jsonl"):
            raise RunError(404, "no saved run in this folder")
        meta: dict[str, Any] = {}
        if path.is_file():
            try:
                meta = json.loads(path.read_text(encoding="utf-8"))
            except ValueError:
                meta = {}
        if title:
            meta["title"] = title
        else:
            meta.pop("title", None)
        path.write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")
        return title

    @staticmethod
    def _title_of(folder: Path) -> str:
        try:
            return str(json.loads((folder / "run_meta.json").read_text(encoding="utf-8"))
                       .get("title") or "")
        except (OSError, ValueError, AttributeError):
            return ""

    def _resumable(self, run_id: str) -> Run:
        run = self.get(run_id)
        if run.status not in ("error", "stopped"):
            raise RunError(409, "only a run that ended with an error or was stopped can be resumed")
        if run.engine is None:
            raise RunError(409, "only graph runs can be resumed")
        if run.mode in ("agent", "until"):
            raise RunError(409, "a single-agent or up-to-agent test cannot be resumed")
        if self.active():
            raise RunError(409, "a run is already in progress")
        return run

    def _fresh_engine(self, run: Run) -> GraphEngine:
        """The engine of this run again, with the flow as it is now (what was edited since the
        run stopped applies) and the state rebuilt from the saved events."""
        with self._in(run):
            graph = load_graph(run.flow, base=run.base)
        graph.policy.approver = self._approver(run)
        engine = GraphEngine(graph, run.input, host=run, emit=lambda ev: self._emit(run, ev),
                             query_fn=self._query, route_fn=self._route, mode="full")
        _restore_engine(engine, run.events, run)
        return engine

    def resume_plan(self, run_id: str, reuse_changed: bool = False) -> dict[str, Any]:
        """What resuming would reuse and repeat (nothing runs, nothing changes)."""
        run = self._resumable(run_id)
        engine = self._fresh_engine(run)
        return engine.resume_plan(_recorded_fingerprints(run.events), reuse_changed)

    def resume(self, run_id: str, reuse_changed: bool = False) -> Run:
        """Goes on with a run that ended with an error or was stopped, in the same run folder:
        the finished nodes whose result still holds keep their work, the failed or interrupted
        ones (and what depends on them, and the ones edited since unless `reuse_changed`) run
        again."""
        run = self._resumable(run_id)
        engine = self._fresh_engine(run)
        with self._in(run):
            plan = engine.prepare_resume(_recorded_fingerprints(run.events), reuse_changed)
        run.engine, run.mode, run.status, run.gate = engine, "full", "running", None
        self._emit(run, {"type": "resume", "reuse": plan["reuse"], "redo": plan["redo"],
                         "changed": plan["changed"], "reuse_changed": reuse_changed})
        self._spawn(run, self._drive_graph(run))
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
        with self._in(run):
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

    # --- past runs: the log in runs/<id>/events.jsonl is enough to open one again ---------------
    def flow_outputs(self, flow: str, kind: str | None = None,
                     limit: int = 20) -> list[dict[str, Any]]:
        """Finished runs of a flow (this workspace) with their outputs, newest first, for "Fill
        from a run": only outputs whose type can feed an input of type `kind` (files stay in
        their run). Each output has a short preview; `output_value` gives the whole value."""
        try:
            spec, _ = load_spec(flow, base=self.flows_base())
        except FlowError:
            return []
        out = []
        for r in self.past_runs(flow, limit=100):
            if r["status"] not in ("done", "stopped") or any(
                    x.input == r["input"] and x.status == "running" for x in self.runs.values()):
                continue  # a failed run, or one still going
            items = []
            for agent, a in spec.agents.items():
                values = io_tool.outputs(r["input"], agent)
                for name, t in a.outputs.items():
                    if name not in values or t.type == "file":
                        continue
                    if kind and not types_compatible(t.type, kind):
                        continue
                    text = values[name] if isinstance(values[name], str) else json.dumps(
                        values[name], ensure_ascii=False)
                    items.append({"agent": agent, "name": name, "type": t.type,
                                  "label": t.label or name, "preview": text[:200]})
            if items:
                out.append({**{k: r[k] for k in ("input", "started", "status")},
                            "outputs": items})
            if len(out) >= limit:
                break
        return out

    def output_value(self, flow: str, run_input: str, agent: str, name: str) -> Any:
        """One output of a finished run of a flow of this workspace (for a hand-off)."""
        events = _last_run_events(safe_artifact_path(settings.runs_dir, run_input,
                                                     "events.jsonl"))
        if not events or events[0].get("flow") != flow:
            raise RunError(404, f"no run {run_input!r} of the flow {flow!r} here")
        values = io_tool.outputs(run_input, agent)
        if name not in values:
            raise RunError(404, f"the run {run_input!r} has no output {agent}.{name}")
        return values[name]

    def past_runs(self, flow: str, limit: int = 30) -> list[dict[str, Any]]:
        """The runs of a flow saved in runs/ (the last run in each folder), newest first."""
        out = []
        root = workspaces.scoped(settings.runs_dir)  # the runs of this workspace
        if not root.is_dir():
            return []
        for d in root.iterdir():
            if not d.is_dir() or d.name.startswith((".", "@")) or d.name == "chats":
                continue
            events = _last_run_events(d / "events.jsonl")
            if not events or events[0].get("flow") != flow:
                continue
            if (events[0].get("workspace") or workspaces.STUDIO) != workspaces.current().name:
                continue
            start = events[0]
            end = next((e for e in reversed(events) if e.get("type") == "end"), None)
            gate = (end or {}).get("gate") or {}
            out.append({
                "input": d.name, "run": start.get("run"), "started": start.get("ts"),
                "mode": start.get("mode"), "execution": start.get("execution"),
                "status": (end or {}).get("status", "stopped"), "gate": gate.get("label"),
                "cost": round(sum(float(e.get("cost") or 0) for e in events
                                  if e.get("type") == "result"), 4),
                "open": any(r.id == start.get("run") for r in self.runs.values()),
                "title": self._title_of(d),
            })
        out.sort(key=lambda x: -(x["started"] or 0))
        return out[:limit]

    def open_past(self, run_input: str) -> Run:
        """A saved run again, as it ended (done, at a gate, stopped…): its log, its outputs and the
        session of each agent, so the person can look at it, ask for more or answer its gate. The
        agents use the flow as it is now."""
        path = safe_artifact_path(settings.runs_dir, run_input, "events.jsonl")
        events = _last_run_events(path)
        if not events:
            raise RunError(404, "no saved run in this folder")
        start = events[0]
        rid = str(start.get("run"))
        if rid in self.runs:  # still in memory: that one
            return self.runs[rid]
        flow = str(start.get("flow"))
        ws = workspaces.get_or_none(start.get("workspace") or workspaces.STUDIO) \
            or workspaces.STUDIO_WS
        env = start.get("environment")
        base = self.base if ws.studio else ws.flows_dir
        with workspaces.using(ws, env):
            compiled = load_flow(flow, base=base)  # the flow must still exist and be valid
        run = Run(rid, flow, run_input, base, mode=start.get("mode") or "full",
                  target=start.get("target"), started=float(start.get("ts") or time.time()),
                  workspace=ws.name, environment=env)
        run.events = [{k: v for k, v in e.items() if k != "run"} | {"i": i}
                      for i, e in enumerate(events)]
        end = next((e for e in reversed(events) if e.get("type") == "end"), None)
        run.status = (end or {}).get("status") or "stopped"
        if run.status == "running":
            run.status = "stopped"  # the editor stopped while it ran
        run.gate = (end or {}).get("gate") if run.status == "gate" else None
        if run.status == "done" and (end or {}).get("gate"):
            run.gate = end["gate"]  # a stop with its message
        run.cost = sum(float(e.get("cost") or 0) for e in events if e.get("type") == "result")
        results = [e for e in events if e.get("type") == "result" and e.get("session_id")]
        run.done_agents = {str(e["agent"]) for e in events
                           if e.get("type") == "agent_done" and not e.get("error") and e.get("agent")}
        if start.get("execution") == "graph" and compiled.spec.execution == "graph":
            with workspaces.using(ws, env):
                graph = load_graph(flow, base=base)
            graph.policy.approver = self._approver(run)
            run.engine = GraphEngine(graph, run_input, host=run,
                                     emit=lambda ev: self._emit(run, ev), query_fn=self._query,
                                     route_fn=self._route, mode=run.mode, target=run.target)
            _restore_engine(run.engine, events, run)
        else:
            orch = [e for e in results if not e.get("agent")]
            run.session_id = orch[-1]["session_id"] if orch else None
        self.runs[run.id] = run
        return run

    def askable(self, run_id: str) -> list[str]:
        """Who can be asked for more after (or between) the steps of a run: in graph mode the
        agents that ran in a session (the last one first); in orchestrator mode the orchestrator
        ("" = the whole flow)."""
        run = self.get(run_id)
        if run.status == "running":
            return []
        if run.engine is not None:
            return run.engine.asked_agents()
        return [""] if run.session_id and run.status in ("done", "stopped", "error") else []

    def ask(self, run_id: str, message: str, agent: str | None = None) -> Run:
        """The person asks for more after a run (improve an output, change something): the
        session of the agent that did it is resumed (graph mode), or the orchestrator's. The run
        keeps its state (done, at a gate…) when the answer ends."""
        run = self.get(run_id)
        if self.active():
            raise RunError(409, "a run is already in progress")
        who = self.askable(run_id)
        if not who:
            raise RunError(409, "nothing to ask in this run (it has no finished session)")
        message = message.strip()
        if not message:
            raise RunError(422, "write what you want")
        target = agent if agent is not None else who[0]
        if target not in who:
            raise RunError(422, f"{target!r} cannot be asked in this run; choose one of: "
                           + ", ".join(w or "the flow" for w in who))
        self._emit(run, {"type": "ask", "agent": target or None, "text": message[:4000]})
        before = (run.status, run.gate)
        run.status = "running"
        if run.engine is not None:
            self._spawn(run, self._drive_ask(run, target, message, before))
            return run
        prompt = ("The person asks, after the run:\n\n" + message + "\n\nAnswer it. You may "
                  "delegate to the agents again to change their outputs, with the same rules; do "
                  "not start the flow over. Drafts stay drafts.")
        run.gate = None
        self._launch(run, prompt, run.session_id)
        return run

    async def _drive_ask(self, run: Run, agent: str, message: str,
                         before: tuple[Status, dict[str, Any] | None]) -> None:
        engine = run.engine
        assert engine is not None
        try:
            await engine.ask(agent, message)
            run.status, run.gate = before  # back where it was (done, at its gate…)
            ev: dict[str, Any] = {"type": "end", "status": run.status, "gate": run.gate}
            self._emit(run, ev)
        except asyncio.CancelledError:
            run.status = "stopped"
            self._emit(run, {"type": "end", "status": "stopped"})
        except Exception as e:  # noqa: BLE001 — report in the trace instead of losing the run
            run.status = before[0] if before[0] != "running" else "error"
            run.gate = before[1]
            self._emit(run, {"type": "end", "status": run.status, "gate": run.gate,
                             "message": f"{type(e).__name__}: {e}"})

    def _launch(self, run: Run, prompt: str, resume: str | None) -> None:
        self._spawn(run, self._drive(run, prompt, resume))

    def flows_base(self) -> Path:
        """The flows folder of the open workspace (the studio's: the runner's base)."""
        ws = workspaces.current()
        return self.base if ws.studio else ws.flows_dir

    @staticmethod
    def _in(run: Run) -> Any:
        """The run's workspace and environment (a run keeps them for all its life)."""
        ws = workspaces.get_or_none(run.workspace) or workspaces.STUDIO_WS
        return workspaces.using(ws, run.environment)

    # --- the workspace context ----------------------------------------------------------------
    async def _after_run(self, run: Run, ev: dict[str, Any]) -> None:
        """A full run of a workspace flow that ended done: "always" updates the workspace
        context as a last system step of the run; "suggest" marks the end (a button)."""
        how = run.context_update
        if ev.get("status") != "done" or run.mode != "full" or how in (None, "never"):
            return
        if how == "suggest" or run.context_done:
            ev["context"] = "suggest" if how == "suggest" else "updated"
            return
        run.context_done = True
        ok = await self._context_steps(run, "update", None, self._changes(run))
        ev["context"] = "updated" if ok else "failed"

    def _changes(self, run: Run) -> str:
        """What a run did (its end, stop message and outputs), for the context update."""
        outs: dict[str, dict[str, Any]] = {}
        names = {e.get("agent") for e in run.events if e.get("type") == "outputs"}
        for a in sorted(n for n in names if isinstance(n, str)):
            try:
                outs[a] = io_tool.outputs(run.input, a)
            except ValueError:
                continue
        events = [*run.events, {"type": "end", "status": "done", "gate": run.gate}]
        return context.run_changes(events, outs)

    def update_context(self, mode: Literal["explore", "update"], roots: list[str] | None = None,
                       after: str | None = None) -> Run:
        """A run of the context keeper on roots of the open workspace (all by default): explore
        writes their sections from scratch, update brings them up to date (`after`: the run
        whose changes it should know about)."""
        if self.active():
            raise RunError(409, "a run is already in progress; wait or stop it")
        ws, env = workspaces.current(), workspaces.current_env()
        if ws.studio or ws.spec is None:
            raise RunError(400, "the studio's own workspace has no context: open a workspace")
        names = roots or list(ws.spec.roots)
        unknown = [r for r in names if r not in ws.spec.roots]
        if unknown or not names:
            raise RunError(400, f"unknown roots: {', '.join(unknown)}" if unknown else
                           "this workspace has no roots yet: add a folder first")
        changes = ""
        if after:
            prev = next((r for r in self.runs.values() if r.input == after), None)
            if prev is not None:
                changes = self._changes(prev)
        rid = new_run_id("context")
        run = Run(uuid.uuid4().hex[:12], context.KEEPER_FLOW, rid, self.flows_base(),
                  workspace=ws.name, environment=env)
        self._emit(run, {"type": "start", "flow": context.KEEPER_FLOW, "input": rid,
                         "mode": "full", "target": None, "mock": settings.mock,
                         "model": settings.model, "execution": "graph", "context": mode,
                         "roots": names, "workspace": ws.name, "environment": env})
        self._spawn(run, self._drive_context(run, mode, names, changes))
        self.runs[run.id] = run  # only a run that started counts as active
        return run

    async def _drive_context(self, run: Run, mode: str, roots: list[str], changes: str) -> None:
        try:
            ok = await self._context_steps(run, mode, roots, changes)
            run.status = "done" if ok else "error"
            self._emit(run, {"type": "end", "status": run.status, "gate": None})
        except asyncio.CancelledError:
            run.status = "stopped"
            self._emit(run, {"type": "end", "status": "stopped"})
        except Exception as e:  # noqa: BLE001 — report in the trace instead of losing the run
            run.status = "error"
            self._emit(run, {"type": "end", "status": "error",
                             "message": f"{type(e).__name__}: {e}"})

    async def _context_steps(self, run: Run, mode: str, roots: list[str] | None,
                             changes: str) -> bool:
        """One context keeper session per root (in sequence; read-only tools on that root); the
        studio stores each section. Events are tagged `sub: "context:<root>"`. True when all
        roots were updated."""
        ws = workspaces.current()
        assert ws.spec is not None
        names = roots or list(ws.spec.roots)
        folders = ws.resolve(workspaces.current_env())[1]
        secs, st = context.sections(ws), context.state(ws)
        all_ok = True
        for root in names:
            self._emit(run, {"type": "context", "root": root, "status": "running", "mode": mode})
            try:
                values = {"mode": mode, "root": root, "current": secs.get(root, "")
                          if mode == "update" else "",
                          "since": (st.get(root) or {}).get("commit") or "", "changes": changes}
                result = await self._child(run, f"{run.input[:24]}--{root[:14]}",
                                           context.keeper_flow(root), context.KEEPER_AGENT,
                                           values, f"context:{root}")
                section = str(result.get("section") or "")
                if not section.strip():
                    raise RuntimeError("the context keeper returned no section")
                context.set_section(ws, root, section, commit=context.head(Path(folders[root])),
                                    by=run.input)
                self._audit_context(ws.name, root, mode, run.input)
                self._emit(run, {"type": "context", "root": root, "status": "done", "mode": mode,
                                 "summary": str(result.get("summary") or "")[:500]})
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001 — one root fails, the others go on
                all_ok = False
                self._emit(run, {"type": "context", "root": root, "status": "error",
                                 "mode": mode, "message": f"{type(e).__name__}: {e}"[:500]})
        return all_ok

    # --- learning from past Claude Code sessions ----------------------------------------------
    @staticmethod
    def _learn_dir() -> Path:
        """Machine-local state of the learning (drafts, what was learned) of this workspace."""
        return workspaces.scoped(settings.runs_dir) / ".context"

    def learned(self, root: str) -> dict[str, dict[str, float]]:
        """{"sessions": {id: last_modified}, "memory": {file: mtime}} already learned for a root."""
        try:
            data = json.loads((self._learn_dir() / "learned.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {"sessions": {}, "memory": {}}
        got = data.get(root) if isinstance(data, dict) else None
        got = got if isinstance(got, dict) else {}
        return {"sessions": dict(got.get("sessions") or {}), "memory": dict(got.get("memory") or {})}

    def drafts(self) -> dict[str, dict[str, Any]]:
        out = {}
        for f in sorted(self._learn_dir().glob("draft-*.json")):
            try:
                d = json.loads(f.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if isinstance(d, dict) and isinstance(d.get("root"), str):
                out[d["root"]] = d
        return out

    def discard_draft(self, root: str) -> None:
        if RUN_ID.fullmatch(root):
            (self._learn_dir() / f"draft-{root}.json").unlink(missing_ok=True)

    def apply_draft(self, root: str, section: str) -> None:
        """Saves the reviewed history of a root and records its sources as learned."""
        ws = workspaces.current()
        draft = self.drafts().get(root)
        if draft is None:
            raise RunError(404, "there is no draft history for this root")
        context.set_section(ws, context.history_of(root), section, by=draft.get("run"))
        d = self._learn_dir()
        try:
            data = json.loads((d / "learned.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = {}
        data = data if isinstance(data, dict) else {}
        mine = self.learned(root)
        mine["sessions"].update(draft.get("sessions") or {})
        mine["memory"].update(draft.get("memory") or {})
        data[root] = mine
        d.mkdir(parents=True, exist_ok=True)
        (d / "learned.json").write_text(json.dumps(data, indent=1), encoding="utf-8")
        self.discard_draft(root)
        settings.runs_dir.mkdir(parents=True, exist_ok=True)
        with (settings.runs_dir / "audit.jsonl").open("a", encoding="utf-8") as f:
            f.write(json.dumps({"ts": time.time(), "event": "workspace_context_learn",
                                "workspace": ws.name, "root": root, "run": draft.get("run"),
                                "sessions": len(draft.get("sessions") or {}),
                                "memory": len(draft.get("memory") or {})}) + "\n")

    def sources(self, root: str) -> dict[str, Any]:
        """The Claude Code sessions and memory notes about a root's folder."""
        folder = self._root_folder(root)
        seen = self.learned(root)
        return {"root": root, "folder": folder,
                "sessions": claude_sessions.list_sessions(folder, seen["sessions"]),
                "memory": claude_sessions.list_memory(folder, seen["memory"])}

    def _root_folder(self, root: str) -> str:
        ws = workspaces.current()
        if ws.studio or ws.spec is None:
            raise RunError(400, "the studio's own workspace has no context: open a workspace")
        if root not in ws.spec.roots:
            raise RunError(400, f"unknown root: {root}")
        return ws.resolve(workspaces.current_env())[1][root]

    def estimate(self, root: str, sessions: list[str], memory: list[str]) -> dict[str, int]:
        folder = self._root_folder(root)
        known = {s["id"]: s for s in claude_sessions.list_sessions(folder)}
        return claude_sessions.estimate(folder, [known[s] for s in sessions if s in known],
                                        memory)

    def learn_context(self, root: str, sessions: list[str], memory: list[str]) -> Run:
        """Reads the chosen past sessions and memory notes about a root (one reader each), then
        the history keeper writes the root's history, checked against the code. The result is a
        draft the person reviews (`apply_draft`); nothing is saved by the run."""
        if self.active():
            raise RunError(409, "a run is already in progress; wait or stop it")
        folder = self._root_folder(root)
        if not sessions and not memory:
            raise RunError(400, "choose at least one session or memory note")
        known = {s["id"]: s for s in claude_sessions.list_sessions(folder)}
        unknown = [s for s in sessions if s not in known]
        if unknown:
            raise RunError(400, f"unknown sessions: {', '.join(unknown[:3])}")
        notes_known = {m["file"]: m for m in claude_sessions.list_memory(folder)}
        memory = [m for m in memory if m in notes_known]
        ws, env = workspaces.current(), workspaces.current_env()
        rid = new_run_id("context")
        run = Run(uuid.uuid4().hex[:12], context.KEEPER_FLOW, rid, self.flows_base(),
                  workspace=ws.name, environment=env)
        picked = [known[s] for s in sessions]
        self._emit(run, {"type": "start", "flow": context.KEEPER_FLOW, "input": rid,
                         "mode": "full", "target": None, "mock": settings.mock,
                         "model": settings.model, "execution": "graph", "context": "learn",
                         "roots": [root], "workspace": ws.name, "environment": env})
        self._spawn(run, self._drive_learn(run, root, folder, picked,
                                           [notes_known[m] for m in memory]))
        self.runs[run.id] = run
        return run

    async def _child(self, run: Run, child: str, spec_data: dict[str, Any], agent: str,
                     values: dict[str, Any], tag: str) -> dict[str, Any]:
        """One system agent as a child engine of `run`; returns its outputs."""
        spec = FlowSpec.model_validate(spec_data)
        compiled = compile_graph(spec, Path(context.__file__).parent)
        compiled.policy.approver = self._approver(run)
        io_tool.save_flow_inputs(child, {k: v for k, v in values.items() if v})
        engine = GraphEngine(compiled, child, host=run, query_fn=self._query, route_fn=self._route,
                             emit=lambda e: self._emit(run, {**e, "sub": tag}))
        out = await engine.run()
        if out.status != "done":
            raise RuntimeError(out.message or f"the agent stopped ({out.status})")
        return io_tool.outputs(child, agent)

    async def _drive_learn(self, run: Run, root: str, folder: str,
                           sessions: list[dict[str, Any]], memory: list[dict[str, Any]]) -> None:
        try:
            ok = await self._learn_steps(run, root, folder, sessions, memory)
            run.status = "done" if ok else "error"
            self._emit(run, {"type": "end", "status": run.status, "gate": None})
        except asyncio.CancelledError:
            run.status = "stopped"
            self._emit(run, {"type": "end", "status": "stopped"})
        except Exception as e:  # noqa: BLE001 — report in the trace instead of losing the run
            run.status = "error"
            self._emit(run, {"type": "end", "status": "error",
                             "message": f"{type(e).__name__}: {e}"})

    async def _learn_steps(self, run: Run, root: str, folder: str,
                           sessions: list[dict[str, Any]], memory: list[dict[str, Any]]) -> bool:
        ws = workspaces.current()
        sources: list[tuple[str, str, Any]] = [("memory", "Memory notes", None)] if memory else []
        sources += [("session", s["title"] or s["first_prompt"][:60] or s["id"], s)
                    for s in sessions]
        notes, used_sessions, used_memory = [], {}, {}
        for n, (kind, title, s) in enumerate(sources, 1):
            self._emit(run, {"type": "context", "root": root, "status": "running",
                             "mode": "learn", "source": title, "n": n, "of": len(sources)})
            try:
                text = (await asyncio.to_thread(claude_sessions.memory_digest, folder,
                                                [m["file"] for m in memory])
                        if kind == "memory" else
                        await asyncio.to_thread(claude_sessions.digest, s["id"], folder, s))
                if not text.strip():
                    raise RuntimeError("the source has no conversation text")
                out = await self._child(run, f"{run.input[:24]}--r{n}", context.reader_flow(),
                                        context.READER_AGENT,
                                        {"root": root, "kind": kind, "source": text},
                                        f"learn:{n}")
                got = str(out.get("notes") or "").strip()
                if got and got != "Nothing to keep.":
                    notes.append(f"#### From {kind} `{title}`\n{got}")
                if kind == "memory":
                    used_memory = {m["file"]: m["modified"] for m in memory}
                else:
                    used_sessions[s["id"]] = s["modified"] or 0
                self._emit(run, {"type": "context", "root": root, "status": "done",
                                 "mode": "learn", "source": title, "n": n, "of": len(sources)})
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001 — one source fails, the others go on
                self._emit(run, {"type": "context", "root": root, "status": "error",
                                 "mode": "learn", "source": title, "n": n, "of": len(sources),
                                 "message": f"{type(e).__name__}: {e}"[:500]})
        if not used_sessions and not used_memory:
            return False
        secs = context.sections(ws)
        self._emit(run, {"type": "context", "root": root, "status": "running", "mode": "history"})
        try:
            out = await self._child(
                run, f"{run.input[:24]}--h", context.historian_flow(root),
                context.HISTORIAN_AGENT,
                {"root": root, "notes": "\n\n".join(notes)[:context.MAX_NOTES] or "Nothing to keep.",
                 "current": secs.get(context.history_of(root), ""),
                 "context": secs.get(root, "")}, "learn:history")
            section = str(out.get("section") or "").strip()
            if not section:
                raise RuntimeError("the history keeper returned no section")
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            self._emit(run, {"type": "context", "root": root, "status": "error",
                             "mode": "history", "message": f"{type(e).__name__}: {e}"[:500]})
            return False
        draft = {"root": root, "run": run.input, "section": section,
                 "summary": str(out.get("summary") or "")[:500],
                 "sessions": used_sessions, "memory": used_memory, "created": time.time()}
        d = self._learn_dir()
        d.mkdir(parents=True, exist_ok=True)
        (d / f"draft-{root}.json").write_text(json.dumps(draft, indent=1), encoding="utf-8")
        self._emit(run, {"type": "context", "root": root, "status": "done", "mode": "history",
                         "draft": True, "summary": draft["summary"]})
        return True

    @staticmethod
    def _audit_context(ws: str, root: str, mode: str, run_id: str) -> None:
        settings.runs_dir.mkdir(parents=True, exist_ok=True)
        with (settings.runs_dir / "audit.jsonl").open("a", encoding="utf-8") as f:
            f.write(json.dumps({"ts": time.time(), "event": "workspace_context", "workspace": ws,
                                "root": root, "mode": mode, "run": run_id}) + "\n")

    def _spawn(self, run: Run, coro: Any) -> None:
        def finished(task: asyncio.Task[None]) -> None:
            # cancelled before it started: the driver never handled the CancelledError
            if task.cancelled() and run.status == "running":
                run.status = "stopped"
                self._emit(run, {"type": "end", "status": "stopped"})

        with self._in(run):  # the task works in the run's workspace, whatever the request
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
            ev = {"type": "end", "status": run.status, "gate": run.gate}
            await self._after_run(run, ev)
            self._emit(run, ev)
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
            await self._after_run(run, ev)
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
            claude_sessions.mark_ours(msg.session_id)
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


def _last_run_events(path: Path) -> list[dict[str, Any]]:
    """The events of the last run written in an events.jsonl (a folder can hold several runs
    with the same run id); [] when there is none."""
    if not path.is_file():
        return []
    events = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            e = json.loads(line)
        except ValueError:
            continue
        if isinstance(e, dict):
            events.append(e)
    starts = [i for i, e in enumerate(events) if e.get("type") == "start"]
    if not starts:
        return []
    last = events[starts[-1]]
    return [e for e in events[starts[-1]:] if e.get("run") == last.get("run")]


def _recorded_fingerprints(events: list[dict[str, Any]]) -> dict[str, str]:
    """node → the fingerprint saved when it last finished well (older runs have none)."""
    out: dict[str, str] = {}
    for e in events:
        if e.get("type") == "agent_done" and e.get("agent") and not e.get("sub"):
            if e.get("error"):
                out.pop(str(e["agent"]), None)
            elif e.get("fp"):
                out[str(e["agent"])] = str(e["fp"])
    return out


def _restore_engine(engine: GraphEngine, events: list[dict[str, Any]], run: Run) -> None:
    """The state of a graph run again, from its events: done/skipped/failed nodes, the decided
    edges, the gate it waits at, the agents' sessions (for "ask for more")."""
    spec = engine.spec
    engine.started = True
    for e in events:
        t, node = e.get("type"), e.get("agent") or e.get("node") or e.get("gate")
        if t == "delegate" and node in engine.runs:
            engine.runs[node] += 1
        elif t == "agent_done" and node in engine.state:
            engine.state[node] = "failed" if e.get("error") else "done"
        elif t in ("skipped", "decided") and node in engine.state:
            engine.state[node] = "skipped" if t == "skipped" else "done"
        elif t == "gate" and node in spec.gates:
            engine.state[node] = "done" if e.get("kind") == "stop" else "gate"
        elif t == "decision" and node in spec.gates:
            engine.state[node] = "done"
        elif t == "edge":
            i = next((k for k, x in enumerate(spec.edges)
                      if x.from_ == e.get("from") and x.to == e.get("to")
                      and k not in engine.back), None)
            if i is not None:
                engine.decided[i] = bool(e.get("fired"))
        elif t == "loop":  # the nodes of a loop went back to waiting
            part = {str(n) for n in e.get("nodes") or []}
            for n in part & set(engine.state):
                engine.state[n] = "waiting"
            for j, ed in enumerate(spec.edges):
                if j not in engine.back and ed.from_ in part and ed.to in part:
                    engine.decided.pop(j, None)
        elif t == "resume":  # an earlier resume reset these nodes
            for n in set(e.get("redo") or []) & set(engine.state):
                engine.state[n] = "waiting"
            for j, ed in enumerate(spec.edges):
                if j not in engine.back and ed.from_ in set(e.get("redo") or []):
                    engine.decided.pop(j, None)
        elif t == "result" and e.get("agent") and e.get("session_id"):
            engine.sessions[str(e["agent"])] = str(e["session_id"])
    if run.status == "gate" and run.gate and run.gate.get("kind") == "human":
        engine.pending_gates = [run.gate]
    elif run.gate and run.gate.get("kind") == "stop":
        engine.stop_gate = run.gate


def list_artifacts(run_input: str) -> list[dict[str, Any]]:
    base = safe_artifact_path(settings.runs_dir, run_input, "x").parent
    if not base.is_dir():
        return []
    return [{"name": p.name, "size": p.stat().st_size} for p in sorted(base.iterdir())
            if p.is_file() and p.suffix in (".json", ".md", ".jsonl") and p.name != "run_meta.json"]


def read_artifact_text(run_input: str, name: str, limit: int = 200_000) -> str:
    path = safe_artifact_path(settings.runs_dir, run_input, name)
    if not path.is_file() or path.suffix not in (".json", ".md", ".jsonl"):
        raise FileNotFoundError(name)
    return path.read_text(encoding="utf-8")[:limit]

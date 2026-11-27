"""The data of a run: its state (`Run`), errors, the run modes and statuses, the constants of the
run panel, and the flow inputs (checked and saved before a run starts)."""
from __future__ import annotations

import asyncio
import base64
import binascii
import re
import shutil
import time
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from ...builder import expr
from ...builder.catalog import CONTRACTS
from ...builder.spec import FlowSpec
from ...builder.types import IOType, check_value
from ...engine import GraphEngine
from ...messages import msg as m_
from ...messages import reason, to_json
from ...policy import ApprovalRequest
from ...tools import io as io_tool
from .. import uploads

# "decided": decided at its gate while another run went; it goes on as soon as no run goes (a
# lasting state: its `end` event keeps the decision, so a restart goes on with it)
Status = Literal["running", "gate", "paused", "decided", "done", "error", "stopped"]
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
RUN_ID = re.compile(r"^[A-Za-z0-9_-]{1,40}$")
# The Claude Code CLI (used by the SDK) is not signed in, or its session expired: the editor shows
# the steps to sign in again instead of the raw error.
AUTH_ERROR = re.compile(
    r"failed to authenticate|oauth (session|token)|not logged in|please run /login"
    r"|invalid (x-)?api[ -]?key|authentication_error|could not be refreshed", re.IGNORECASE)


def error_hint(message: str) -> str | None:
    """'auth' when a run or agent error is a sign-in problem of the Claude Code CLI."""
    return "auth" if AUTH_ERROR.search(message or "") else None
HOLD_FOR = 15 * 60  # a decision being changed holds its run at most this long
STALE_GATE = 24 * 3600  # a gate waiting longer no longer counts as "waits for you"
GATE_SCAN = 30.0  # seconds the scan of saved gates is kept
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
    after: tuple[dict[str, Any], Any, str, float] | None = None  # a decision waiting its turn
    holding: float | None = None  # the person is changing that decision: it does not go on
    policy: Any = None  # orchestrator mode: the write policy of the segment that runs now
    priced: bool = True  # orchestrator mode: False on a gateway provider (cost unknown)
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
        if self.status == "decided" and self.holding:  # until when a change of decision holds it
            out["held_until"] = self.holding + HOLD_FOR
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

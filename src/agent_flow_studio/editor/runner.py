"""Flow runs from the editor: trace as events and decisions at gates.

One run at a time (the `artifacts` module is configured by the active flow). Events are kept in
memory and in `runs/<input>/events.jsonl`; human decisions in `runs/<input>/decisions.jsonl`.
A decision **runs nothing**: it is recorded and, if the gate has outgoing edges, the run resumes
(same session) so that the orchestrator follows the graph. Drafts stay drafts.
Events do not keep tool arguments (they can contain personal data), only the tool name.
"""
from __future__ import annotations

import asyncio
import json
import re
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

from ..builder import load_flow
from ..builder.compile import GATE_TOOL
from ..builder.spec import FlowSpec
from ..config import settings
from ..hooks import DELEGATION_TOOLS
from ..policy import ApprovalRequest
from ..tools.guards import safe_artifact_path

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
    pending: dict[str, tuple[ApprovalRequest, asyncio.Future[tuple[bool, str]]]] = field(
        default_factory=dict)

    def summary(self) -> dict[str, Any]:
        return {"id": self.id, "flow": self.flow, "input": self.input, "status": self.status,
                "session_id": self.session_id, "gate": self.gate, "cost": round(self.cost, 4),
                "started": self.started, "events": len(self.events),
                "pending_approvals": list(self.pending), "mode": self.mode,
                "target": self.target, "done_agents": sorted(self.done_agents)}


class RunError(Exception):
    def __init__(self, status: int, msg: str):
        super().__init__(msg)
        self.status = status


def _text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return " ".join(str(c.get("text", "")) if isinstance(c, dict) else str(c) for c in content)
    return str(content or "")


def _short(name: str) -> str:
    return name.split("__")[-1] if name.startswith("mcp__") else name


class Runner:
    def __init__(self, base: Path | None = None, query_fn: QueryFn = query):
        self.base = base
        self.runs: dict[str, Run] = {}
        self._query = query_fn

    # --- state
    def active(self) -> Run | None:
        return next((r for r in self.runs.values() if r.status == "running"), None)

    def get(self, run_id: str) -> Run:
        if run_id not in self.runs:
            raise RunError(404, "run does not exist")
        return self.runs[run_id]

    def _emit(self, run: Run, ev: dict[str, Any]) -> None:
        ev = {"i": len(run.events), "ts": time.time(), **ev}
        run.events.append(ev)
        try:
            path = safe_artifact_path(settings.runs_dir, run.input, "events.jsonl")
            path.parent.mkdir(parents=True, exist_ok=True)
            disk = {k: v for k, v in ev.items() if k != "detail"}  # contents only in memory
            with path.open("a", encoding="utf-8") as f:
                f.write(json.dumps({"run": run.id, **disk}, ensure_ascii=False) + "\n")
        except (OSError, ValueError):
            pass  # the in-memory trace goes on; the file is only history

    # --- start and resume
    def start(self, flow: str, run_input: str, mode: Mode = "full", target: str | None = None,
              instruction: str = "") -> Run:
        """full: the whole flow · step: one agent at a time · until: runs step by step until
        `target` finishes · agent: only the `target` agent, isolated (with the saved artifacts)."""
        if self.active():
            raise RunError(409, "a run is already in progress; wait or stop it")
        compiled = load_flow(flow, base=self.base)  # validates flow and input before starting
        prompt = compiled.task(run_input)
        if mode in ("until", "agent") and target not in compiled.spec.agents:
            raise RunError(400, f"agent {target!r} does not exist in the flow")
        if mode in ("step", "until"):
            prompt += STEP_HINT
        if mode == "agent":
            prompt = instruction.strip() or f"Run your step of the flow for {run_input}."
        run = Run(uuid.uuid4().hex[:12], flow, run_input, self.base, mode=mode,
                  target=target if mode in ("until", "agent") else None)
        self.runs[run.id] = run
        self._emit(run, {"type": "start", "flow": flow, "input": run_input, "mode": mode,
                         "target": run.target, "mock": settings.mock,
                         "model": compiled.options.model})
        self._launch(run, prompt, None)
        return run

    def continue_(self, run_id: str, how: Literal["step", "full"]) -> Run:
        """Resumes a paused run: one more step, or up to the end."""
        run = self.get(run_id)
        if run.status != "paused":
            raise RunError(409, "the run is not paused")
        if self.active():
            raise RunError(409, "a run is already in progress")
        run.mode, run.status = how, "running"
        self._emit(run, {"type": "continue", "how": how})
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
        def finished(task: asyncio.Task[None]) -> None:
            # cancelled before it started: _drive never handled the CancelledError
            if task.cancelled() and run.status == "running":
                run.status = "stopped"
                self._emit(run, {"type": "end", "status": "stopped"})

        run.task = asyncio.create_task(self._drive(run, prompt, resume))
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
                            self._emit(run, {"type": "gate", **run.gate})
                    else:
                        ev: dict[str, Any] = {"type": "tool", "agent": agent, "tool": _short(b.name)}
                        if _short(b.name) in ("save_artifact", "read_artifact"):
                            ev["artifact"] = str(b.input.get("name", ""))[:80]
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
                    self._emit(run, {"type": "tool_error", "agent": agent})
        elif isinstance(msg, ResultMessage):
            run.session_id = msg.session_id
            run.cost += msg.total_cost_usd or 0.0
            self._emit(run, {"type": "result", "session_id": msg.session_id,
                             "cost": msg.total_cost_usd, "turns": msg.num_turns,
                             "is_error": msg.is_error, "subtype": msg.subtype,
                             "denials": len(msg.permission_denials or [])})
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

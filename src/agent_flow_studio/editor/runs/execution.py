"""Driving a run: the SDK sessions of orchestrator runs (segments) and the graph engine,
the messages turned into events, and the live event stream.
A mixin of `Runner` (editor/runner.py): it uses the runner's state through `self`."""
from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from dataclasses import replace
from pathlib import Path
from typing import Any

from claude_agent_sdk import (
    AssistantMessage,
    ResultMessage,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
)

from ... import claude_sessions, workspaces
from ...builder import load_flow
from ...builder.compile import (
    GATE_TOOL,
    CompiledFlow,
    gate_message,
)
from ...builder.spec import FlowSpec
from ...engine import usage_tokens
from ...hooks import DELEGATION_TOOLS
from ...trace import error_text, tool_detail
from .model import AGENT_TEST, CONTINUE, STEP_DENIAL, STEP_HINT, Mode, Run, _short, _text


class ExecutionMixin:

    @staticmethod
    def first_prompt(compiled: CompiledFlow, run_id: str, values: dict[str, Any], mode: Mode,
                     instruction: str) -> str:
        if mode == "agent":
            return instruction.strip() or f"Run your step of the flow for {run_id}."
        prompt = compiled.task_for(run_id, values)
        return prompt + STEP_HINT if mode in ("step", "until") else prompt

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

    def _spawn(self, run: Run, coro: Any) -> None:
        def finished(task: asyncio.Task[None]) -> None:
            # cancelled before it started: the driver never handled the CancelledError
            if task.cancelled() and run.status == "running":
                run.status = "stopped"
                self._emit(run, {"type": "end", "status": "stopped"})

        with self._in(run):  # the task works in the run's workspace, whatever the request
            run.task = asyncio.create_task(coro)
        run.task.add_done_callback(finished)
        run.task.add_done_callback(lambda _t: self._start_queued())

    # --- execution
    async def _segment(self, run: Run, prompt: str, resume: str | None) -> int:
        """One call to the SDK. Returns how many agents finished in this segment."""
        compiled = load_flow(run.flow, resume=resume, base=run.base)
        options = compiled.options
        policy = compiled.policy
        assert policy is not None
        policy.approver = self._approver(run)
        run.policy = policy
        run.priced = compiled.priced
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
            cost = msg.total_cost_usd if run.priced else None  # never a guess
            run.cost += cost or 0.0
            ev: dict[str, Any] = {"type": "result", "session_id": msg.session_id,
                                  "cost": cost, "turns": msg.num_turns,
                                  "is_error": msg.is_error, "subtype": msg.subtype,
                                  "denials": len(msg.permission_denials or [])}
            tokens = usage_tokens(getattr(msg, "usage", None))
            if tokens:
                ev["tokens"] = tokens
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

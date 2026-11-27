"""Flow runs from the editor: trace as events and decisions at gates.

One run at a time (the `artifacts` module is configured by the active flow). Events are kept in
memory and in `runs/<input>/events.jsonl`; human decisions in `runs/<input>/decisions.jsonl`.
A decision **runs nothing**: it is recorded and, if the gate has outgoing edges, the run resumes
(same session) so that the orchestrator follows the graph. Drafts stay drafts.
Graph flows (`execution: graph`) run through `engine.GraphEngine` instead: the engine state lives
on the `Run`, and continue/decide resume it (the decision feeds the conditions of the gate's edges).
Events do not keep tool arguments (they can contain personal data), only the tool name.

The parts live in `editor/runs/`: `model` (Run, statuses, inputs), `events` (saved events, the
files of a run) and the mixins `queue`, `history`, `control`, `execution` and
`workspace_context`. This module keeps the `Runner` itself: its state, `start` and the event log.
"""
from __future__ import annotations

import json
import time
import uuid
from pathlib import Path
from typing import Any

from claude_agent_sdk import (
    query,
)

from .. import workspaces
from ..builder import load_flow
from ..builder.compile import (
    load_graph,
)
from ..builder.spec import LEGACY_INPUT
from ..config import settings
from ..engine import GraphEngine, RouteFn
from ..tools import io as io_tool
from ..tools.guards import safe_artifact_path
from .runs.control import ControlMixin
from .runs.events import (
    _git_baseline,
    _waited,
    list_artifacts,
    list_run_files,
    read_artifact_text,
    run_file,
)
from .runs.execution import ExecutionMixin
from .runs.history import HistoryMixin
from .runs.model import (
    HOLD_FOR,
    RUN_ID,
    STALE_GATE,
    Mode,
    QueryFn,
    Run,
    RunError,
    Status,
    check_given,
    error_hint,
    input_visible,
    new_run_id,
    prepare_inputs,
)
from .runs.queue import QueueMixin
from .runs.workspace_context import ContextMixin

# what other modules import from here (the runner was split into editor/runs/)
__all__ = ["HOLD_FOR", "RUN_ID", "STALE_GATE", "Mode", "Run", "RunError", "Runner", "Status",
           "_waited", "check_given", "error_hint", "input_visible", "list_artifacts",
           "list_run_files", "new_run_id", "prepare_inputs", "read_artifact_text", "run_file"]

class Runner(QueueMixin, HistoryMixin, ControlMixin, ExecutionMixin, ContextMixin):
    def __init__(self, base: Path | None = None, query_fn: QueryFn = query,
                 route_fn: RouteFn | None = None):
        """query_fn: SDK sessions (the orchestrator, or each agent in graph mode); route_fn: the
        router of `when` conditions in graph mode (default: a small model call via query_fn)."""
        self.base = base
        self.runs: dict[str, Run] = {}
        self._query = query_fn
        self._route = route_fn
        # History: the summary of each run folder, by the time and size of its events file
        self._summaries: dict[Path, tuple[tuple[int, int], dict[str, Any]]] = {}
        # runs asked for while another one runs: they start, in order, when it stops
        self.queued: list[dict[str, Any]] = []
        self.queue_log: list[dict[str, Any]] = []  # queued runs that started or failed to start
        self._restored = False  # the queue and the decided runs come back from disk once
        # saved runs at a gate, per runs folder: (when scanned, items); dropped on any change
        self._gates: dict[Path, tuple[float, list[dict[str, Any]]]] = {}

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
        if ev.get("type") == "end":
            self._gates.clear()  # the saved gates may have changed
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
                             **({"git": git} if (git := _git_baseline(spec)) else {}),
                             **({"handoff": handoff} if handoff else {})})
            self._spawn(run, self._drive_graph(run))
            return run
        prompt = self.first_prompt(compiled, rid, values, mode, instruction)
        self.runs[run.id] = run
        self._emit(run, {"type": "start", "flow": flow, "input": rid, "mode": mode,
                         "target": run.target, "mock": settings.mock,
                         "model": compiled.options.model, "execution": spec.execution,
                         "workspace": ws.name, "environment": env,
                         **({"git": git} if (git := _git_baseline(spec)) else {}),
                         **({"handoff": handoff} if handoff else {})})
        self._launch(run, prompt, None)
        return run


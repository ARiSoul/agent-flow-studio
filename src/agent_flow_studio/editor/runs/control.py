"""What the person does to a run: continue, run from a step, resume, decide at a gate,
approve writes, pause, hold, retry, stop, ask an agent for more; the autonomy refresh.
A mixin of `Runner` (editor/runner.py): it uses the runner's state through `self`."""
from __future__ import annotations

import asyncio
import json
import time
from typing import Any, Literal

from ...builder import FlowError, load_flow, load_spec
from ...builder.compile import (
    load_graph,
)
from ...config import settings
from ...engine import GraphEngine
from ...policy import ApprovalRequest
from ...tools import io as io_tool
from ...tools.guards import safe_artifact_path
from .events import Decision, _recorded_fingerprints, _restore_engine
from .model import CONTINUE, RUN_ID, STEP_HINT, Run, RunError, Status, check_given


class ControlMixin:

    def continue_(self, run_id: str, how: Literal["step", "full"]) -> Run:
        """Resumes a paused run: one more step, or up to the end."""
        run = self.get(run_id)
        if run.status != "paused":
            raise RunError(409, "the run is not paused")
        if self.active():
            raise RunError(409, "a run is already in progress")
        run.mode, run.status = how, "running"
        self._refresh_policy(run)  # what was changed in the flow while it paused applies
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

    def from_plan(self, run_input: str, node: str | None = None) -> dict[str, Any]:
        """What "run from step" on a saved run would do: the state of each step in that run and,
        for `node`, the steps it keeps (`reuse`), runs again (`redo`) and the earlier ones that
        never ran there (`unreached`: their outputs are missing). Nothing runs."""
        try:
            run = self.open_past(run_input, register=False)  # a look only: nothing is kept
        except ValueError as e:
            raise RunError(400, str(e)) from e
        if run.engine is None:
            raise RunError(409, "only graph runs can start from a step")
        engine = self._fresh_engine(run)
        out: dict[str, Any] = {"status": run.status, "states": dict(engine.state)}
        if node:
            if node not in engine.state:
                raise RunError(400, f"{node!r} is not a step of the flow")
            plan = engine.resume_plan(_recorded_fingerprints(run.events), True, start_at=node)
            out |= {k: plan[k] for k in ("reuse", "redo", "unreached", "gates")}
        return out

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
        changed = run.status == "decided" and run.after is not None
        if changed:  # a change of mind before it goes on: the gate is decided again
            run.gate = run.after[0]  # type: ignore[index]
            run.holding = None
        elif run.status != "gate" or not run.gate:
            raise RunError(409, "the run already went on with its decision: open it to see "
                                "what it did" if run.status in ("running", "done") else
                           "the run is not stopped at a gate")
        if run.gate["kind"] != "human":
            raise RunError(409, "a stop gate cannot be approved; fix the cause and run again")
        gate = run.gate
        reached = next((e.get("ts") for e in reversed(run.events)
                        if e.get("type") == "gate" and e.get("gate") == gate["gate"]), None)
        waited = round(time.time() - reached, 1) if reached else None
        record = {"ts": time.time(), "run": run.id, "session_id": run.session_id,
                  "gate": gate["gate"], "decision": decision, "comment": comment,
                  "waited": waited, "via": "editor", **({"supersedes": True} if changed else {})}
        path = safe_artifact_path(settings.runs_dir, run.input, "decisions.jsonl")
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
        self._emit(run, {"type": "decision", "gate": gate["gate"], "decision": decision,
                         "comment": comment, "waited": waited})
        io_tool.record_gate(run.input, gate["gate"], decision, comment)  # for `if` conditions
        live = self.active()
        if live is not None:  # never refused: the decision is kept, the run goes on next
            run.status, run.after = "decided", (gate, decision, comment, time.time())
            self._emit(run, {"type": "turn", "after": live.input})
            self._emit(run, {"type": "end", "status": "decided", "gate": gate,
                             "decision": decision, "comment": comment})
            return run
        return self._continue_gate(run, gate, decision, comment)

    def _continue_gate(self, run: Run, gate: dict[str, Any], decision: Decision,
                       comment: str) -> Run:
        """After a human decision: the run goes on from its gate."""
        run.after = None
        self._refresh_policy(run)  # what was changed in the flow while it waited applies
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

    async def pause(self, run_id: str, node: str | None = None) -> Run:
        """Pauses the running agents of a graph run (all, or one): their sessions stop where they
        are, the run stops `paused` once nothing else runs, and Continue goes on in each paused
        agent's own session. A write approval that waits for a paused agent ends unanswered."""
        run = self.get(run_id)
        if run.engine is None or run.status != "running":
            raise RunError(409, "only a running graph run can be paused")
        hit = await run.engine.pause([node] if node else None)
        if not hit:
            raise RunError(409, "no agent of this run is in a session now (an action or a "
                                "subflow cannot be paused)")
        for req_id, (req, fut) in list(run.pending.items()):
            if req.agent in hit and not fut.done():
                fut.cancel()
                self._emit(run, {"type": "approval_result", "id": req_id, "approved": False,
                                 "summary": req.summary, "comment": "the step was paused"})
        return run

    def refresh_autonomy(self, flow: str) -> list[str]:
        """The flow was saved: its runs in memory that are not over take its autonomy now (the
        flow's and each agent's), also while a step runs. Returns the runs that changed."""
        changed = []
        for run in list(self.runs.values()):
            if run.flow == flow and run.status not in ("done", "error", "stopped") \
                    and self._refresh_policy(run):
                changed.append(run.id)
        return changed

    def _refresh_policy(self, run: Run) -> bool:
        """The run's write policy takes the autonomy of the flow as saved now. A write approval
        that waits and that the new autonomy no longer asks for is approved by it (and logged)."""
        policy = run.engine.compiled.policy if run.engine is not None else run.policy
        if policy is None:
            return False
        try:
            with self._in(run):
                spec, _ = load_spec(run.flow, base=run.base)
        except (FlowError, OSError):
            return False
        agents = {n: a.autonomy for n, a in spec.agents.items() if a.autonomy}
        if (policy.flow_autonomy, policy.agent_autonomy) == (spec.autonomy, agents):
            return False
        policy.flow_autonomy, policy.agent_autonomy = spec.autonomy, agents
        self._emit(run, {"type": "autonomy", "flow": spec.autonomy, "agents": agents})
        for req_id, (req, fut) in list(run.pending.items()):
            level = policy.level(req.agent)
            if fut.done() or not (level == "auto" or (level == "approve_prod"
                                                      and req.environment != "prod")):
                continue
            note = f"approved by the autonomy now set ({level})"
            self._log_decision(run, {"type": "write", "agent": req.agent, "tool": req.tool,
                                     "environment": req.environment, "summary": req.summary,
                                     "decision": "approve", "comment": note, "by": "autonomy"})
            self._emit(run, {"type": "approval_result", "id": req_id, "approved": True,
                             "summary": req.summary, "comment": note})
            fut.set_result((True, note))
        return True

    def hold(self, run_id: str, on: bool) -> Run:
        """The person changes the decision of a decided run: it does not go on meanwhile (for at
        most HOLD_FOR seconds); `on=False` keeps the recorded decision and lets it go on."""
        run = self.get(run_id)
        if run.status != "decided" or not run.after:
            raise RunError(409, "the run already went on with its decision: open it to see "
                                "what it did" if run.status in ("running", "done") else
                           "the run has no decision waiting for its turn")
        run.holding = time.time() if on else None
        self._emit(run, {"type": "hold", "on": on})
        if not on:
            self._start_queued()
        return run

    def retry(self, run_id: str, node: str | None, action: Literal["now", "cancel"]) -> Run:
        """A step waits to run again after a temporary error: run it now, or cancel the retry."""
        run = self.get(run_id)
        if run.engine is None or run.status != "running" or not run.engine.retry_control(
                node, action):
            raise RunError(409, "no step of this run waits to run again")
        return run

    def stop(self, run_id: str) -> Run:
        run = self.get(run_id)
        for _, fut in list(run.pending.values()):
            if not fut.done():
                fut.cancel()
        if run.task and not run.task.done():
            run.task.cancel()
        elif run.status in ("paused", "gate", "decided"):  # no task running: close the run
            run.after = None
            run.status = "stopped"
            self._emit(run, {"type": "end", "status": "stopped"})
            self._start_queued()
        return run

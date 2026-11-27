"""Runs: start, queue, history, decisions, approvals, pause, retry, stop, events, files."""
from __future__ import annotations

from typing import Any

import yaml
from fastapi import Depends, FastAPI, HTTPException
from fastapi.responses import FileResponse, StreamingResponse

from ... import claude_sessions
from ...builder.compile import FlowError
from ...tools import io as io_tool
from .. import assistant
from ..runner import (
    RunError,
    list_artifacts,
    list_run_files,
    read_artifact_text,
    run_file,
)
from .common import (
    SLUG_RE,
    ApprovalBody,
    AskBody,
    ContinueBody,
    Ctx,
    DecisionBody,
    DismissBody,
    OpenRunBody,
    PauseBody,
    ResumeBody,
    RetryBody,
    RunBody,
    RunTitleBody,
    _flow_dir,
    _open_folder,
    _read_json_file,
    _run_call,
)


def register(app: FastAPI, ctx: Ctx) -> None:
    fb = ctx.fb
    csrf = ctx.csrf
    runner = ctx.runner
    # --- runs -----------------------------------------------------------------------------


    @app.get("/api/runs")
    def runs() -> list[dict[str, Any]]:
        return [r.summary() for r in sorted(runner.runs.values(), key=lambda r: -r.started)]

    @app.post("/api/runs", dependencies=[Depends(csrf)], status_code=201)
    async def start_run(body: RunBody) -> dict[str, Any]:
        _flow_dir(fb(), body.flow)
        return _run_call(runner.start, body.flow, body.input, body.mode, body.target,
                         body.instruction, run_id=body.run_id, inputs=body.inputs,
                         given=body.given,
                         handoff={k: v.model_dump() for k, v in body.handoff.items()})

    @app.post("/api/runs-queue", dependencies=[Depends(csrf)], status_code=201)
    def queue_run(body: RunBody) -> dict[str, Any]:
        """A run that starts by itself when the one running now stops."""
        _flow_dir(fb(), body.flow)
        try:
            return runner.enqueue(body.flow, run_input=body.input, mode=body.mode,
                                  target=body.target, instruction=body.instruction,
                                  run_id=body.run_id, inputs=body.inputs, given=body.given,
                                  handoff={k: v.model_dump() for k, v in body.handoff.items()})
        except RunError as e:
            raise HTTPException(e.status, e.detail) from e
        except FlowError as e:
            raise HTTPException(400, str(e)) from e

    @app.get("/api/runs-queue")
    def run_queue() -> dict[str, Any]:
        return runner.queue()

    @app.post("/api/runs-queue/{item_id}/start", dependencies=[Depends(csrf)])
    async def start_queued_now(item_id: str) -> dict[str, Any]:
        """A queued run starts now, even while other runs wait for the person."""
        return _run_call(runner.start_queued_now, item_id)

    @app.delete("/api/runs-queue/{item_id}", dependencies=[Depends(csrf)])
    def unqueue_run(item_id: str) -> dict[str, Any]:
        try:
            runner.unqueue(item_id)
        except RunError as e:
            raise HTTPException(e.status, e.detail) from e
        return {"ok": True}

    @app.get("/api/flows/{flow}/runs")
    def past_runs(flow: str, limit: int = 30, offset: int = 0, q: str = "",
                  status: str = "", days: int = 0) -> dict[str, Any]:
        """The saved runs of a flow (runs/), newest first, a page at a time; `q` and `status`
        filter them; `stats` are the totals of all of them."""
        _flow_dir(fb(), flow)
        runs, total = runner.past_page(flow, max(1, min(limit, 200)), max(0, offset), q[:200],
                                       status[:20])
        return {"runs": runs, "total": total,
                "stats": runner.past_stats(flow, max(0, min(days, 3650)) or None)}

    @app.get("/api/runs/{run_id}/tools/{tool_id}")
    def tool_call(run_id: str, tool_id: str) -> dict[str, Any]:
        """A tool call's arguments and result, read now from the local session (never stored)."""
        try:
            return runner.tool_call(run_id, tool_id)
        except RunError as e:
            raise HTTPException(e.status, e.detail) from e

    @app.get("/api/runs/{run_input}/steps/{node}/session")
    def step_session(run_input: str, node: str) -> dict[str, Any]:
        """A step's Claude Code session: its id, folder and the command to go on with it."""
        try:
            return runner.step_session(run_input, node)
        except ValueError as e:
            raise HTTPException(400, str(e)) from e
        except RunError as e:
            raise HTTPException(e.status, e.detail) from e

    @app.post("/api/runs/{run_input}/steps/{node}/terminal", dependencies=[Depends(csrf)])
    def step_terminal(run_input: str, node: str) -> dict[str, Any]:
        """Opens a terminal on this machine that goes on with the step's session in Claude Code."""
        try:
            s = runner.step_session(run_input, node)
            claude_sessions.open_terminal(s["session_id"], s["cwd"])
        except ValueError as e:
            raise HTTPException(400, str(e)) from e
        except RunError as e:
            raise HTTPException(e.status, e.detail) from e
        except OSError as e:
            raise HTTPException(500, f"could not open a terminal: {e}") from e
        return {"opened": True, **s}

    @app.get("/api/runs/{run_input}/changes")
    def run_changes(run_input: str) -> dict[str, Any]:
        """What the run changed in the git roots of its workspace since it started (read only)."""
        try:
            return runner.run_changes(run_input)
        except ValueError as e:
            raise HTTPException(400, str(e)) from e
        except RunError as e:
            raise HTTPException(e.status, e.detail) from e

    @app.get("/api/runs-waiting")
    def runs_waiting() -> dict[str, Any]:
        """The runs of this workspace that wait for the person (gates, paused, write approvals)."""
        return {"runs": runner.waiting()}

    @app.get("/api/runs/{run_input}/from-plan")
    def from_plan(run_input: str, node: str | None = None) -> dict[str, Any]:
        """What "run from step" on a saved run would keep and run again (nothing runs)."""
        try:
            return runner.from_plan(run_input, node)
        except RunError as e:
            raise HTTPException(e.status, e.detail) from e
        except FlowError as e:
            raise HTTPException(400, str(e)) from e

    @app.get("/api/runs-trash")
    def runs_trash(flow: str | None = None) -> dict[str, Any]:
        return {"runs": runner.trash(flow)}

    @app.post("/api/runs-trash/{name}/restore", dependencies=[Depends(csrf)])
    def restore_run(name: str) -> dict[str, Any]:
        try:
            return {"input": runner.restore_run(name)}
        except RunError as e:
            raise HTTPException(e.status, e.detail) from e

    @app.delete("/api/runs-trash", dependencies=[Depends(csrf)])
    def empty_runs_trash(flow: str | None = None) -> dict[str, Any]:
        """Deletes the runs in the trash (of a flow) for good (the editor asks first)."""
        done, failed = runner.empty_trash(flow)
        return {"deleted": done, "failed": failed}

    @app.post("/api/runs/open", dependencies=[Depends(csrf)])
    def open_run(body: OpenRunBody) -> dict[str, Any]:
        """A saved run again (its log, outputs and sessions): to look at it or ask for more."""
        return _run_call(runner.open_past, body.input)

    @app.post("/api/runs/{run_id}/ask", dependencies=[Depends(csrf)])
    async def ask_run(run_id: str, body: AskBody) -> dict[str, Any]:
        """After a run: ask the agent that did a step (or the orchestrator) for more."""
        return _run_call(runner.ask, run_id, body.message, body.agent)

    @app.get("/api/runs/{run_id}/askable")
    def run_askable(run_id: str) -> dict[str, Any]:
        try:
            return {"agents": runner.askable(run_id)}
        except RunError as e:
            raise HTTPException(e.status, e.detail) from e

    @app.post("/api/runs/{run_id}/continue", dependencies=[Depends(csrf)])
    async def continue_run(run_id: str, body: ContinueBody) -> dict[str, Any]:
        return _run_call(runner.continue_, run_id, body.how)

    @app.get("/api/runs/{run_id}/resume-plan")
    def resume_plan(run_id: str, reuse_changed: bool = False) -> dict[str, Any]:
        """What resuming a run that ended with an error (or was stopped) would keep and repeat."""
        try:
            return runner.resume_plan(run_id, reuse_changed)
        except RunError as e:
            raise HTTPException(e.status, e.detail) from e
        except FlowError as e:
            raise HTTPException(400, str(e)) from e

    @app.put("/api/runs/{run_input}/title", dependencies=[Depends(csrf)])
    def rename_run(run_input: str, body: RunTitleBody) -> dict[str, Any]:
        """A name for a saved run (its folder and id do not change)."""
        try:
            return {"input": run_input, "title": runner.rename(run_input, body.title)}
        except ValueError as e:
            raise HTTPException(400, str(e)) from e
        except RunError as e:
            raise HTTPException(e.status, e.detail) from e

    @app.delete("/api/runs/{run_input}", dependencies=[Depends(csrf)])
    def delete_run(run_input: str) -> dict[str, Any]:
        """Moves a saved run to the trash (`runs/.trash/`); a running run is refused."""
        try:
            return {"input": run_input, "trash": runner.delete_run(run_input)}
        except ValueError as e:
            raise HTTPException(400, str(e)) from e
        except RunError as e:
            raise HTTPException(e.status, e.detail) from e

    @app.post("/api/runs/{run_id}/resume", dependencies=[Depends(csrf)])
    async def resume_run(run_id: str, body: ResumeBody) -> dict[str, Any]:
        return _run_call(runner.resume, run_id, body.reuse_changed)

    @app.post("/api/runs/{run_id}/decision", dependencies=[Depends(csrf)])
    async def decide(run_id: str, body: DecisionBody) -> dict[str, Any]:
        return _run_call(runner.decide, run_id, body.decision, body.comment)

    @app.post("/api/runs/{run_id}/approvals/{approval_id}", dependencies=[Depends(csrf)])
    async def approve_write(run_id: str, approval_id: str, body: ApprovalBody) -> dict[str, Any]:
        return _run_call(runner.approve, run_id, approval_id, body.approve, body.comment)

    @app.post("/api/runs/{run_id}/decision/hold", dependencies=[Depends(csrf)])
    async def hold_decision(run_id: str, body: DismissBody) -> dict[str, Any]:
        """While the person changes a recorded decision, its run does not go on (`dismissed`:
        true holds it, false keeps the decision and lets it go on)."""
        return _run_call(runner.hold, run_id, body.dismissed)

    @app.post("/api/runs/{run_id}/pause", dependencies=[Depends(csrf)])
    async def pause_run(run_id: str, body: PauseBody) -> dict[str, Any]:
        """Pauses running agents (all, or `node`); Continue goes on in their own sessions."""
        try:
            return (await runner.pause(run_id, body.node)).summary()
        except RunError as e:
            raise HTTPException(e.status, e.detail) from e

    @app.post("/api/runs/{run_id}/retry", dependencies=[Depends(csrf)])
    async def retry_run(run_id: str, body: RetryBody) -> dict[str, Any]:
        """A step waiting to run again after a temporary error: now, or cancel the retry."""
        return _run_call(runner.retry, run_id, body.node, body.action)

    @app.put("/api/runs/{run_input}/dismissed", dependencies=[Depends(csrf)])
    async def dismiss_run(run_input: str, body: DismissBody) -> dict[str, Any]:
        """A waiting run out of (or back into) the "waits for you" count."""
        try:
            runner.dismiss(run_input, body.dismissed)
        except ValueError as e:
            raise HTTPException(400, str(e)) from e
        except RunError as e:
            raise HTTPException(e.status, e.detail) from e
        return {"input": run_input, "dismissed": body.dismissed}

    @app.post("/api/runs/{run_id}/stop", dependencies=[Depends(csrf)])
    async def stop_run(run_id: str) -> dict[str, Any]:
        return _run_call(runner.stop, run_id)

    @app.get("/api/runs/{run_id}/events")
    async def run_events(run_id: str, after: int = 0) -> StreamingResponse:
        try:
            runner.get(run_id)
        except RunError as e:
            raise HTTPException(e.status, str(e)) from e

        async def gen():  # type: ignore[no-untyped-def]
            async for ev in runner.stream(run_id, after):
                yield assistant.encode(ev)

        return StreamingResponse(gen(), media_type="application/x-ndjson")

    @app.get("/api/runs/{run_input}/files")
    def run_files(run_input: str) -> dict[str, Any]:
        """The files of a run (agents' files, outputs, inputs) for the previews."""
        try:
            return {"files": list_run_files(run_input)}
        except ValueError as e:
            raise HTTPException(400, str(e)) from e

    @app.get("/api/runs/{run_input}/file")
    def run_file_bytes(run_input: str, path: str) -> FileResponse:
        """One file of a run as it is (an image or a video for the preview, text as plain text,
        never HTML: SVG and HTML come as text/plain)."""
        try:
            f, media_type = run_file(run_input, path)
        except (ValueError, FileNotFoundError) as e:
            raise HTTPException(404, "no such file in this run") from e
        inline = media_type.split(";")[0] != "application/octet-stream"
        return FileResponse(f, media_type=media_type, filename=f.name,
                            content_disposition_type="inline" if inline else "attachment",
                            headers={"X-Content-Type-Options": "nosniff",
                                     "Cache-Control": "no-store",
                                     "Content-Security-Policy": "default-src 'none'; sandbox"})

    @app.get("/api/artifacts/{run_input}")
    def artifacts_list(run_input: str) -> list[dict[str, Any]]:
        try:
            return list_artifacts(run_input)
        except ValueError as e:
            raise HTTPException(400, str(e)) from e

    @app.get("/api/artifacts/{run_input}/{name}")
    def artifact_text(run_input: str, name: str) -> dict[str, Any]:
        try:
            return {"name": name, "text": read_artifact_text(run_input, name)}
        except ValueError as e:
            raise HTTPException(400, str(e)) from e
        except FileNotFoundError as e:
            raise HTTPException(404, "artifact does not exist") from e

    @app.post("/api/runs/{run_input}/reveal", dependencies=[Depends(csrf)])
    def reveal_run(run_input: str) -> dict[str, Any]:
        """Opens the run folder (its outputs folder when it has one) in the file manager of this
        machine. The server only listens on this machine, and the folder is under runs/."""
        try:
            d = io_tool.run_dir(run_input)
        except ValueError as e:
            raise HTTPException(400, str(e)) from e
        if not d.is_dir():
            raise HTTPException(404, "the run has no folder yet")
        target = d / io_tool.OUTPUTS_DIR if (d / io_tool.OUTPUTS_DIR).is_dir() else d
        _open_folder(target)
        return {"opened": str(target)}

    @app.get("/api/io/{run_input}")
    def run_io(run_input: str, flow: str) -> dict[str, Any]:
        """Saved values of a run (read only): flow inputs, outputs per agent, gate decisions and
        the inputs last given to each agent (agent test mode)."""
        d = _flow_dir(fb(), flow)
        try:
            raw = yaml.safe_load((d / "flow.yaml").read_text(encoding="utf-8")) or {}
        except yaml.YAMLError as e:
            raise HTTPException(422, f"flow.yaml is not valid YAML: {e}") from e
        agents = raw.get("agents") if isinstance(raw, dict) else None
        names = [a for a in (agents or {}) if isinstance(a, str) and SLUG_RE.fullmatch(a)]
        try:
            io_dir = io_tool.run_dir(run_input) / io_tool.IO_DIR
            outputs = {a: o for a in names if (o := io_tool.outputs(run_input, a))}
            given = {a: g for a in names
                     if (g := _read_json_file(io_dir / f"{a}{io_tool.GIVEN_SUFFIX}"))}
            return {"inputs": io_tool.flow_inputs(run_input), "outputs": outputs,
                    "gates": _read_json_file(io_dir / io_tool.GATES_FILE), "given": given}
        except ValueError as e:
            raise HTTPException(400, str(e)) from e

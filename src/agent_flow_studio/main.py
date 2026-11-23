"""agent-flow-studio: runs a flow from the command line (no interactive approvals).

    agent-flow-studio INC1 --flow pipeline              # legacy flow: the input is the run id
    agent-flow-studio --flow parallel --set topic="LLM agents" --set docs=@notes.md --run-id r1

Typed flows take their inputs with `--set name=value` (repeat a name for a list; `@path` reads a
file and sends it as an upload; `@@text` is the literal `@text`). Graph flows
(`execution: graph`) run through the studio's engine. Writes that need approval are denied: there
is nobody to approve them here.
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import re
import sys
from collections.abc import AsyncIterator, Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from claude_agent_sdk import AssistantMessage, ResultMessage, TextBlock, ToolUseBlock, query

from . import workspaces
from .builder import FlowError, load_flow, load_spec
from .builder.compile import load_graph
from .builder.spec import LEGACY_INPUT, FlowSpec
from .engine import EngineMode, GraphEngine, Outcome, QueryFn, RouteFn
from .extensions import LOADED
from .hooks import DELEGATION_TOOLS
from .messages import MsgError
from .orchestrator import run_flow
from .tools import io as io_tool

_KEY = re.compile(r"^[a-z][a-z0-9_]{0,39}$")
Out = Callable[[str], None]


@dataclass
class CliState:
    """What the engine updates (the editor uses its `Run`)."""

    cost: float = 0.0
    session_id: str | None = None
    done_agents: set[str] = field(default_factory=set)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="agent-flow-studio",
                                description="Runs a flow (no interactive approvals).")
    p.add_argument("input", nargs="?", help="legacy flows: the input (also the run id in runs/)")
    p.add_argument("--flow", required=True, help="flow in flows/<name>/flow.yaml")
    p.add_argument("--set", dest="sets", action="append", default=[], metavar="NAME=VALUE",
                   help="a flow input (repeat for lists; @path sends a file)")
    p.add_argument("--run-id", help="run id (folder runs/<id>/); default <flow>-<date>-<time>")
    p.add_argument("--resume", help="orchestrator flows: session_id to resume a run")
    p.add_argument("--workspace", help="studio workspace: a registered name, or the folder of a "
                                       "workspace or of its repository (default: the studio)")
    p.add_argument("--env", help="environment of the workspace (default: its default one)")
    return p


STAGE_OVER = 8 * 1024 * 1024  # --set x=@file: larger files are staged (editor/uploads.py)


def parse_sets(sets: Sequence[str], cwd: Path | None = None) -> dict[str, Any]:
    """["a=1", "a=2", "doc=@x.md"] → {"a": ["1", "2"], "doc": {"name", "content_b64"}}."""
    out: dict[str, Any] = {}
    for item in sets:
        name, sep, value = item.partition("=")
        name = name.strip()
        if not sep or not _KEY.fullmatch(name):
            raise ValueError(f"--set {item!r}: use name=value (name in lowercase, digits, _)")
        v: Any = value
        if value.startswith("@@"):
            v = value[1:]
        elif value.startswith("@"):
            path = Path(value[1:]).expanduser()
            path = path if path.is_absolute() else (cwd or Path.cwd()) / path
            if not path.is_file():
                raise ValueError(f"--set {name}: file not found: {path}")
            if path.stat().st_size > STAGE_OVER:  # a video…: not in memory as base64
                from .editor import uploads
                v = {"name": path.name, "upload": uploads.stage(path)["upload"]}
            else:
                v = {"name": path.name,
                     "content_b64": base64.b64encode(path.read_bytes()).decode()}
        if name in out:
            prev = out[name]
            out[name] = [*prev, v] if isinstance(prev, list) else [prev, v]
        else:
            out[name] = v
    return out


def collect_inputs(spec: FlowSpec, positional: str | None, sets: dict[str, Any],
                   run_id: str | None) -> tuple[str, dict[str, Any]]:
    """(run id, raw inputs). Legacy flows: the positional input (or --set input=…) is the one
    input and the run id. Typed flows: --set values and --run-id (or a generated id)."""
    from .editor.runner import RUN_ID, new_run_id

    if not spec.inputs:
        value = positional if positional is not None else sets.get(LEGACY_INPUT)
        extra = set(sets) - {LEGACY_INPUT}
        if extra:
            raise ValueError(f"this flow has one input ({spec.input.label}); unknown: "
                             f"{', '.join(sorted(extra))}")
        if not isinstance(value, str) or not value:
            raise ValueError(f"give the input ({spec.input.label}) as the first argument")
        if run_id and run_id != value:
            raise ValueError("legacy flows: the input is also the run id (do not use --run-id)")
        return value, {LEGACY_INPUT: value}
    if positional is not None:
        raise ValueError("this flow has typed inputs: give them with --set name=value "
                         f"({', '.join(spec.inputs)})")
    rid = (run_id or "").strip() or new_run_id(spec.name)
    if not RUN_ID.fullmatch(rid):
        raise ValueError("invalid run id: letters, digits, - or _ (up to 40)")
    return rid, sets


def format_event(ev: dict[str, Any]) -> str | None:
    """A readable line of the graph trace (None: not shown)."""
    t = ev.get("type")
    agent = ev.get("agent")
    if t == "delegate":
        return f"> {agent}: {ev.get('task', '')}"
    if t == "text":
        return f"  [{agent}] {ev.get('text', '')}"
    if t == "tool":
        extra = f" ({ev['artifact']})" if ev.get("artifact") else ""
        return f"  - {agent}: {ev.get('tool')}{extra}"
    if t == "tool_error":
        return f"  ! {agent}: a tool returned an error"
    if t == "outputs":
        return f"  outputs of {agent}: {', '.join(ev.get('names', [])) or '-'}"
    if t == "follow_up":
        return f"  ! {agent} did not save {', '.join(ev.get('missing', []))}: asking again"
    if t == "agent_done":
        return (f"x {agent} failed: {ev.get('message', '')}" if ev.get("error")
                else f"= {agent} done")
    if t == "edge":
        state = "fired" if ev.get("fired") else "not fired"
        extra = " (loop limit)" if ev.get("limit") else ""
        extra += f" [{ev['error']}]" if ev.get("error") else ""
        return f"  {ev.get('from')} -> {ev.get('to')}: {state} ({ev.get('by')}){extra}"
    if t == "skipped":
        return f"  skipped: {ev.get('node')}"
    if t == "loop":
        return f"  loop {ev.get('from')} -> {ev.get('to')}: again {', '.join(ev.get('nodes', []))}"
    if t == "loop_limit":
        return f"  ! loop {ev.get('from')} -> {ev.get('to')}: max_runs reached " \
               f"({', '.join(ev.get('agents', []))})"
    if t == "route_error":
        return f"  ! conditions of {ev.get('node')} not checked: {ev.get('message')}"
    if t == "gate":
        reason = f" - {ev['reason']}" if ev.get("reason") else ""
        return f"# gate {ev.get('gate')} ({ev.get('kind')}): {ev.get('label')}{reason}"
    if t == "result":
        cost = f"{ev['cost']:.4f}" if isinstance(ev.get("cost"), (int, float)) else "?"
        return (f"  [{agent}] {ev.get('subtype')} session_id={ev.get('session_id')} "
                f"turns={ev.get('turns')} cost~{cost} USD"
                + (f" denials={ev['denials']}" if ev.get("denials") else ""))
    return None


def _printer(out: Out) -> Callable[[dict[str, Any]], None]:
    def emit(ev: dict[str, Any]) -> None:
        line = format_event(ev)
        if line is not None:
            out(line)

    return emit


async def run_graph(flow: str, run_id: str, *, mode: EngineMode = "full", target: str | None = None,
                    query_fn: QueryFn = query, route_fn: RouteFn | None = None,
                    out: Out = print, base: Path | None = None) -> tuple[Outcome, CliState]:
    """Runs a graph flow whose inputs are already saved in runs/<run_id>/ (no approver)."""
    graph = load_graph(flow, base=base)
    state = CliState()
    engine = GraphEngine(graph, run_id, host=state, emit=_printer(out), query_fn=query_fn,
                         route_fn=route_fn, mode=mode, target=target)
    outcome = await engine.run()
    return outcome, state


async def _print_messages(messages: AsyncIterator[Any], out: Out) -> None:
    async for msg in messages:
        if isinstance(msg, AssistantMessage):
            for block in msg.content:
                if isinstance(block, TextBlock):
                    out(block.text)
                elif isinstance(block, ToolUseBlock):
                    sub = block.input.get("subagent_type") if block.name in DELEGATION_TOOLS \
                        else None
                    out(f"  · {block.name}" + (f" → {sub}" if sub else ""))
        elif isinstance(msg, ResultMessage):
            cost = f"{msg.total_cost_usd:.4f}" if msg.total_cost_usd is not None else "?"
            out(f"\n[{msg.subtype}] session_id={msg.session_id} turns={msg.num_turns} "
                f"cost≈{cost} USD")
            if msg.is_error:
                out(f"ERROR: {msg.errors or msg.result or msg.terminal_reason}")
            if msg.permission_denials:
                out(f"Permission denials: {len(msg.permission_denials)}")


async def _typed_orchestrator(flow: str, run_id: str, values: dict[str, Any],
                              resume: str | None, query_fn: QueryFn, out: Out) -> None:
    compiled = load_flow(flow, resume=resume)
    prompt = compiled.task_for(run_id, values)
    await _print_messages(query_fn(prompt=prompt, options=compiled.options), out)


def _report(flow: str, run_id: str, out: Out) -> None:
    report = LOADED.reports.get(flow)  # optional report provided by an extension
    if report is not None:
        try:
            out(f"Report: {report(run_id).resolve()}")
        except (FileNotFoundError, ValueError) as e:
            out(f"Report not generated: {e}")


async def _main(args: argparse.Namespace, query_fn: QueryFn, route_fn: RouteFn | None,
                out: Out) -> int:
    from .editor.runner import RunError, prepare_inputs

    spec, _ = load_spec(args.flow)
    try:
        rid, raw = collect_inputs(spec, args.input, parse_sets(args.sets), args.run_id)
    except ValueError as e:
        raise SystemExit(f"Invalid input: {e}") from e
    if not spec.inputs and spec.execution == "orchestrator":
        await _print_messages(run_flow(args.flow, rid, args.resume), out)  # as before
        _report(args.flow, rid, out)
        return 0
    try:
        values = prepare_inputs(spec, rid, raw)
    except RunError as e:
        raise SystemExit(str(e)) from e
    io_tool.save_flow_inputs(rid, values)
    out(f"Run id: {rid}")
    if spec.execution == "orchestrator":
        await _typed_orchestrator(args.flow, rid, values, args.resume, query_fn, out)
        _report(args.flow, rid, out)
        return 0
    if args.resume:
        out("(--resume is ignored: graph flows run each agent in its own session)")
    outcome, state = await run_graph(args.flow, rid, query_fn=query_fn, route_fn=route_fn,
                                     out=out)
    out(f"\n[{outcome.status}] run {rid}: cost~{state.cost:.4f} USD")
    if outcome.status == "gate" and outcome.gate:
        out(f"Stopped at the human gate {outcome.gate['gate']} ({outcome.gate['label']}): "
            "decide it in the editor (Run panel) to go on.")
    elif outcome.gate:
        out(f"Stopped: {outcome.gate['label']}")
    if outcome.message:
        out(f"ERROR: {outcome.message}")
    _report(args.flow, rid, out)
    return 1 if outcome.status == "error" else 0


def main(argv: Sequence[str] | None = None, *, query_fn: QueryFn = query,
         route_fn: RouteFn | None = None, out: Out = print) -> None:
    args = build_parser().parse_args(argv)
    try:
        ws = workspaces.from_cli(args.workspace, args.env)
    except MsgError as e:
        raise SystemExit(str(e)) from e
    try:
        with workspaces.using(ws, args.env):  # flows, agents, connectors, vars of the workspace
            code = asyncio.run(_main(args, query_fn, route_fn, out))
    except FlowError as e:
        raise SystemExit(f"Invalid flow: {e}") from e
    if code:
        sys.exit(code)


if __name__ == "__main__":
    main()

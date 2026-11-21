"""Typed inputs and outputs of the agents (MCP server `io`).

- `get_inputs(run_id, agent)`: the agent's inputs, resolved from the flow inputs and from the
  outputs of earlier agents.
- `set_output(run_id, agent, name, value)`: saves one output after checking its type
  (builder/types.py). A file output is `{"filename", "content"}` (text, saved in
  `runs/<id>/files/<agent>/`) or `{"path"}` of an existing file in the workspace or the run folder.
- `read_file(run_id, path)`: reads a text file from the run folder (uploaded inputs, file outputs).

The guard hook makes sure `agent` is the agent that calls the tool. Values live in
`runs/<id>/io/`: `_inputs.json` (flow inputs), `_gates.json` (human decisions), `<agent>.json`
(outputs). The compiler sets the active flow (`configure`).
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import TYPE_CHECKING, Any

from claude_agent_sdk import tool

from ..config import settings
from .artifacts import DRAFT_MARKER
from .common import err, ok
from .guards import safe_artifact_path

if TYPE_CHECKING:
    from ..builder.spec import FlowSpec

IO_DIR = "io"
INPUTS_FILE = "_inputs.json"
GATES_FILE = "_gates.json"
GIVEN_SUFFIX = ".given.json"  # free inputs given to an agent (agent test mode)
PREVIOUS_DIR = ".previous"  # runs/.previous/<flow>.json: outputs of the last finished run
MAX_READ = 200_000
_FILE_NAME = re.compile(r"^[A-Za-z0-9_.-]{1,80}$")

_spec: FlowSpec | None = None
_workspace: Path | None = None


def configure(spec: FlowSpec | None, workspace: Path | None) -> None:
    global _spec, _workspace
    _spec, _workspace = spec, workspace


def run_dir(run_id: str) -> Path:
    return safe_artifact_path(settings.runs_dir, run_id, "x").parent


def _io_path(run_id: str, name: str) -> Path:
    d = run_dir(run_id) / IO_DIR
    p = (d / name).resolve()
    if d.resolve() not in p.parents:
        raise ValueError("Path outside the run folder.")
    return p


def _read_json(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(path)


# --- storage (also used by the runner and the engine) --------------------------------------------
def save_flow_inputs(run_id: str, values: dict[str, Any]) -> None:
    _write_json(_io_path(run_id, INPUTS_FILE), values)


def flow_inputs(run_id: str) -> dict[str, Any]:
    return _read_json(_io_path(run_id, INPUTS_FILE))


def outputs(run_id: str, agent: str) -> dict[str, Any]:
    return _read_json(_io_path(run_id, f"{agent}.json"))


def save_given(run_id: str, agent: str, values: dict[str, Any]) -> None:
    _write_json(_io_path(run_id, agent + GIVEN_SUFFIX), values)


def clear_given(run_id: str, agents: list[str]) -> None:
    """Drops the values given in earlier agent tests (they would hide the real outputs)."""
    for a in agents:
        p = _io_path(run_id, a + GIVEN_SUFFIX)
        if p.exists():
            p.unlink()


def record_gate(run_id: str, gate: str, decision: str, comment: str) -> None:
    p = _io_path(run_id, GATES_FILE)
    data = _read_json(p)
    data[gate] = {"decision": decision, "comment": comment}
    _write_json(p, data)


def clear_outputs(run_id: str, agent: str) -> None:
    p = _io_path(run_id, f"{agent}.json")
    if p.exists():
        p.unlink()


def _previous_file(flow: str) -> Path:
    d = settings.runs_dir / PREVIOUS_DIR
    p = (d / f"{flow}.json").resolve()
    if d.resolve() not in p.parents:
        raise ValueError("Path outside the runs folder.")
    return p


def previous_outputs(flow: str) -> dict[str, dict[str, Any]]:
    """The outputs of the last finished run of a flow ({} before the first one)."""
    try:
        data = _read_json(_previous_file(flow))
    except ValueError:
        return {}
    outs = data.get("outputs")
    return outs if isinstance(outs, dict) else {}


def save_previous(flow: str, run_id: str) -> None:
    """At the end of a finished run: its outputs become `previous.*` for the next run."""
    io = run_dir(run_id) / IO_DIR
    outs = {}
    if io.is_dir():
        for f in sorted(io.glob("*.json")):
            if f.name.startswith("_") or f.name.endswith(GIVEN_SUFFIX):
                continue
            outs[f.stem] = _read_json(f)
    import time
    _write_json(_previous_file(flow), {"run_id": run_id, "ts": time.time(), "outputs": outs})


def scope(spec: FlowSpec, run_id: str) -> dict[str, Any]:
    """Everything an `if` expression can see: input.*, <agent>.*, <gate>.decision/comment and
    previous.<agent>.* (the last finished run)."""
    out: dict[str, Any] = {"input": flow_inputs(run_id), "previous": previous_outputs(spec.name)}
    for a in spec.agents:
        out[a] = outputs(run_id, a)
    gates = _read_json(_io_path(run_id, GATES_FILE))
    for g in spec.gates:
        out[g] = gates.get(g, {})
    return out


def resolve_inputs(spec: FlowSpec, run_id: str, agent: str) -> tuple[dict[str, Any], list[str]]:
    """(values, missing required names) for an agent."""
    a = spec.agents[agent]
    given = _read_json(_io_path(run_id, agent + GIVEN_SUFFIX))
    flow_in = flow_inputs(run_id)
    values: dict[str, Any] = {}
    missing: list[str] = []
    cache: dict[str, dict[str, Any]] = {}
    for name, i in a.inputs.items():
        v: Any = None
        if name in given:
            v = given[name]
        elif i.from_:
            root, src = i.from_.split(".", 1)
            if root == "previous":  # empty on the first run: never "missing"
                pa, _, po = src.partition(".")
                v = previous_outputs(spec.name).get(pa, {}).get(po)
                if v is None:
                    continue
            elif root == "input":
                v = flow_in.get(src)
            else:
                cache.setdefault(root, outputs(run_id, root))
                v = cache[root].get(src)
        if v is None or v == "" or v == []:
            if i.required:
                missing.append(name)
            continue
        values[name] = v
    return values, missing


def store_output(spec: FlowSpec, run_id: str, agent: str, name: str, value: Any,
                 workspace: Path | None = None) -> Any:
    """Checks and saves one output. Returns the saved value. Raises ValueError."""
    from ..builder.catalog import CONTRACTS
    from ..builder.types import check_value

    if agent not in spec.agents:
        raise ValueError(f"agent {agent!r} is not in this flow")
    outs = spec.agents[agent].outputs
    if name not in outs:
        raise ValueError(f"agent {agent!r} has no output {name!r}; outputs: "
                         f"{', '.join(outs) or '—'}")
    t = outs[name]
    rd = run_dir(run_id)
    if t.type == "file":
        value = _file_value(rd, agent, value, workspace, t.multiple)
    roots = [r for r in (workspace, rd) if r is not None]
    clean = check_value(t, value, roots=roots, run_dir=rd, contracts=CONTRACTS)
    if t.type == "markdown" and t.draft:
        items = clean if isinstance(clean, list) else [clean]
        items = [x if DRAFT_MARKER in x else f"> **{DRAFT_MARKER}**\n\n{x}" for x in items]
        clean = items if isinstance(clean, list) else items[0]
    p = _io_path(run_id, f"{agent}.json")
    data = _read_json(p)
    data[name] = clean
    _write_json(p, data)
    return clean


def _file_value(rd: Path, agent: str, value: Any, workspace: Path | None, multiple: bool) -> Any:
    """{"filename", "content"} → written in files/<agent>/; {"path"} → copied into the run."""
    if multiple and isinstance(value, list):
        return [_file_value(rd, agent, v, workspace, False) for v in value]
    if isinstance(value, dict) and "content" in value:
        fname = str(value.get("filename") or "")
        if not _FILE_NAME.fullmatch(fname) or ".." in fname:
            raise ValueError("filename: letters, digits, '.', '-' or '_' (up to 80)")
        target = rd / "files" / agent / fname
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(str(value["content"])[:MAX_READ * 5], encoding="utf-8")
        return {"name": fname, "path": f"files/{agent}/{fname}"}
    if isinstance(value, dict) and "path" in value:
        p = Path(str(value["path"]))
        roots = [r.resolve() for r in (workspace, rd) if r is not None]
        p = (p if p.is_absolute() else (workspace or rd) / p).resolve()
        if not any(p == r or r in p.parents for r in roots) or not p.is_file():
            raise ValueError("path: an existing file in the workspace or the run folder")
        if rd.resolve() in p.parents:
            return {"name": p.name, "path": p.relative_to(rd.resolve()).as_posix()}
        target = rd / "files" / agent / p.name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(p.read_bytes())
        return {"name": p.name, "path": f"files/{agent}/{p.name}"}
    raise ValueError("a file output is {filename, content} or {path}")


def read_run_file(run_id: str, rel: str) -> str:
    rd = run_dir(run_id).resolve()
    p = (rd / rel).resolve()
    if rd not in p.parents or not p.is_file():
        raise ValueError(f"file not found in the run folder: {rel}")
    data = p.read_bytes()[:MAX_READ]
    if b"\x00" in data[:4096]:
        raise ValueError("binary file: only text files can be read")
    return data.decode("utf-8", "replace")


# --- MCP tools ---------------------------------------------------------------------------------
def _need_spec() -> FlowSpec:
    if _spec is None:
        raise ValueError("no active flow")
    return _spec


@tool("get_inputs", "Returns your inputs for this run (values resolved from the flow inputs and "
      "from the outputs of earlier agents). Call it first.", {"run_id": str, "agent": str})
async def get_inputs(args):
    try:
        spec = _need_spec()
        if args["agent"] not in spec.agents:
            return err(f"agent {args['agent']!r} is not in this flow")
        values, missing = resolve_inputs(spec, args["run_id"], args["agent"])
    except ValueError as e:
        return err(str(e))
    a = spec.agents[args["agent"]]
    return ok({"inputs": values, "missing": missing,
               "outputs_to_save": {n: t.type + ("[]" if t.multiple else "")
                                   for n, t in a.outputs.items()}})


@tool("set_output", "Saves one of your outputs. 'value' must match the output type (text, number, "
      "date YYYY-MM-DD, url, json…); for a file give {\"filename\", \"content\"} or {\"path\"}.",
      {"type": "object", "properties": {
          "run_id": {"type": "string"}, "agent": {"type": "string"}, "name": {"type": "string"},
          "value": {}}, "required": ["run_id", "agent", "name", "value"]})
async def set_output(args):
    try:
        clean = store_output(_need_spec(), args["run_id"], args["agent"], args["name"],
                             args.get("value"), _workspace)
    except ValueError as e:
        return err(f"output {args.get('name')!r} not saved: {e}. Fix the value and try again.")
    shown = clean if len(json.dumps(clean, ensure_ascii=False)) < 300 else "(saved)"
    return ok({"saved": args["name"], "value": shown})


@tool("read_file", "Reads a text file of the run (an uploaded input or a file output), by its "
      "path in the run folder, e.g. inputs/docs/notes.md.", {"run_id": str, "path": str})
async def read_file(args):
    try:
        return ok(read_run_file(args["run_id"], args["path"]))
    except ValueError as e:
        return err(str(e))


IO_TOOLS = ("get_inputs", "set_output", "read_file")

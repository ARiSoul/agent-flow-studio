"""Typed inputs and outputs of the agents (MCP server `io`).

- `get_inputs(run_id, agent)`: the agent's inputs, resolved from the flow inputs and from the
  outputs of earlier agents.
- `set_output(run_id, agent, name, value)`: saves one output after checking its type
  (builder/types.py). A file output is `{"filename", "content"}` (text, saved in
  `runs/<id>/files/<agent>/`) or `{"path"}` of an existing file in the workspace or the run folder.
- `read_file(run_id, agent, path)`: reads a text file from the run folder (uploaded inputs, file
  outputs).
- `view_image(run_id, agent, path)`: shows an image of the run folder to the agent (tools/media.py).
- `view_video(run_id, agent, path, start?, end?, frames?)`: shows frames of a video (needs ffmpeg).

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
from . import media
from .artifacts import DRAFT_MARKER
from .common import err, ok
from .guards import safe_artifact_path

if TYPE_CHECKING:
    from ..builder.spec import FlowSpec

IO_DIR = "io"
INPUTS_FILE = "_inputs.json"
GATES_FILE = "_gates.json"
HANDOFF_FILE = "_handoff.json"  # where flow inputs came from: input -> {flow, run, agent, output}
GIVEN_SUFFIX = ".given.json"  # free inputs given to an agent (agent test mode)
OUTPUTS_DIR = "outputs"  # runs/<id>/outputs/: text and markdown outputs as files
PREVIOUS_DIR = ".previous"  # runs/.previous/<flow>.json: outputs of the last finished run
MAX_READ = 200_000
_FILE_NAME = re.compile(r"^[A-Za-z0-9_.-]{1,80}$")

_spec: FlowSpec | None = None
_workspace: Path | None = None


# nested runs (subflows): their flow by run id, while the parent flow stays the active one
_by_run: dict[str, tuple[FlowSpec, Path | None]] = {}


def configure(spec: FlowSpec | None, workspace: Path | None) -> None:
    global _spec, _workspace
    _spec, _workspace = spec, workspace


def configure_run(run_id: str, spec: FlowSpec, workspace: Path | None) -> None:
    _by_run[run_id] = (spec, workspace)


def release_run(run_id: str) -> None:
    _by_run.pop(run_id, None)


def spec_for(run_id: str | None) -> FlowSpec | None:
    """The flow of a run: a nested run's own, else the active flow."""
    return _by_run[run_id][0] if run_id in _by_run else _spec


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


def save_handoff(run_id: str, data: dict[str, dict[str, str]]) -> None:
    p = _io_path(run_id, HANDOFF_FILE)
    if data:
        _write_json(p, data)
    elif p.exists():
        p.unlink()  # a new run with the same id, typed by hand


def handoff(run_id: str) -> dict[str, Any]:
    return _read_json(_io_path(run_id, HANDOFF_FILE))


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
    out = run_dir(run_id) / OUTPUTS_DIR
    if out.is_dir():
        for f in out.glob(f"{agent}.*"):
            f.unlink()


def output_file(run_id: str, agent: str, name: str, t: Any, value: Any) -> str | None:
    """A text or markdown output also as a file the person can open: runs/<id>/outputs/
    <agent>.<output>.md (.txt for text). Returns its path in the run folder, or None."""
    if t.type not in ("text", "markdown"):
        return None
    items = value if isinstance(value, list) else [value]
    if not all(isinstance(x, str) for x in items):
        return None
    ext = ".md" if t.type == "markdown" else ".txt"
    d = run_dir(run_id) / OUTPUTS_DIR
    d.mkdir(parents=True, exist_ok=True)
    path = d / f"{agent}.{name}{ext}"
    path.write_text("\n\n---\n\n".join(items), encoding="utf-8")
    return f"{OUTPUTS_DIR}/{path.name}"


def _previous_file(flow: str) -> Path:
    from .. import workspaces
    ws = workspaces.current()
    # the studio keeps runs/.previous/<flow>.json; a workspace has its own folder
    d = workspaces.scoped(settings.runs_dir / PREVIOUS_DIR)
    del ws
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
    from .. import workspaces
    values, roots, _ = workspaces.resolved()
    out: dict[str, Any] = {"input": flow_inputs(run_id), "previous": previous_outputs(spec.name),
                           "vars": values, "roots": roots}
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
            elif root in spec.gates:  # a human gate's decision/comment: empty until it is asked
                v = _read_json(_io_path(run_id, GATES_FILE)).get(root, {}).get(src)
                if v is None or v == "":
                    continue
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
    output_file(run_id, agent, name, t, clean)
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


def run_file(run_id: str, rel: str) -> Path:
    """A file of the run folder, never outside it."""
    rd = run_dir(run_id).resolve()
    p = (rd / rel).resolve()
    if rd not in p.parents or not p.is_file():
        raise ValueError(f"file not found in the run folder: {rel}")
    return p


def read_run_file(run_id: str, rel: str) -> str:
    p = run_file(run_id, rel)
    with p.open("rb") as f:
        data = f.read(MAX_READ)
    if media.image_type(data[:16]):
        raise ValueError("this is an image: use view_image")
    if media.video_format(data[:16]):
        raise ValueError("this is a video: use view_video")
    if b"\x00" in data[:4096]:
        raise ValueError("binary file: only text files can be read")
    return data.decode("utf-8", "replace")


# --- MCP tools ---------------------------------------------------------------------------------
def _need_spec(run_id: str | None = None) -> FlowSpec:
    spec = spec_for(run_id)
    if spec is None:
        raise ValueError("no active flow")
    return spec


@tool("get_inputs", "Returns your inputs for this run (values resolved from the flow inputs and "
      "from the outputs of earlier agents). Call it first.", {"run_id": str, "agent": str})
async def get_inputs(args):
    try:
        spec = _need_spec(args.get("run_id"))
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
        rid = args["run_id"]
        clean = store_output(_need_spec(rid), rid, args["agent"], args["name"],
                             args.get("value"), _by_run[rid][1] if rid in _by_run else _workspace)
    except ValueError as e:
        return err(f"output {args.get('name')!r} not saved: {e}. Fix the value and try again.")
    shown = clean if len(json.dumps(clean, ensure_ascii=False)) < 300 else "(saved)"
    out: dict[str, Any] = {"saved": args["name"], "value": shown}
    f = run_dir(args["run_id"]) / OUTPUTS_DIR
    files = sorted(f.glob(f"{args['agent']}.{args['name']}.*")) if f.is_dir() else []
    if files:  # say where the person finds it (do not invent another place)
        out["file"] = f"runs/{args['run_id']}/{OUTPUTS_DIR}/{files[0].name}"
    return ok(out)


@tool("read_file", "Reads a text file of the run (an uploaded input or a file output), by its "
      "path in the run folder, e.g. inputs/docs/notes.md.",
      {"run_id": str, "agent": str, "path": str})
async def read_file(args):
    try:
        return ok(read_run_file(args["run_id"], args["path"]))
    except ValueError as e:
        return err(str(e))


@tool("view_image", "Shows you an image of the run (PNG, JPEG, GIF or WebP: an uploaded input or a "
      "file output), by its path in the run folder, e.g. inputs/shot/screen.png. A large image is "
      "scaled down.", {"run_id": str, "agent": str, "path": str})
async def view_image(args):
    try:
        return {"content": await media.image_blocks(run_file(args["run_id"], args["path"]))}
    except (ValueError, OSError) as e:
        return err(str(e))


@tool("view_video", "Shows you frames of a video of the run (MP4, MOV, WebM, MKV or AVI), evenly "
      "spread between start and end (seconds; default the whole video), each with its time. "
      f"frames: 1 to {media.MAX_FRAMES} (default {media.DEFAULT_FRAMES}). You get no sound. Look "
      "at the whole video first, then at the parts that matter.",
      {"type": "object", "properties": {
          "run_id": {"type": "string"}, "agent": {"type": "string"}, "path": {"type": "string"},
          "start": {"type": "number", "minimum": 0}, "end": {"type": "number", "minimum": 0},
          "frames": {"type": "integer", "minimum": 1, "maximum": media.MAX_FRAMES}},
       "required": ["run_id", "agent", "path"]})
async def view_video(args):
    try:
        blocks = await media.video_blocks(
            run_file(args["run_id"], args["path"]), float(args.get("start") or 0),
            args.get("end"), int(args.get("frames") or media.DEFAULT_FRAMES))
    except (ValueError, OSError) as e:
        return err(str(e))
    return {"content": blocks}


IO_TOOLS = ("get_inputs", "set_output", "read_file", "view_image", "view_video")

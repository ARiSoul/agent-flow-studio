"""The saved events of a run (`runs/<id>/events.jsonl`) and what is built from them: the last
run, totals, the engine restored after a restart; the files a run made, for the editor."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Literal

from ... import changes, workspaces
from ...builder.compile import (
    _workspace_dir,
)
from ...builder.spec import FlowSpec
from ...config import settings
from ...engine import GraphEngine
from ...tools import media
from ...tools.guards import safe_artifact_path
from .model import Run

Decision = Literal["approve", "reject", "answer"]


def _read_events(path: Path) -> list[dict[str, Any]]:
    """Every event written in an events.jsonl (bad lines are skipped)."""
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
    return events


def _last_run_events(path: Path) -> list[dict[str, Any]]:
    """The events of the last run written in an events.jsonl (a folder can hold several runs
    with the same run id); [] when there is none."""
    return _last_run(_read_events(path))


def _last_run(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
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


def _git_baseline(spec: FlowSpec) -> dict[str, Any]:
    """The state of each git root of the run's workspace (and of the flow's own folder) at the
    start of a run: what `run_changes` compares with later. Nothing is written to the roots."""
    roots = dict(workspaces.resolved()[1])
    try:
        own = _workspace_dir(spec, check=False)
    except Exception:  # noqa: BLE001 — a flow without a usable folder: only the roots
        own = None
    if own is not None and all(Path(p).resolve() != Path(own).resolve() for p in roots.values()):
        roots["workspace"] = str(own)
    out: dict[str, Any] = {}
    for name, path in roots.items():
        snap = changes.snapshot(Path(path))
        if snap is not None:
            out[name] = {"path": str(path), **snap}
    return out


def _waited(events: list[dict[str, Any]], until: float | None) -> float:
    """Seconds a run waited for the person at human gates (a gate still open counts until
    `until`, or not at all when None)."""
    total, open_at = 0.0, {}
    for e in events:
        if e.get("type") == "gate" and e.get("kind") == "human" and e.get("ts"):
            open_at[e.get("gate")] = float(e["ts"])
        elif e.get("type") == "decision" and e.get("gate") in open_at and e.get("ts"):
            total += float(e["ts"]) - open_at.pop(e.get("gate"))
    if until is not None:
        total += sum(max(0.0, until - t) for t in open_at.values())
    return total


def _totals(runs: list[dict[str, Any]]) -> dict[str, Any]:
    took = sorted(r["ended"] - r["started"] for r in runs
                  if r["status"] == "done" and r.get("ended") and r.get("started"))
    return {"runs": len(runs),
            "failed": sum(1 for r in runs if r["status"] == "error"),
            "lost": sum(1 for r in runs if r["status"] == "lost"),
            "median": took[len(took) // 2] if took else None,
            "cost": round(sum(r["cost"] for r in runs), 4)}


def _restore_engine(engine: GraphEngine, events: list[dict[str, Any]], run: Run) -> None:
    """The state of a graph run again, from its events: done/skipped/failed nodes, the decided
    edges, the gate it waits at, the agents' sessions (for "ask for more")."""
    spec = engine.spec
    engine.started = True
    for e in events:
        t, node = e.get("type"), e.get("agent") or e.get("node") or e.get("gate")
        if t == "delegate" and node in engine.runs:
            engine.runs[node] += 0 if e.get("retry") or e.get("after_pause") else 1
            engine.paused.pop(node, None)
            if e.get("task") != "follow-up":  # started: interrupted unless it ends below
                engine.state[node] = "running"
        elif t == "paused" and node in engine.state:  # paused by the person: goes on in its session
            engine.state[node] = "paused"
            engine.runs[node] = max(0, engine.runs[node] - 1)
            engine.paused[node] = e.get("session")
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


# --- the files of a run, for the previews of the editor ------------------------------------------
FILE_DIRS = ("files", "outputs", "inputs")  # what the editor may show of a run folder (not io/)
TEXT_EXTS = frozenset({".txt", ".md", ".json", ".jsonl", ".yaml", ".yml", ".csv", ".log", ".js",
                       ".mjs", ".ts", ".tsx", ".jsx", ".py", ".css", ".html", ".xml", ".glsl",
                       ".vert", ".frag", ".svg", ".toml", ".ini", ".sh", ".ps1"})
_VIDEO_TYPES = {".mp4": "video/mp4", ".m4v": "video/mp4", ".mov": "video/quicktime",
                ".webm": "video/webm"}


def file_kind(path: Path) -> str:
    ext = path.suffix.lower()
    if ext in media.IMAGE_EXTS:
        return "image"
    if ext in _VIDEO_TYPES:
        return "video"
    return "text" if ext in TEXT_EXTS else "other"


def list_run_files(run_input: str, limit: int = 300) -> list[dict[str, Any]]:
    """The files of a run (the agents' files, outputs, inputs): path, size and kind."""
    base = safe_artifact_path(settings.runs_dir, run_input, "x").parent
    out: list[dict[str, Any]] = []
    for top in FILE_DIRS:
        root = base / top
        if not root.is_dir():
            continue
        for f in sorted(root.rglob("*")):
            if f.is_file() and not any(part.startswith(".") for part in f.relative_to(base).parts):
                out.append({"path": f.relative_to(base).as_posix(), "size": f.stat().st_size,
                            "kind": file_kind(f)})
                if len(out) >= limit:
                    return out
    return out


def run_file(run_input: str, rel: str) -> tuple[Path, str]:
    """A file of a run to serve (inside files/, outputs/ or inputs/ only) and its media type:
    images by their bytes, videos by extension, text (SVG too) as text/plain, else bytes."""
    base = safe_artifact_path(settings.runs_dir, run_input, "x").parent.resolve()
    parts = Path(rel).parts
    if not parts or parts[0] not in FILE_DIRS or any(p in ("..", "") or p.startswith(".") for p in parts):
        raise FileNotFoundError(rel)
    f = (base / rel).resolve()
    if not f.is_file() or not f.is_relative_to(base):
        raise FileNotFoundError(rel)
    kind = file_kind(f)
    with f.open("rb") as fh:
        head = fh.read(16)
    if kind == "image":
        return f, media.image_type(head) or "application/octet-stream"
    if kind == "video":
        return f, _VIDEO_TYPES[f.suffix.lower()]
    return f, "text/plain; charset=utf-8" if kind == "text" else "application/octet-stream"


def read_artifact_text(run_input: str, name: str, limit: int = 200_000) -> str:
    path = safe_artifact_path(settings.runs_dir, run_input, name)
    if not path.is_file() or path.suffix not in (".json", ".md", ".jsonl"):
        raise FileNotFoundError(name)
    return path.read_text(encoding="utf-8")[:limit]

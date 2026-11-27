"""Shared parts of the editor API: request bodies, the flow files, small helpers and the
context the routers get (`Ctx`)."""
from __future__ import annotations

import html
import json
import re
import subprocess
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

import yaml
from fastapi import HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field

from ...builder.compile import FlowError, find_flow, flow_locations
from ...builder.spec import SLUG
from ...config import settings
from ...connections.model import Connection
from ...extensions import LOADED
from ...publish import (
    PUBLIC,
)
from ..runner import (
    RunError,
    Runner,
)

STATIC = Path(__file__).parent.parent / "static"  # editor/static (this file is in editor/api/)
FLOW_NAME = re.compile(r"^[a-z0-9][a-z0-9_-]{0,39}$")
PROMPT_PATH = re.compile(r"^prompts/[A-Za-z0-9_-]{1,60}\.md$")
MAX_PROMPT = 100_000
MODELS = ["inherit", "sonnet", "opus", "haiku"]
CSRF_HEADER = "x-flow-editor"
QUEUE_TICK = 10.0  # seconds between two looks of the queue's own clock
SLUG_RE = re.compile(SLUG)
# validation errors about secrets written in the flow: a flow is never saved with one of these
SECRET_ERROR = re.compile(r"secrets? (stay|go) out|looks like a secret|\$\{VAR\}", re.IGNORECASE)


class FlowBody(BaseModel):
    spec: dict[str, Any]
    prompts: dict[str, str] = Field(default_factory=dict)
    force: bool = False  # save even with problems (the person confirmed); secrets never


class AssistBody(BaseModel):
    flow: str
    message: str = Field(min_length=1, max_length=20_000)
    spec: dict[str, Any]
    prompts: dict[str, str] = Field(default_factory=dict)
    session_id: str | None = Field(default=None, pattern=r"^[A-Za-z0-9-]{1,80}$")
    lang: str | None = Field(default=None, max_length=10)  # "pt" | "en" | "es"; else English
    command: str | None = Field(default=None, pattern=r"^[a-z0-9]{6,32}$")  # saved command used


class WsAssistBody(BaseModel):
    message: str = Field(min_length=1, max_length=20_000)
    session_id: str | None = Field(default=None, pattern=r"^[A-Za-z0-9-]{1,80}$")
    lang: str | None = Field(default=None, max_length=10)
    command: str | None = Field(default=None, pattern=r"^[a-z0-9]{6,32}$")


class HandoffRef(BaseModel):
    flow: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]{0,39}$")
    run: str = Field(pattern=r"^[A-Za-z0-9_-]{1,40}$")
    agent: str = Field(pattern=r"^[a-z][a-z0-9_]{0,39}$")
    output: str = Field(pattern=r"^[a-z][a-z0-9_]{0,39}$")


class RunBody(BaseModel):
    flow: str
    input: str | None = Field(default=None, min_length=1, max_length=80)  # legacy flows
    run_id: str | None = Field(default=None, max_length=40)  # typed flows (default: generated)
    inputs: dict[str, Any] = Field(default_factory=dict)  # typed flows; files {name, content_b64}
    given: dict[str, Any] = Field(default_factory=dict)  # agent mode: free inputs of the agent
    mode: Literal["full", "step", "until", "agent", "from"] = "full"  # from: an earlier run
    target: str | None = Field(default=None, max_length=40)
    instruction: str = Field(default="", max_length=20_000)
    # where inputs came from ("Fill from a run"): input -> {flow, run, agent, output}
    handoff: dict[str, HandoffRef] = Field(default_factory=dict, max_length=50)


class PauseBody(BaseModel):
    node: str | None = Field(default=None, max_length=64)


class RetryBody(BaseModel):
    action: Literal["now", "cancel"]
    node: str | None = Field(default=None, max_length=64)


class DismissBody(BaseModel):
    dismissed: bool = True


class OpenRunBody(BaseModel):
    input: str = Field(pattern=r"^[A-Za-z0-9_-]{1,64}$")


class AskBody(BaseModel):
    message: str = Field(min_length=1, max_length=20_000)
    agent: str | None = Field(default=None, max_length=64)


class ContinueBody(BaseModel):
    how: Literal["step", "full"] = "step"


class RunTitleBody(BaseModel):
    title: str = Field(default="", max_length=200)


class ResumeBody(BaseModel):
    reuse_changed: bool = False  # keep the work of nodes edited since they ran


class AllowPostBody(BaseModel):
    allow: bool = True


class ActionTestBody(BaseModel):
    params: dict[str, Any] = Field(default_factory=dict)


class RecordBody(BaseModel):
    banner: str = Field(default="", max_length=300)  # the hint shown in the window (UI language)


class DecisionBody(BaseModel):
    decision: Literal["approve", "reject", "answer"]
    comment: str = Field(default="", max_length=20_000)


class ApprovalBody(BaseModel):
    approve: bool
    comment: str = Field(default="", max_length=5_000)


OAUTH_CALLBACK = "/api/oauth/callback"


def _oauth_page(ok: bool, text: str) -> HTMLResponse:
    """Small page for the end of a sign-in. Plain HTML, no scripts (the CSP has none)."""
    title = "Signed in" if ok else "Sign-in failed"
    body = (f"<!doctype html><html lang='en'><head><meta charset='utf-8'><title>{title}</title>"
            "<style>body{font:15px system-ui,sans-serif;margin:15vh auto;max-width:560px;"
            "padding:0 20px}h1{font-size:20px}</style></head>"
            f"<body><h1>{'✓' if ok else '✗'} {title}</h1><p>{html.escape(text)}</p></body></html>")
    return HTMLResponse(body, status_code=200 if ok else 400)


class SecretBody(BaseModel):
    value: str | None = Field(default=None, min_length=1, max_length=10_000)  # goes to the keyring
    env: str | None = Field(default=None, pattern=r"^[A-Z_][A-Z0-9_]*$")  # or ${VAR}


class LibraryBody(BaseModel):
    id: str = Field(min_length=1, max_length=40)
    title: str = Field(default="", max_length=80)
    agent: dict[str, Any] | None = None
    agent_yaml: str | None = Field(default=None, max_length=50_000)  # or the definition as YAML
    prompt: str = Field(max_length=MAX_PROMPT)
    overwrite: bool = False
    location: str = Field(default="local", max_length=40)  # local library or an extension


class CopyBody(BaseModel):
    """Copy one item to another workspace (or duplicate it in the same one)."""
    kind: Literal["flow", "agent", "flow_agent", "connector", "vars"]
    to: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,39}$")
    name: str = Field(default="", max_length=40)  # flow, library agent or connector
    agent: str | None = Field(default=None, max_length=40)  # flow_agent: the agent of `name`
    to_flow: str | None = Field(default=None, max_length=40)  # flow_agent: target flow
    vars: list[str] = Field(default_factory=list, max_length=200)
    environments: list[str] = Field(default_factory=list, max_length=50)
    new_name: str | None = Field(default=None, max_length=40)
    replace: bool = False
    deps: dict[str, Literal["copy", "target", "leave"]] = Field(default_factory=dict)
    same_secrets: bool = False  # copy keyring secrets on this machine (never in files)


class DuplicateBody(BaseModel):
    name: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,39}$")
    title: str = Field(default="", max_length=120)
    where: Literal["repo", "local"] = "local"
    repo: str | None = Field(default=None, max_length=1000)
    same_secrets: bool = False


class NewWorkspace(BaseModel):
    name: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,39}$")
    title: str = Field(default="", max_length=120)
    description: str = Field(default="", max_length=2000)
    where: Literal["repo", "local"] = "local"  # in a repository (.agent-flow/) or in the studio
    repo: str | None = Field(default=None, max_length=1000)


class NewFlow(BaseModel):
    name: str
    title: str = Field(min_length=1, max_length=120)
    location: str = Field(default="local", max_length=40)  # flows/ or an extension's flows/
    copy_from: str | None = None
    template: str | None = Field(default=None, pattern=r"^[a-z][a-z0-9-]{0,39}$")
    params: dict[str, str] = Field(default_factory=dict)


def _blank(name: str, title: str) -> dict[str, Any]:
    return {
        # no inputs and no legacy task: a run only needs its id (add typed inputs when needed)
        # graph: the studio runs the steps (resume, run from a step, retry, pause need it)
        "version": 1, "name": name, "title": title, "execution": "graph",
        "start": "first",
        "agents": {"first": {"description": "First step of the flow.",
                             "prompt": "Describe here what this agent does.",
                             "tools": []}},
        "gates": {"end": {"kind": "human", "label": "Human review"}},
        "edges": [{"from": "first", "to": "end"}],  # graph: no condition = it always goes on
        "layout": {"first": {"x": 0, "y": 0}, "end": {"x": 0, "y": 160}},
    }


# --- files ---------------------------------------------------------------------------------------
def _flow_dir(base: Path, name: str, must_exist: bool = True) -> Path:
    """An existing flow's folder (in flows/ or in an extension), or base/name for a new one."""
    if not FLOW_NAME.fullmatch(name):
        raise HTTPException(400, "invalid flow name")
    found = find_flow(name, base)
    if found:
        return found[1]
    if must_exist:
        raise HTTPException(404, f"flow {name!r} does not exist")
    return base / name


def _locations() -> list[str]:
    """Where flows, library agents and connectors can be kept: the open workspace (when one is),
    this machine, an extension, or public (git)."""
    from ... import workspaces
    own = [] if workspaces.current().studio else ["workspace"]
    return [*own, "local", *LOADED.names, PUBLIC]


def _location_dir(base: Path, location: str) -> Path:
    from ... import workspaces
    if not workspaces.current().studio:  # a workspace keeps its flows in one place
        if location not in ("workspace", "local"):
            raise HTTPException(400, "a workspace keeps its flows in its own folder")
        return workspaces.current().flows_dir
    if location == PUBLIC:  # flows/ too, with an exception in .gitignore
        return base
    for loc, d in flow_locations(base):
        if loc == location:
            return d
    raise HTTPException(400, f"unknown location {location!r} (local, public or a loaded "
                             "extension)")


def _flow_place(base: Path, name: str) -> str:
    """local · public (git publishes it) · an extension's name."""
    found = find_flow(name, base)
    if found is None:
        raise HTTPException(404, f"flow {name!r} does not exist")
    loc, d = found
    return PUBLIC if loc == "local" and _in_public_repo(d / "flow.yaml") else loc


def _flow_text(d: Path) -> str:
    """Everything of a flow that would be published (flow.yaml and its prompts)."""
    parts = [(d / "flow.yaml").read_text(encoding="utf-8")] if (d / "flow.yaml").is_file() else []
    return "\n".join([*parts, *_disk_prompts(d).values()])


def _in_public_repo(path: Path) -> bool:
    """True when the path is inside this git repository and git would commit it (not ignored).
    Extensions are their own (private) places, so only local paths are checked."""
    probe = path if path.exists() else path.parent
    try:
        r = subprocess.run(["git", "check-ignore", "-q", str(path)], cwd=str(probe if probe.is_dir()
                           else probe.parent), capture_output=True, timeout=5, check=False)
    except (OSError, subprocess.SubprocessError):
        return False
    return r.returncode == 1  # 0 = ignored, 128 = not a git repository


def _disk_prompts(d: Path) -> dict[str, str]:
    p = d / "prompts"
    if not p.is_dir():
        return {}
    return {f"prompts/{f.name}": f.read_text(encoding="utf-8")
            for f in sorted(p.glob("*.md")) if PROMPT_PATH.fullmatch(f"prompts/{f.name}")}


def _read_json_file(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _open_folder(path: Path) -> None:
    """The file manager of this machine on a folder (Explorer, Finder or xdg-open)."""
    import os
    import sys
    if sys.platform.startswith("win"):
        os.startfile(str(path))  # type: ignore[attr-defined]
    elif sys.platform == "darwin":
        subprocess.Popen(["open", str(path)])
    else:
        subprocess.Popen(["xdg-open", str(path)])


def _tests_file(name: str) -> Path:
    """runs/.connector-tests/<connector>.json: the last test of each action (local only; one
    folder per workspace)."""
    from ... import workspaces
    return workspaces.scoped(settings.runs_dir / ".connector-tests") / f"{name}.json"


def _write_tests(name: str, tests: dict[str, Any]) -> None:
    f = _tests_file(name)
    f.parent.mkdir(parents=True, exist_ok=True)
    tmp = f.with_suffix(".tmp")
    tmp.write_text(json.dumps(tests, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(f)


def _actions_yaml(c: Connection) -> str:
    data = {k: a.model_dump(mode="json", exclude_defaults=True) for k, a in c.actions.items()}
    return yaml.safe_dump(data, allow_unicode=True, sort_keys=False, width=100) if data else ""


def _audit_event(event: str, **fields: Any) -> None:
    """A line in runs/audit.jsonl for a change made in the editor (names and sizes, no content)."""
    settings.runs_dir.mkdir(parents=True, exist_ok=True)
    with (settings.runs_dir / "audit.jsonl").open("a", encoding="utf-8") as f:
        f.write(json.dumps({"ts": time.time(), "event": event, **fields}, ensure_ascii=False) + "\n")


def _check_prompts(prompts: dict[str, str]) -> None:
    for k, v in prompts.items():
        if not PROMPT_PATH.fullmatch(k):
            raise HTTPException(400, f"invalid prompt path: {k!r} (use prompts/<name>.md)")
        if len(v) > MAX_PROMPT:
            raise HTTPException(400, f"{k}: prompt too long")


def _run_call(fn, *a, **kw):  # type: ignore[no-untyped-def]
    try:
        return fn(*a, **kw).summary()
    except RunError as e:
        raise HTTPException(e.status, e.detail) from e
    except FlowError as e:
        raise HTTPException(400, str(e)) from e


@dataclass
class Ctx:
    """What the routers share: the studio's flows folder, the flows folder of the current
    workspace, the write check (X-Flow-Editor header) and the runner."""
    base: Path
    fb: Callable[[], Path]
    csrf: Callable[..., None]
    runner: Runner

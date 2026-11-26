"""agent-flow-studio-editor: local visual flow editor (FastAPI + React Flow canvas).

It only edits files in flows/<name>/ (flow.yaml and prompts/*.md). It does not read `.env` and
never expands `${VAR}`: secrets stay out of the flows. It listens on 127.0.0.1 by default, accepts
only local Hosts and requires the X-Flow-Editor header on requests that change files (an external
site in the browser cannot send it without a CORS preflight, which is not accepted here).
The one GET that changes state is the OAuth callback, protected by its single-use `state`.
"""
from __future__ import annotations

import argparse
import asyncio
import html
import json
import re
import shutil
import subprocess
import time
import webbrowser
from pathlib import Path
from typing import Any, Literal

import httpx
import yaml
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, ValidationError
from starlette.middleware.trustedhost import TrustedHostMiddleware

from .. import context, workspaces
from ..builder.catalog import BUILTIN_SERVERS, BUILTIN_TOOLS, CONTRACTS
from ..builder.compile import FlowError, find_flow, flow_locations, flows_dir
from ..builder.library import LibraryError, agents_dict, delete_agent, move_agent, save_local
from ..builder.spec import SLUG, FlowSpec
from ..builder.templates import TemplateError, templates_dict
from ..builder.templates import render as render_template
from ..builder.types import KINDS as IO_KINDS_ALL
from ..builder.types import IOType
from ..config import settings
from ..connections import actions as conn_actions
from ..connections import client as conn_client
from ..connections import oauth as conn_oauth
from ..connections import recorder as conn_rec
from ..connections import store as conn_store
from ..connections import web as conn_web
from ..connections.model import SLUG as CONN_SLUG
from ..connections.model import Connection, types_meta
from ..extensions import LOADED
from ..messages import MsgError, reason, to_json
from ..publish import (
    PUBLIC,
    PrivateTermsError,
    check_public,
    set_flow_public,
    terms_in,
)
from ..tools import io as io_tool
from . import assistant, copying, uploads, ws_assistant
from . import chats as assist_chats
from . import commands as assist_commands
from .runner import RunError, Runner, input_visible, list_artifacts, read_artifact_text
from .validation import _atomic_write, _public, _structured, _validate
from .yaml_io import dump_flow, dump_raw, spec_to_data

STATIC = Path(__file__).parent / "static"
FLOW_NAME = re.compile(r"^[a-z0-9][a-z0-9_-]{0,39}$")
PROMPT_PATH = re.compile(r"^prompts/[A-Za-z0-9_-]{1,60}\.md$")
MAX_PROMPT = 100_000
MODELS = ["inherit", "sonnet", "opus", "haiku"]
CSRF_HEADER = "x-flow-editor"
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
    mode: Literal["full", "step", "until", "agent"] = "full"
    target: str | None = Field(default=None, max_length=40)
    instruction: str = Field(default="", max_length=20_000)
    # where inputs came from ("Fill from a run"): input -> {flow, run, agent, output}
    handoff: dict[str, HandoffRef] = Field(default_factory=dict, max_length=50)


class OpenRunBody(BaseModel):
    input: str = Field(pattern=r"^[A-Za-z0-9_-]{1,64}$")


class AskBody(BaseModel):
    message: str = Field(min_length=1, max_length=20_000)
    agent: str | None = Field(default=None, max_length=64)


class ContinueBody(BaseModel):
    how: Literal["step", "full"] = "step"


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
        "version": 1, "name": name, "title": title,
        "start": "first",
        "agents": {"first": {"description": "First step of the flow.",
                             "prompt": "Describe here what this agent does.",
                             "tools": []}},
        "gates": {"end": {"kind": "human", "label": "Human review"}},
        "edges": [{"from": "first", "to": "end", "when": "finished"}],
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
    from .. import workspaces
    own = [] if workspaces.current().studio else ["workspace"]
    return [*own, "local", *LOADED.names, PUBLIC]


def _location_dir(base: Path, location: str) -> Path:
    from .. import workspaces
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
    from .. import workspaces
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


# --- application --------------------------------------------------------------------------------
def create_app(base: Path | None = None, allowed_hosts: list[str] | None = None) -> FastAPI:
    base = (base or flows_dir()).resolve()

    def fb() -> Path:
        """The flows folder of the current workspace (the studio's: `base`)."""
        ws = workspaces.current()
        return base if ws.studio else ws.flows_dir
    app = FastAPI(title="Flow editor", docs_url=None, redoc_url=None, openapi_url=None)
    app.add_middleware(TrustedHostMiddleware,
                       allowed_hosts=allowed_hosts or ["localhost", "127.0.0.1"])

    def csrf(request: Request) -> None:
        if request.headers.get(CSRF_HEADER) != "1":
            raise HTTPException(403, "request without the X-Flow-Editor header")

    @app.middleware("http")
    async def workspace_of_request(request: Request, call_next):  # type: ignore[no-untyped-def]
        """The workspace and environment chosen in the editor (cookies afs_ws / afs_env): every
        flow, agent, connector and run of this request belongs to them."""
        ws = workspaces.get_or_none(request.cookies.get("afs_ws") or workspaces.STUDIO)
        token = workspaces.set_current(ws or workspaces.STUDIO_WS, request.cookies.get("afs_env"))
        try:
            return await call_next(request)
        finally:
            workspaces.reset_current(token)

    @app.middleware("http")
    async def headers(request: Request, call_next):  # type: ignore[no-untyped-def]
        resp = await call_next(request)
        resp.headers["Content-Security-Policy"] = (
            "default-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; "
            "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'")
        resp.headers["X-Content-Type-Options"] = "nosniff"
        resp.headers["Referrer-Policy"] = "no-referrer"
        resp.headers["Cache-Control"] = "no-store"
        return resp

    @app.get("/api/catalog")
    def catalog() -> dict[str, Any]:
        return {
            "tools": [{"id": t.id, "server": t.server, "name": t.name,
                       "description": t.description, "writes_local_only": t.writes_local_only,
                       "kind": t.kind}
                      for t in BUILTIN_TOOLS.values()],
            "servers": sorted(BUILTIN_SERVERS),
            "contracts": {k: v.json_schema() for k, v in CONTRACTS.items()},
            "models": MODELS,
            "mock": settings.mock,
            "connections": _conn_summary(),
            # where flows, library agents and connectors can be kept: this machine or an extension
            "locations": _locations(),
            "workspace": _ws_summary(),
        }

    def _conns() -> dict[str, Connection]:
        try:
            return conn_store.load()
        except conn_store.ConnectorConfigError as e:
            raise HTTPException(422, str(e)) from e

    def _conn_summary() -> dict[str, Any]:
        try:
            conns = conn_store.load()
        except conn_store.ConnectorConfigError:
            return {}
        return {n: {"type": c.type, "environment": c.environment, "description": c.description,
                    "access": c.access, "has_instructions": bool(c.instructions.strip()),
                    "actions_only": c.actions_only,  # what an agent with it gets (agent panel)
                    "actions": {k: {"access": x.access, "description": x.description,
                                    "params": list(x.params)} for k, x in c.actions.items()}}
                for n, c in conns.items()}

    def _conn_name(name: str) -> str:
        if not CONN_SLUG.fullmatch(name):
            raise HTTPException(400, "invalid connector name")
        return name

    # --- connectors ---------------------------------------------------------------------------
    def _redirect_uri(request: Request) -> str:
        # the origin the editor was opened at (the Host is already limited to local hosts)
        return str(request.base_url).rstrip("/") + OAUTH_CALLBACK

    @app.get("/api/connections")
    def connections(request: Request) -> dict[str, Any]:
        conns = _conns()
        return {**types_meta(), "file": str(conn_store.connections_file()),
                "oauth_redirect_uri": _redirect_uri(request),
                "locations": _locations(),
                "connections": {n: c.model_dump(mode="json") | {
                    "location": conn_store.location(n),
                    "actions_yaml": _actions_yaml(c),
                    "web_session": ({"profile": conn_web.has_profile(n),
                                     "signing_in": n in signing_in} if c.type == "website" else None),
                    "secret": conn_store.secret_status(n, c),
                    "oauth": conn_store.oauth_status(n, c)} for n, c in conns.items()}}

    # --- website connectors: the person signs in in a browser window -----------------------------
    signing_in: dict[str, asyncio.Task[Any]] = {}

    def _website(name: str) -> Connection:
        c = _conns().get(_conn_name(name))
        if c is None:
            raise HTTPException(404, "connector does not exist")
        if c.type != "website":
            raise HTTPException(400, "not a website connector")
        return c

    @app.post("/api/connections/{name}/browser/sign-in", dependencies=[Depends(csrf)])
    async def browser_sign_in(name: str) -> dict[str, Any]:
        """Opens a visible browser window on the site's login page; returns at once. The person
        signs in there and closes the window; the session stays on this machine."""
        c = _website(name)
        if name in signing_in:
            return {"signing_in": True}
        try:
            conn_web._playwright()
        except conn_web.WebError as e:
            raise HTTPException(400, str(e)) from e

        async def run() -> None:
            try:
                await conn_web.sign_in(name, c)
            finally:
                signing_in.pop(name, None)

        signing_in[name] = asyncio.create_task(run())
        return {"signing_in": True}

    @app.delete("/api/connections/{name}/browser", dependencies=[Depends(csrf)])
    def browser_sign_out(name: str) -> dict[str, Any]:
        _website(name)
        conn_web.forget_session(name)
        return {"profile": False}

    # --- test one action from the Connectors window (read actions only) ----------------------
    @app.post("/api/connections/{name}/actions/{action}/test", dependencies=[Depends(csrf)])
    async def test_action(name: str, action: str, body: ActionTestBody) -> dict[str, Any]:
        """Runs a read action now (a website one in a visible window) and returns what an agent
        would get. Write actions only run in a flow, with the autonomy and approvals."""
        c = _conns().get(_conn_name(name))
        if c is None:
            raise HTTPException(404, "connector does not exist")
        a = c.actions.get(action)
        if a is None:
            raise HTTPException(404, "the connector has no such action")
        if a.access != "read":
            raise HTTPException(400, "write actions run only in a flow (with approval)")
        if c.type == "website" and name in signing_in:
            raise HTTPException(409, "the sign-in window is still open")
        _audit_event("connector_action_tested", connector=name, action=action)  # names only
        try:
            out: dict[str, Any] = {"ok": True, "result": await conn_actions.run(
                name, action, body.params, visible=c.type == "website")}
        except (conn_actions.ActionError, conn_client.ConnectorError) as e:  # no secrets in them
            out = {"ok": False, "error": str(e), "blocked": getattr(e, "blocked", [])}
        # the last test of each action stays on this machine (runs/, outside git), so the person
        # can look at what it returned when tuning the agent that uses it
        tests = _read_json_file(_tests_file(name))
        tests[action] = {**out, "ts": time.time(), "params": body.params}
        _write_tests(name, tests)
        return out

    @app.get("/api/connections/{name}/tests")
    def action_tests(name: str) -> dict[str, Any]:
        """The last test of each action of a connector (what it returned, or its error)."""
        return {"tests": _read_json_file(_tests_file(_conn_name(name)))}

    @app.post("/api/connections/{name}/actions/{action}/allow-post", dependencies=[Depends(csrf)])
    def action_allow_post(name: str, action: str, body: AllowPostBody) -> dict[str, Any]:
        """The person allows (or not) POST requests in one read action of a website: some sites
        connect to their server or load data that way. Steps still never write by themselves."""
        conns = _conns()
        c = conns.get(_conn_name(name))
        if c is None or action not in c.actions:
            raise HTTPException(404, "the connector has no such action")
        if c.type != "website":
            raise HTTPException(400, "allow_post is only for website connectors")
        c.actions[action].allow_post = body.allow
        conn_store.save(conns)
        _audit_event("connector_action_allow_post", connector=name, action=action,
                     allow=body.allow)
        return {"allow_post": body.allow}

    # --- website connectors: record an action while the person clicks on the site ---------------
    recordings: dict[str, conn_rec.Recording] = {}

    @app.post("/api/connections/{name}/browser/record", dependencies=[Depends(csrf)])
    async def browser_record(name: str, body: RecordBody) -> dict[str, Any]:
        """Opens a visible window on the site and records the steps; returns at once. The editor
        reads them with GET while the person works; the result is a draft, never saved here."""
        c = _website(name)
        if name in signing_in:
            raise HTTPException(409, "the sign-in window is still open")
        old = recordings.get(name)
        if old and not old.done:
            return old.summary()
        try:
            conn_web._playwright()
        except conn_web.WebError as e:
            raise HTTPException(400, str(e)) from e
        rec = recordings[name] = conn_rec.Recording(c)
        asyncio.create_task(conn_rec.record(name, c, rec, body.banner))
        return rec.summary()

    @app.get("/api/connections/{name}/browser/record")
    def browser_recording(name: str, action: str = "recorded", fixed: str = "",
                          allow_post: bool = False) -> dict[str, Any]:
        """`fixed`: params (comma separated) that keep the text typed while recording;
        `allow_post`: the draft lets the page send POST requests."""
        _website(name)
        rec = recordings.get(name)
        if rec is None:
            raise HTTPException(404, "no recording for this connector")
        keep = {f for f in fixed.split(",") if f}
        return {**rec.summary(),
                "yaml": rec.yaml(action, keep, allow_post) if rec.done and rec.steps else ""}

    @app.delete("/api/connections/{name}/browser/record", dependencies=[Depends(csrf)])
    def browser_record_stop(name: str) -> dict[str, Any]:
        """Stops a recording (closes its window) or, when it has ended, forgets it."""
        _website(name)
        rec = recordings.get(name)
        if rec is None:
            return {"recording": False}
        if rec.done:
            recordings.pop(name, None)
        else:
            rec.stop.set()
        return {"recording": not rec.done}

    # --- sign-in on a page (OAuth 2 + PKCE) ------------------------------------------------------
    @app.post("/api/connections/{name}/oauth/start", dependencies=[Depends(csrf)])
    def oauth_start(name: str, request: Request) -> dict[str, Any]:
        c = _conns().get(_conn_name(name))
        if c is None:
            raise HTTPException(404, "connector does not exist")
        redirect = _redirect_uri(request)
        try:
            url = conn_oauth.start(name, c, redirect)
        except conn_oauth.OAuthError as e:
            raise HTTPException(400, str(e)) from e
        return {"url": url, "redirect_uri": redirect}

    @app.delete("/api/connections/{name}/oauth", dependencies=[Depends(csrf)])
    def oauth_sign_out(name: str) -> dict[str, Any]:
        c = _conns().get(_conn_name(name))
        if c is None:
            raise HTTPException(404, "connector does not exist")
        conn_store.delete_oauth_tokens(name)
        return {"oauth": conn_store.oauth_status(name, c)}

    @app.get(OAUTH_CALLBACK, response_class=HTMLResponse)
    async def oauth_callback(state: str = "", code: str = "", error: str = "",
                             error_description: str = "") -> HTMLResponse:
        if error or not code:
            name = conn_oauth.cancel(state)  # a refused sign-in cannot be reused
            detail = (error or "no code") + (f" — {error_description}" if error_description else "")
            return _oauth_page(False, f"The provider did not sign in {name or 'the connector'}: "
                                      f"{detail[:300]}")
        try:
            name = await conn_oauth.finish(state, code, _conns())
        except conn_oauth.OAuthError as e:
            return _oauth_page(False, str(e))
        except httpx.HTTPError as e:
            return _oauth_page(False, f"could not reach the provider: {type(e).__name__}")
        return _oauth_page(True, f"Connector {name} is signed in. You can close this tab and go "
                                 "back to the editor.")

    @app.put("/api/connections/{name}", dependencies=[Depends(csrf)])
    def save_connection(name: str, body: dict[str, Any]) -> dict[str, Any]:
        conns = _conns()
        old = conns.get(_conn_name(name))
        body.pop("secret", None)
        loc = body.pop("location", None)  # where to keep it: local, an extension or public
        if loc is not None and loc not in _locations():
            raise HTTPException(400, f"unknown location {loc!r}")
        if "actions_yaml" in body:  # the editor edits the actions as YAML text
            text = str(body.pop("actions_yaml") or "")
            try:
                parsed = yaml.safe_load(text) if text.strip() else {}
            except yaml.YAMLError as e:
                raise HTTPException(422, f"actions: invalid YAML: {e}") from e
            if not isinstance(parsed, dict):
                raise HTTPException(422, "actions: a mapping of action name → definition")
            body["actions"] = parsed
        if old:  # secret references do not come from the UI: keep them
            body["secrets"] = dict(old.secrets)
        else:
            body["secrets"] = {}
        try:
            conns[name] = Connection.model_validate(body)
        except ValidationError as e:
            raise HTTPException(422, _structured(e, {})) from e
        if (loc or conn_store.location(name)) == PUBLIC:  # git publishes it: no private words
            try:
                check_public(name + "\n" + conns[name].model_dump_json())
            except PrivateTermsError as e:
                raise HTTPException(409, {"private_terms": e.terms}) from e
        conn_store.save(conns, moves={name: loc} if loc else None)
        if (old.instructions if old else "") != conns[name].instructions:
            _audit_event("connector_instructions_changed", connector=name,
                         chars=len(conns[name].instructions))  # never the text itself
        new = conns[name]
        if (old.actions if old else {}) != new.actions or \
                (old.actions_only if old else False) != new.actions_only:
            _audit_event("connector_actions_changed", connector=name,
                         actions={k: a.access for k, a in new.actions.items()},
                         actions_only=new.actions_only)  # names and access only
        return {"saved": name, "secret": conn_store.secret_status(name, conns[name])}

    @app.put("/api/connections/{name}/secret", dependencies=[Depends(csrf)])
    def set_secret(name: str, body: SecretBody) -> dict[str, Any]:
        conns = _conns()
        c = conns.get(_conn_name(name))
        if c is None:
            raise HTTPException(404, "connector does not exist")
        field = c.secret_field
        if field is None:
            raise HTTPException(400, "this authentication method does not use a secret")
        if bool(body.value) == bool(body.env):
            raise HTTPException(400, "give 'value' (stored in the keyring) or 'env'")
        if body.value:
            conn_store.set_secret(name, field, body.value)
            ref = "keyring"
        else:
            ref = "${" + str(body.env) + "}"
        conns[name] = c.model_copy(update={"secrets": {**c.secrets, field: ref}})
        conn_store.save(conns)
        return {"secret": conn_store.secret_status(name, conns[name])}

    @app.delete("/api/connections/{name}", dependencies=[Depends(csrf)])
    def delete_connection(name: str) -> dict[str, Any]:
        conns = _conns()
        c = conns.pop(_conn_name(name), None)
        if c is None:
            raise HTTPException(404, "connector does not exist")
        conn_store.delete_secrets(name, c)
        conn_store.save(conns)
        _tests_file(name).unlink(missing_ok=True)  # its saved test results go with it
        return {"deleted": name}

    @app.post("/api/connections/{name}/test", dependencies=[Depends(csrf)])
    async def test_connection(name: str) -> dict[str, Any]:
        return await conn_client.test(_conn_name(name), _conns())

    @app.get("/api/flows")
    def list_all() -> list[dict[str, Any]]:
        out = []
        seen: set[str] = set()
        files = [(loc, f) for loc, d in flow_locations(fb()) for f in sorted(d.glob("*/flow.yaml"))]
        for loc, f in files:
            name = f.parent.name
            if not FLOW_NAME.fullmatch(name) or name in seen:
                continue
            seen.add(name)
            # where it lives, and whether git would publish it (only flows/ in this repository)
            public = loc == "local" and _in_public_repo(f)
            item: dict[str, Any] = {"name": name, "title": name, "valid": False,
                                    "location": PUBLIC if public else loc, "public": public}
            try:
                raw = yaml.safe_load(f.read_text(encoding="utf-8")) or {}
                item["title"] = str(raw.get("title") or name)
                spec = FlowSpec.model_validate(raw)
                item |= {"valid": True, "agents": len(spec.agents), "gates": len(spec.gates)}
            except (yaml.YAMLError, ValidationError, AttributeError) as e:
                item["error"] = str(e).splitlines()[0][:200]
            out.append(item)
        return out

    @app.get("/api/flows/{name}")
    def get_flow(name: str) -> dict[str, Any]:
        d = _flow_dir(fb(), name)
        try:
            raw = yaml.safe_load((d / "flow.yaml").read_text(encoding="utf-8")) or {}
        except yaml.YAMLError as e:
            raise HTTPException(422, f"flow.yaml is not valid YAML: {e}") from e
        if not isinstance(raw, dict):
            raise HTTPException(422, "flow.yaml must be a mapping")
        prompts = _disk_prompts(d)
        place = _flow_place(fb(), name)
        return {"spec": raw, "prompts": prompts, "location": place, "public": place == PUBLIC,
                **_public(_validate(raw, prompts))}

    @app.post("/api/flows/{name}/validate")
    def validate(name: str, body: FlowBody) -> dict[str, Any]:
        d = _flow_dir(fb(), name)
        _check_prompts(body.prompts)
        return _public(_validate(body.spec, {**_disk_prompts(d), **body.prompts}))

    @app.put("/api/flows/{name}", dependencies=[Depends(csrf)])
    def save(name: str, body: FlowBody) -> JSONResponse:
        d = _flow_dir(fb(), name)
        _check_prompts(body.prompts)
        if body.spec.get("name") != name:
            raise HTTPException(400, "the flow name cannot change (duplicate it to rename)")
        # a flow that git publishes never names the private terms (move it to this machine first)
        if _in_public_repo(d / "flow.yaml"):
            found = terms_in(json.dumps(body.spec, ensure_ascii=False) + "\n"
                             + "\n".join(body.prompts.values()))
            if found:
                return JSONResponse({"saved": False, "private_terms": found}, status_code=409)
        disk = _disk_prompts(d)
        result = _validate(body.spec, {**disk, **body.prompts})
        if not result["ok"]:
            # a literal secret is never written, even when the person asks to save anyway
            secret = [e for e in result["errors"] if SECRET_ERROR.search(e["msg"])]
            if not body.force or secret:
                return JSONResponse(_public(result) | {"saved": False,
                                                       "blocked_by_secret": bool(secret)},
                                    status_code=422)
        for k, v in body.prompts.items():
            if disk.get(k) != v:
                _atomic_write(d / k, v)
        flow = d / "flow.yaml"
        shutil.copyfile(flow, d / "flow.yaml.bak")
        if result["ok"]:
            _atomic_write(flow, dump_flow(result["spec"]))
            return JSONResponse(_public(result) | {"saved": True,
                                                   "spec": spec_to_data(result["spec"])})
        _atomic_write(flow, dump_raw(body.spec, name))  # with problems: as it is
        return JSONResponse(_public(result) | {"saved": True, "with_problems": True,
                                               "spec": body.spec})

    @app.post("/api/assist", dependencies=[Depends(csrf)])
    def assist(body: AssistBody) -> StreamingResponse:
        """Assistant chat: returns NDJSON events; never saves (the editor applies the draft)."""
        d = _flow_dir(fb(), body.flow)
        _check_prompts(body.prompts)
        prompts = {**_disk_prompts(d), **body.prompts}
        cmd = None
        if body.command:
            try:
                cmd = assist_commands.get(body.command)
            except assist_commands.CommandError as e:
                raise HTTPException(500, str(e)) from e
            if cmd is None:
                raise HTTPException(404, "saved command does not exist")

        async def events():  # type: ignore[no-untyped-def]
            async for ev in assistant.chat(body.message, body.flow, body.spec, prompts,
                                           body.session_id, lang=body.lang, command=cmd):
                yield assistant.encode(ev)

        return StreamingResponse(events(), media_type="application/x-ndjson")

    def _command(cid: str | None):  # type: ignore[no-untyped-def]
        if not cid:
            return None
        try:
            cmd = assist_commands.get(cid)
        except assist_commands.CommandError as e:
            raise HTTPException(500, str(e)) from e
        if cmd is None:
            raise HTTPException(404, "saved command does not exist")
        return cmd

    @app.post("/api/assist/workspace", dependencies=[Depends(csrf)])
    def assist_workspace(body: WsAssistBody) -> StreamingResponse:
        """Workspace assistant (Board): NDJSON events; it only proposes (the editor applies)."""
        cmd = _command(body.command)
        ws, env = workspaces.current(), workspaces.current_env()

        async def events():  # type: ignore[no-untyped-def]
            with workspaces.using(ws, env):  # the stream runs after the request returned
                async for ev in ws_assistant.chat(body.message, body.session_id, lang=body.lang,
                                                  command=cmd):
                    yield assistant.encode(ev)

        return StreamingResponse(events(), media_type="application/x-ndjson")

    # --- runs -----------------------------------------------------------------------------
    runner = Runner(base)
    app.state.runner = runner

    def _run_call(fn, *a, **kw):  # type: ignore[no-untyped-def]
        try:
            return fn(*a, **kw).summary()
        except RunError as e:
            raise HTTPException(e.status, e.detail) from e
        except FlowError as e:
            raise HTTPException(400, str(e)) from e

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

    @app.get("/api/flows/{flow}/runs")
    def past_runs(flow: str) -> dict[str, Any]:
        """The saved runs of a flow (runs/), newest first."""
        _flow_dir(fb(), flow)
        return {"runs": runner.past_runs(flow)}

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

    @app.post("/api/runs/{run_id}/decision", dependencies=[Depends(csrf)])
    async def decide(run_id: str, body: DecisionBody) -> dict[str, Any]:
        return _run_call(runner.decide, run_id, body.decision, body.comment)

    @app.post("/api/runs/{run_id}/approvals/{approval_id}", dependencies=[Depends(csrf)])
    async def approve_write(run_id: str, approval_id: str, body: ApprovalBody) -> dict[str, Any]:
        return _run_call(runner.approve, run_id, approval_id, body.approve, body.comment)

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

    @app.get("/api/templates")
    def templates() -> list[dict[str, Any]]:
        try:
            return templates_dict()
        except TemplateError as e:
            raise HTTPException(500, str(e)) from e

    @app.post("/api/inputs/visible")
    def visible_inputs(body: dict[str, Any]) -> dict[str, Any]:
        """Run form: which flow inputs are hidden by their show_if for the values typed so far
        (the definitions come from the editor, saved or not). Nothing is stored."""
        defs = body.get("inputs") if isinstance(body.get("inputs"), dict) else {}
        given = body.get("values") if isinstance(body.get("values"), dict) else {}
        shown: dict[str, Any] = {}
        hidden = []
        for n, raw in defs.items():
            t = IOType.model_construct(show_if=raw.get("show_if")) if isinstance(raw, dict) \
                else IOType()
            if input_visible(t, shown):
                if n in given:
                    shown[n] = given[n]
            else:
                hidden.append(n)
        return {"hidden": hidden}

    # --- assistant conversations (history per flow, runs/chats/) ---------------------------
    def _chat_call(fn, *a):  # type: ignore[no-untyped-def]
        try:
            return fn(*a)
        except FileNotFoundError as e:
            raise HTTPException(404, "conversation does not exist") from e
        except assist_chats.ChatError as e:
            raise HTTPException(422, str(e)) from e

    @app.get("/api/chats/{flow}")
    def list_chats(flow: str) -> list[dict[str, Any]]:
        return _chat_call(assist_chats.list_chats, flow)

    @app.get("/api/chats/{flow}/{chat_id}")
    def get_chat(flow: str, chat_id: str) -> dict[str, Any]:
        return _chat_call(assist_chats.get, flow, chat_id)

    @app.put("/api/chats/{flow}/{chat_id}", dependencies=[Depends(csrf)])
    def save_chat(flow: str, chat_id: str, body: dict[str, Any]) -> dict[str, Any]:
        return _chat_call(assist_chats.save, flow, chat_id, body)

    @app.delete("/api/chats/{flow}/{chat_id}", dependencies=[Depends(csrf)])
    def delete_chat(flow: str, chat_id: str) -> dict[str, Any]:
        _chat_call(assist_chats.delete, flow, chat_id)
        return {"deleted": chat_id}

    @app.get("/api/commands")
    def list_commands() -> list[dict[str, Any]]:
        try:
            return [c.model_dump() for c in assist_commands.load()]
        except assist_commands.CommandError as e:
            raise HTTPException(500, str(e)) from e

    @app.put("/api/commands/{command_id}", dependencies=[Depends(csrf)])
    def save_command(command_id: str, body: dict[str, Any]) -> dict[str, Any]:
        """Creates or replaces a saved command ('new' as the id creates one)."""
        if command_id != "new" and not assist_commands.COMMAND_ID.fullmatch(command_id):
            raise HTTPException(400, "invalid command id")
        try:
            c = assist_commands.save(body | {"id": None if command_id == "new" else command_id})
        except assist_commands.CommandError as e:
            raise HTTPException(422, str(e)) from e
        return c.model_dump()

    @app.delete("/api/commands/{command_id}", dependencies=[Depends(csrf)])
    def delete_command(command_id: str) -> dict[str, Any]:
        try:
            assist_commands.delete(command_id)
        except assist_commands.CommandError as e:
            raise HTTPException(404, str(e)) from e
        return {"deleted": command_id}

    @app.get("/api/agents")
    def library() -> list[dict[str, Any]]:
        try:
            return agents_dict()
        except LibraryError as e:
            raise HTTPException(500, str(e)) from e

    @app.post("/api/agents", dependencies=[Depends(csrf)], status_code=201)
    def save_to_library(body: LibraryBody) -> dict[str, Any]:
        """Saves an agent of a flow to the local library (agents/, outside git)."""
        agent = body.agent
        if body.agent_yaml is not None:  # the library window edits the definition as YAML
            try:
                agent = yaml.safe_load(body.agent_yaml) or {}
            except yaml.YAMLError as e:
                raise HTTPException(422, f"definition: invalid YAML: {e}") from e
            if not isinstance(agent, dict):
                raise HTTPException(422, "definition: a mapping (description, tools, inputs…)")
        try:
            a, notes = save_local(body.id, body.title, agent or {}, body.prompt, body.overwrite,
                                  location=body.location)
        except PrivateTermsError as e:
            raise HTTPException(409, {"private_terms": e.terms}) from e
        except LibraryError as e:
            raise HTTPException(409 if "already exists" in str(e) else 422, str(e)) from e
        return {"id": a.id, "hash": a.hash, "source": a.source, "notes": notes}

    @app.delete("/api/agents/{agent_id}", dependencies=[Depends(csrf)])
    def delete_library_agent(agent_id: str) -> dict[str, Any]:
        try:
            return {"deleted": agent_id, "source": delete_agent(agent_id)}
        except LibraryError as e:
            raise HTTPException(404, str(e)) from e

    @app.post("/api/agents/{agent_id}/move", dependencies=[Depends(csrf)])
    def move_library_agent(agent_id: str, body: dict[str, Any]) -> dict[str, Any]:
        try:
            a = move_agent(agent_id, str(body.get("location") or ""))
        except PrivateTermsError as e:
            raise HTTPException(409, {"private_terms": e.terms}) from e
        except LibraryError as e:
            raise HTTPException(422, str(e)) from e
        return {"id": a.id, "source": a.source}

    @app.post("/api/flows", dependencies=[Depends(csrf)], status_code=201)
    def create(body: NewFlow) -> dict[str, Any]:
        if not FLOW_NAME.fullmatch(body.name):
            raise HTTPException(400, "invalid flow name")
        if find_flow(body.name, fb()) or (_location_dir(fb(), body.location) / body.name).exists():
            raise HTTPException(409, f"a flow {body.name!r} already exists")
        d = _location_dir(fb(), body.location) / body.name  # local (flows/) or an extension
        if body.template and body.copy_from:
            raise HTTPException(400, "give either 'template' or 'copy_from'")
        if body.template:
            try:
                tspec, prompts = render_template(body.template, body.name, body.title, body.params)
            except TemplateError as e:
                raise HTTPException(422, str(e)) from e
            for rel, text in prompts.items():
                if not PROMPT_PATH.fullmatch(rel):
                    raise HTTPException(500, f"template prompt with an invalid path: {rel}")
                _atomic_write(d / rel, text)
            _atomic_write(d / "flow.yaml", dump_flow(tspec))
            _publish_new(d, body.location)
            return {"name": body.name}
        if body.copy_from:
            src = _flow_dir(fb(), body.copy_from)
            shutil.copytree(src, d, ignore=shutil.ignore_patterns("*.bak", "*.tmp"))
            raw = yaml.safe_load((d / "flow.yaml").read_text(encoding="utf-8"))
            raw |= {"name": body.name, "title": body.title}
        else:
            raw = _blank(body.name, body.title)
        try:
            spec = FlowSpec.model_validate(raw)
        except ValidationError as e:
            shutil.rmtree(d, ignore_errors=True)
            raise HTTPException(422, _structured(e, raw)) from e
        _atomic_write(d / "flow.yaml", dump_flow(spec))
        _publish_new(d, body.location)
        return {"name": body.name}

    def _publish_new(d: Path, location: str) -> None:
        if location != PUBLIC:
            return
        try:
            _publish(d)
        except HTTPException:
            shutil.rmtree(d, ignore_errors=True)  # not created: nothing half-public is left
            raise

    def _publish(d: Path) -> None:
        """Makes a flow in flows/ public (the .gitignore exception) after the private terms check;
        409 with the terms, or 400 without git."""
        found = terms_in(_flow_text(d))
        if found:
            raise HTTPException(409, {"private_terms": found})
        if not set_flow_public(base, d.name, True):
            raise HTTPException(400, "a public flow needs this folder to be in a git repository")

    @app.post("/api/flows/{name}/move", dependencies=[Depends(csrf)])
    def move_flow(name: str, body: dict[str, Any]) -> dict[str, Any]:
        """Moves a flow between this machine, an extension and public (git)."""
        to = str(body.get("location") or "")
        if not workspaces.current().studio:
            raise HTTPException(400, "a workspace keeps its flows in its own folder (copy it to "
                                     "another workspace instead)")
        d, now = _flow_dir(fb(), name), _flow_place(fb(), name)
        target_dir = _location_dir(fb(), to)  # also checks the location
        if to == now:
            return {"location": now}
        if to == PUBLIC and d.parent.resolve() == base.resolve():  # in flows/: the exception only
            _publish(d)
        if now == PUBLIC:
            set_flow_public(base, name, False)
        if d.parent.resolve() != target_dir.resolve():
            if (target_dir / name).exists():
                raise HTTPException(409, f"{target_dir / name} already exists")
            target_dir.mkdir(parents=True, exist_ok=True)
            shutil.move(str(d), target_dir / name)
            if to == PUBLIC:
                try:
                    _publish(target_dir / name)
                except HTTPException:
                    shutil.move(str(target_dir / name), d)  # back where it was
                    raise
        return {"location": _flow_place(fb(), name)}

    @app.delete("/api/flows/{name}", dependencies=[Depends(csrf)])
    def delete(name: str) -> dict[str, Any]:
        d = _flow_dir(fb(), name)
        if _flow_place(fb(), name) == PUBLIC:
            set_flow_public(base, name, False)
        trash = d.parent / ".trash"  # next to the flow (flows/ or the extension's flows/)
        trash.mkdir(exist_ok=True)
        dest = trash / f"{name}-{time.strftime('%Y%m%d-%H%M%S')}"
        shutil.move(str(d), dest)  # recoverable: nothing is deleted for good
        return {"trashed": str(dest.relative_to(trash.parent))}

    # --- workspaces ---------------------------------------------------------------------------
    def _ws_error(e: Exception) -> HTTPException:
        """422 with the English text and its code (the editor shows it in the UI language)."""
        m = reason(e)
        return HTTPException(422, {"msg": str(m), **(to_json(m) or {})})

    def _ws_summary() -> dict[str, Any]:
        ws = workspaces.current()
        return {"name": ws.name, "where": ws.where, "environment": workspaces.current_env(),
                "environments": ws.env_names(), "kind": workspaces.env_kind(),
                "title": (ws.spec.title if ws.spec else "") or ws.name,
                "vars": sorted(ws.known_vars()),
                "roots": sorted(ws.spec.roots) if ws.spec else []}

    def _ws_detail(ws: workspaces.Workspace) -> dict[str, Any]:
        out: dict[str, Any] = {"name": ws.name, "where": ws.where,
                               "folder": str(ws.folder) if ws.folder else None,
                               "home": str(ws.home) if ws.folder else None}
        if ws.spec is not None:
            out["spec"] = ws.spec.model_dump(mode="json")
            out["roots"] = ws.resolve(None)[1]
        return out

    @app.get("/api/workspaces")
    def list_workspaces() -> dict[str, Any]:
        return {"workspaces": workspaces.list_all(), "current": _ws_summary()}

    @app.post("/api/workspaces", dependencies=[Depends(csrf)], status_code=201)
    def create_workspace(body: NewWorkspace) -> dict[str, Any]:
        try:
            ws = workspaces.create(body.name, body.where,
                                   Path(body.repo) if body.repo else None,
                                   title=body.title, description=body.description)
        except (MsgError, ValidationError, OSError) as e:
            raise _ws_error(e) from e
        _audit_event("workspace_create", workspace=ws.name, where=ws.where)
        return _ws_detail(ws)

    @app.post("/api/workspaces/open", dependencies=[Depends(csrf)])
    def open_workspace(body: dict[str, Any]) -> dict[str, Any]:
        path = str(body.get("path") or "").strip()
        if not path:
            raise HTTPException(400, "give the folder of the workspace (or of its repository)")
        try:
            ws = workspaces.open_folder(Path(path))
        except (MsgError, ValidationError) as e:
            raise _ws_error(e) from e
        _audit_event("workspace_open", workspace=ws.name)
        return _ws_detail(ws)

    @app.get("/api/folders")
    def folders(path: str = "") -> dict[str, Any]:
        """The subfolders of a folder on this machine (names only), to choose a repository or a
        workspace in the editor; marks git repositories and folders that hold a workspace."""
        import os
        import string
        here = Path(path).expanduser() if path.strip() else Path.home()
        def is_dir(e: os.DirEntry[str]) -> bool:
            try:
                return e.is_dir(follow_symlinks=False)
            except OSError:
                return False

        try:
            here = here.resolve()
            entries = sorted((e for e in os.scandir(here) if is_dir(e)
                              and not e.name.startswith((".", "$"))), key=lambda e: e.name.lower())
        except OSError as e:
            raise HTTPException(400, f"cannot read the folder {path!r}: {type(e).__name__}") from e
        def has(*parts: Path) -> bool:  # a protected system folder may refuse even a look
            try:
                return any(x.exists() for x in parts)
            except OSError:
                return False

        out = []
        for e in entries[:500]:
            d = Path(e.path)
            out.append({"name": e.name, "path": str(d), "repo": has(d / ".git"),
                        "workspace": has(d / workspaces.REPO_DIR / workspaces.WS_FILE,
                                         d / workspaces.WS_FILE)})
        roots = ([f"{c}:\\" for c in string.ascii_uppercase if os.path.exists(f"{c}:\\")]
                 if os.name == "nt" else ["/"])
        return {"path": str(here), "parent": str(here.parent) if here.parent != here else None,
                "home": str(Path.home()), "roots": roots, "folders": out,
                "repo": has(here / ".git"),
                "workspace": has(here / workspaces.REPO_DIR / workspaces.WS_FILE,
                                 here / workspaces.WS_FILE)}

    # --- workspace context --------------------------------------------------------------------
    @app.get("/api/context")
    def get_context() -> dict[str, Any]:
        """The context of the open workspace: its file, sections and the state of each root."""
        ws = workspaces.current()
        if ws.studio:
            return {"studio": True, "sections": {}, "roots": {}}
        return {"studio": False, "file": str(ws.folder / context.CONTEXT_FILE) if ws.folder else None,
                "sections": context.sections(ws), "roots": context.roots_state(ws),
                "chars": len(context.context_text(ws, limit=10**9)), "limit": context.MAX_PROMPT,
                "drafts": runner.drafts()}

    # --- learning from past Claude Code sessions (see claude_sessions.py) -----------------------
    def _learn_body(body: dict[str, Any]) -> tuple[str, list[str], list[str]]:
        root, sessions, memory = body.get("root"), body.get("sessions") or [], body.get("memory") or []
        if not isinstance(root, str) or not all(isinstance(x, list) for x in (sessions, memory)) \
                or not all(isinstance(x, str) for x in [*sessions, *memory]):
            raise HTTPException(400, "root: a root name; sessions, memory: lists of names")
        return root, sessions[:200], memory[:200]

    @app.get("/api/context/sources/{root}")
    def context_sources(root: str) -> dict[str, Any]:
        """The Claude Code sessions and memory notes about a root's folder."""
        try:
            return runner.sources(root)
        except RunError as e:
            raise HTTPException(e.status, e.detail) from e

    @app.post("/api/context/estimate", dependencies=[Depends(csrf)])
    def context_estimate(body: dict[str, Any]) -> dict[str, int]:
        try:
            return runner.estimate(*_learn_body(body))
        except RunError as e:
            raise HTTPException(e.status, e.detail) from e

    @app.post("/api/context/learn", dependencies=[Depends(csrf)])
    async def context_learn(body: dict[str, Any]) -> dict[str, Any]:
        return _run_call(runner.learn_context, *_learn_body(body))

    @app.post("/api/context/draft/{root}", dependencies=[Depends(csrf)])
    def context_apply_draft(root: str, body: dict[str, Any]) -> dict[str, Any]:
        """The person saves the reviewed history of a root (maybe edited)."""
        try:
            runner.apply_draft(root, str(body.get("section") or ""))
        except RunError as e:
            raise HTTPException(e.status, e.detail) from e
        except MsgError as e:
            raise _ws_error(e) from e
        return get_context()

    @app.delete("/api/context/draft/{root}", dependencies=[Depends(csrf)])
    def context_discard_draft(root: str) -> dict[str, Any]:
        runner.discard_draft(root)
        return get_context()

    @app.put("/api/context/{name}", dependencies=[Depends(csrf)])
    def set_context(name: str, body: dict[str, Any]) -> dict[str, Any]:
        """The person writes the notes, or corrects the section of a root."""
        ws = workspaces.current()
        try:
            context.set_section(ws, name, str(body.get("text") or ""),
                                commit=(context.state(ws).get(name) or {}).get("commit")
                                if not ws.studio and name != context.NOTES else None,
                                by="person")
        except MsgError as e:
            raise _ws_error(e) from e
        _audit_event("workspace_context_edit", workspace=ws.name, section=name)
        return get_context()

    @app.put("/api/uploads", dependencies=[Depends(csrf)], status_code=201)
    async def upload(request: Request) -> dict[str, Any]:
        """Stages a large file for a run (the body is the file): {upload, size}."""
        declared = request.headers.get("content-length", "")
        if declared.isdigit() and int(declared) > uploads.MAX_FILE:
            raise HTTPException(413, f"file larger than {uploads.MAX_FILE // (1024 * 1024)} MB")
        try:
            return await uploads.receive(request.stream())
        except uploads.UploadError as e:
            raise HTTPException(413 if "larger" in str(e) else 422, str(e)) from e

    @app.post("/api/context/update", dependencies=[Depends(csrf)])
    async def update_context(body: dict[str, Any]) -> dict[str, Any]:
        mode = body.get("mode") or "update"
        if mode not in ("explore", "update"):
            raise HTTPException(400, "mode: explore or update")
        roots = body.get("roots") or None
        if roots is not None and not (isinstance(roots, list)
                                      and all(isinstance(r, str) for r in roots)):
            raise HTTPException(400, "roots: a list of root names")
        return _run_call(runner.update_context, mode, roots, body.get("after") or None)

    @app.get("/api/board")
    def board() -> dict[str, Any]:
        """The open workspace as a board: each flow with its inputs (and where they usually come
        from), outputs, subflows and last run, plus the hand-offs recorded by its runs."""
        out = []
        seen: set[str] = set()
        for _, d in flow_locations(fb()):
            for f in sorted(d.glob("*/flow.yaml")):
                name = f.parent.name
                if not FLOW_NAME.fullmatch(name) or name in seen:
                    continue
                seen.add(name)
                item: dict[str, Any] = {"name": name, "title": name, "valid": False, "inputs": {},
                                        "outputs": [], "subflows": [], "handoffs": []}
                try:
                    raw = yaml.safe_load(f.read_text(encoding="utf-8")) or {}
                    item["title"] = str(raw.get("title") or name)
                    item["description"] = str(raw.get("description") or "")
                    spec = FlowSpec.model_validate(workspaces.substitute(raw)[0])
                    item |= {"valid": True, "execution": spec.execution}
                    item["inputs"] = {k: {"type": t.type, "label": t.label or k,
                                          **({"source": t.source.model_dump()} if t.source
                                             else {})} for k, t in spec.inputs.items()}
                    item["outputs"] = [{"agent": an, "name": on, "type": o.type}
                                       for an, a in spec.agents.items()
                                       for on, o in a.outputs.items()]
                    item["subflows"] = sorted({a.subflow.flow for a in spec.agents.values()
                                               if a.subflow})
                except (yaml.YAMLError, ValidationError, AttributeError, ValueError):
                    pass
                runs = runner.past_runs(name, limit=10)
                if runs:
                    item["last_run"] = {k: runs[0][k] for k in ("input", "status", "started")}
                hand: set[tuple[str, str, str]] = set()
                for r in runs:
                    try:
                        for inp, ref in io_tool.handoff(r["input"]).items():
                            hand.add((str(ref.get("flow")), str(ref.get("output")), inp))
                    except (ValueError, AttributeError):
                        continue
                item["handoffs"] = [{"flow": a, "output": b, "input": c}
                                    for a, b, c in sorted(hand)]
                out.append(item)
        return {"workspace": _ws_summary(), "flows": out}

    @app.get("/api/workspaces/{name}/flows")
    def workspace_flows(name: str) -> list[dict[str, Any]]:
        """The flows of another workspace (to choose where a copied agent goes)."""
        try:
            ws = workspaces.get(name)
        except MsgError as e:
            raise HTTPException(404, str(e)) from e
        with workspaces.using(ws):
            base_dir = fb()
            return [{"name": f.parent.name, "title": f.parent.name}
                    for _, d in flow_locations(base_dir) for f in sorted(d.glob("*/flow.yaml"))
                    if FLOW_NAME.fullmatch(f.parent.name)]

    @app.get("/api/workspaces/{name}")
    def get_workspace(name: str) -> dict[str, Any]:
        try:
            return _ws_detail(workspaces.get(name))
        except MsgError as e:
            raise HTTPException(404, str(e)) from e

    @app.put("/api/workspaces/{name}", dependencies=[Depends(csrf)])
    def update_workspace(name: str, body: dict[str, Any]) -> dict[str, Any]:
        """Title, description, roots, variables and environments (the name does not change)."""
        try:
            ws = workspaces.get(name)
        except MsgError as e:
            raise HTTPException(404, str(e)) from e
        if ws.studio or ws.spec is None:
            raise HTTPException(400, "the studio's own workspace has no settings")
        data = {**ws.spec.model_dump(mode="json"), **body, "name": ws.name}
        before = {k: r.path for k, r in ws.spec.roots.items()}
        try:
            ws.spec = workspaces.WorkspaceSpec.model_validate(data)
        except (MsgError, ValidationError) as e:
            raise HTTPException(422, _structured(e, data) if isinstance(e, ValidationError)
                                else {"msg": str(e)}) from e
        workspaces.save(ws)
        # a root that only changed its name keeps its explored context
        renamed = context.follow_renames(ws, before)
        _audit_event("workspace_update", workspace=ws.name, fields=sorted(body))
        return _ws_detail(ws) | {"renamed_roots": renamed}

    def _two(src: str, to: str) -> tuple[workspaces.Workspace, workspaces.Workspace]:
        try:
            return workspaces.get(src), workspaces.get(to)
        except MsgError as e:
            raise HTTPException(404, str(e)) from e

    def _item(body: CopyBody) -> copying.Item:
        return copying.Item(body.kind, body.name, agent=body.agent, to_flow=body.to_flow,
                            vars=body.vars, environments=body.environments)

    @app.post("/api/workspaces/{src}/copy-plan", dependencies=[Depends(csrf)])
    def copy_plan(src: str, body: CopyBody) -> dict[str, Any]:
        """What a copy would do: the name in the target (clash, suggested name) and what the item
        uses that the target lacks, with a choice per dependency."""
        a, b = _two(src, body.to)
        try:
            return copying.copy_plan(a, b, _item(body), body.new_name, studio_flows=base)
        except MsgError as e:
            raise _ws_error(e) from e
        except (LibraryError, conn_store.ConnectorConfigError, OSError, yaml.YAMLError) as e:
            raise HTTPException(422, str(e)) from e

    @app.post("/api/workspaces/{src}/copy", dependencies=[Depends(csrf)])
    def copy_item(src: str, body: CopyBody) -> dict[str, Any]:
        a, b = _two(src, body.to)
        try:
            res = copying.copy(a, b, _item(body), new_name=body.new_name, replace=body.replace,
                               deps=dict(body.deps), same_secrets=body.same_secrets,
                               studio_flows=base)
        except MsgError as e:
            raise _ws_error(e) from e
        except (LibraryError, conn_store.ConnectorConfigError, OSError, yaml.YAMLError) as e:
            raise HTTPException(422, str(e)) from e
        _audit_event("workspace_copy", kind=body.kind, src=a.name, to=b.name,
                     copied=[f"{c['kind']}:{c['name']}>{c['as']}" for c in res.copied],
                     secrets=len(res.secrets))
        return res.to_json()

    @app.post("/api/workspaces/{src}/move", dependencies=[Depends(csrf)])
    def move_item(src: str, body: CopyBody) -> dict[str, Any]:
        """Copy to another workspace, then take the original out of this one."""
        a, b = _two(src, body.to)
        try:
            res = copying.move(a, b, _item(body), new_name=body.new_name, replace=body.replace,
                               deps=dict(body.deps), same_secrets=body.same_secrets,
                               studio_flows=base)
        except MsgError as e:
            raise _ws_error(e) from e
        except (LibraryError, conn_store.ConnectorConfigError, OSError, yaml.YAMLError) as e:
            raise HTTPException(422, str(e)) from e
        _audit_event("workspace_move", kind=body.kind, src=a.name, to=b.name,
                     moved=[f"{c['kind']}:{c['name']}>{c['as']}" for c in res.copied],
                     secrets=len(res.secrets))
        return res.to_json()

    @app.post("/api/workspaces/{src}/duplicate", dependencies=[Depends(csrf)], status_code=201)
    def duplicate_workspace(src: str, body: DuplicateBody) -> dict[str, Any]:
        try:
            ws = workspaces.get(src)
        except MsgError as e:
            raise HTTPException(404, str(e)) from e
        try:
            new, res = copying.duplicate(ws, body.name, body.where,
                                         Path(body.repo) if body.repo else None, title=body.title,
                                         same_secrets=body.same_secrets)
        except (MsgError, ValidationError) as e:
            raise _ws_error(e) from e
        except (conn_store.ConnectorConfigError, OSError) as e:
            raise HTTPException(422, str(e)) from e
        _audit_event("workspace_duplicate", src=ws.name, to=new.name, where=new.where,
                     secrets=len(res.secrets))
        return {**_ws_detail(new), **res.to_json()}

    @app.get("/api/handoff/{flow}")
    def handoff_outputs(flow: str, type: str | None = None) -> dict[str, Any]:
        """"Fill from a run": the finished runs of a flow of the open workspace with outputs that
        can feed an input of type `type`, newest first."""
        _flow_dir(fb(), flow)
        if type is not None and type not in IO_KINDS_ALL:
            raise HTTPException(400, f"unknown type {type!r}")
        return {"runs": runner.flow_outputs(flow, type)}

    @app.get("/api/handoff/{flow}/{run_input}/{agent}/{name}")
    def handoff_value(flow: str, run_input: str, agent: str, name: str) -> dict[str, Any]:
        _flow_dir(fb(), flow)
        if not (SLUG_RE.fullmatch(agent) and SLUG_RE.fullmatch(name)):
            raise HTTPException(400, "invalid agent or output name")
        try:
            value = runner.output_value(flow, run_input, agent, name)
        except ValueError as e:
            raise HTTPException(400, str(e)) from e
        except RunError as e:
            raise HTTPException(e.status, e.detail) from e
        return {"value": value, "from": {"flow": flow, "run": run_input, "agent": agent,
                                         "output": name}}

    @app.delete("/api/workspaces/{name}", dependencies=[Depends(csrf)])
    def forget_workspace(name: str) -> dict[str, Any]:
        """Forgets a workspace on this machine; its files stay where they are."""
        if name == workspaces.STUDIO:
            raise HTTPException(400, "the studio's own workspace cannot be forgotten")
        workspaces.unregister(name)
        _audit_event("workspace_forget", workspace=name)
        return {"forgotten": name}

    if (STATIC / "index.html").is_file():
        app.mount("/", StaticFiles(directory=STATIC, html=True), name="static")
    else:
        @app.get("/", response_class=HTMLResponse)
        def no_build() -> str:
            return ("<h1>Editor not built</h1><p>Run <code>npm install && npm run build</code>"
                    " in the <code>editor/</code> folder.</p>")

    return app


def main() -> None:
    p = argparse.ArgumentParser(prog="agent-flow-studio-editor")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8765)
    p.add_argument("--flows", type=Path, help="flows folder (default: FLOWS_DIR or flows/)")
    p.add_argument("--no-browser", action="store_true")
    a = p.parse_args()
    import uvicorn

    hosts = ["localhost", "127.0.0.1"] + ([a.host] if a.host not in ("127.0.0.1", "0.0.0.0") else [])
    base = (a.flows or flows_dir()).resolve()
    app = create_app(base, allowed_hosts=hosts)
    url = f"http://127.0.0.1:{a.port}/"
    print(f"Flow editor at {url}  (flows: {base})")
    if not a.no_browser:
        webbrowser.open(url)
    uvicorn.run(app, host=a.host, port=a.port, log_level="warning")


if __name__ == "__main__":
    main()

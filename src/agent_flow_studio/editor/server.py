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
import contextlib
import logging
import webbrowser
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.trustedhost import TrustedHostMiddleware

from .. import workspaces
from ..builder.catalog import BUILTIN_SERVERS, BUILTIN_TOOLS, CONTRACTS
from ..builder.compile import flows_dir
from ..config import settings
from ..providers import summary as provider_summary
from .api import connectors as connectors_routes
from .api import flows as flows_routes
from .api import providers as providers_routes
from .api import runs as runs_routes
from .api import workspaces as workspaces_routes
from .api.common import (
    CSRF_HEADER,
    MODELS,
    QUEUE_TICK,
    STATIC,
    Ctx,
    _locations,
)
from .api.connectors import _conn_summary
from .api.workspaces import _ws_summary
from .runner import (
    Runner,
)


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
        if "Content-Security-Policy" not in resp.headers:  # a run file keeps its stricter one
            # the workspace preview (a local web app) may be framed and checked: this machine only
            local = "http://localhost:* http://127.0.0.1:*"
            resp.headers["Content-Security-Policy"] = (
                "default-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; "
                f"connect-src 'self' {local}; frame-src {local}; frame-ancestors 'none'; "
                "base-uri 'none'; form-action 'none'")
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
            "models": MODELS + [f"{pv['name']}/{m}" for pv in provider_summary()
                                if not pv.get("error") for m in pv["models"]],
            "providers": provider_summary(),  # no secrets: only whether the key is set
            "default_model": settings.model,  # agents with no model (and flows) use it; None: the CLI's
            "mock": settings.mock,
            "connections": _conn_summary(),
            # where flows, library agents and connectors can be kept: this machine or an extension
            "locations": _locations(),
            "workspace": _ws_summary(),
        }

    runner = Runner(base)
    app.state.runner = runner

    async def queue_clock() -> None:
        """The queue's own clock: decided runs and queued runs go on when nothing holds them,
        with or without an editor open (a gate that turns older than a day releases them)."""
        while True:
            try:
                runner.tick()
            except Exception:  # the clock never stops; the next tick tries again
                logging.getLogger(__name__).exception("queue clock")
            await asyncio.sleep(QUEUE_TICK)

    @contextlib.asynccontextmanager
    async def lifespan(_app: FastAPI) -> Any:
        clock = asyncio.create_task(queue_clock())
        try:
            yield
        finally:
            clock.cancel()

    app.router.lifespan_context = lifespan

    ctx = Ctx(base, fb, csrf, runner)
    connectors_routes.register(app, ctx)
    providers_routes.register(app, ctx)
    flows_routes.register(app, ctx)
    runs_routes.register(app, ctx)
    workspaces_routes.register(app, ctx)

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

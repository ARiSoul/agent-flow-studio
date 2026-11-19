"""agent-flow-studio-editor: editor visual local de flows (FastAPI + canvas React Flow).

Só edita ficheiros em flows/<nome>/ (flow.yaml e prompts/*.md). Não executa flows, não lê `.env`
e nunca expande `${VAR}`: os segredos continuam fora dos flows. Escuta em 127.0.0.1 por defeito,
aceita só Hosts locais e exige o cabeçalho X-Flow-Editor nos pedidos que alteram ficheiros
(um site externo no browser não o consegue enviar sem preflight CORS, que aqui não é aceite).
"""
from __future__ import annotations

import argparse
import re
import shutil
import time
import webbrowser
from pathlib import Path
from typing import Any, Literal

import yaml
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, ValidationError
from starlette.middleware.trustedhost import TrustedHostMiddleware

from ..builder.catalog import BUILTIN_SERVERS, BUILTIN_TOOLS, CONTRACTS
from ..builder.compile import FlowError, flows_dir
from ..builder.spec import FlowSpec
from ..config import settings
from ..connections import client as conn_client
from ..connections import store as conn_store
from ..connections.model import SLUG as CONN_SLUG
from ..connections.model import Connection, types_meta
from . import assistant
from .runner import RunError, Runner, list_artifacts, read_artifact_text
from .validation import _atomic_write, _public, _structured, _validate
from .yaml_io import dump_flow, spec_to_data

STATIC = Path(__file__).parent / "static"
FLOW_NAME = re.compile(r"^[a-z0-9][a-z0-9_-]{0,39}$")
PROMPT_PATH = re.compile(r"^prompts/[A-Za-z0-9_-]{1,60}\.md$")
MAX_PROMPT = 100_000
MODELS = ["inherit", "sonnet", "opus", "haiku"]
CSRF_HEADER = "x-flow-editor"


class FlowBody(BaseModel):
    spec: dict[str, Any]
    prompts: dict[str, str] = Field(default_factory=dict)


class AssistBody(BaseModel):
    flow: str
    message: str = Field(min_length=1, max_length=20_000)
    spec: dict[str, Any]
    prompts: dict[str, str] = Field(default_factory=dict)
    session_id: str | None = Field(default=None, pattern=r"^[A-Za-z0-9-]{1,80}$")


class RunBody(BaseModel):
    flow: str
    input: str = Field(min_length=1, max_length=80)
    mode: Literal["full", "step", "until", "agent"] = "full"
    target: str | None = Field(default=None, max_length=40)
    instruction: str = Field(default="", max_length=20_000)


class ContinueBody(BaseModel):
    how: Literal["step", "full"] = "step"


class DecisionBody(BaseModel):
    decision: Literal["approve", "reject", "answer"]
    comment: str = Field(default="", max_length=20_000)


class ApprovalBody(BaseModel):
    approve: bool
    comment: str = Field(default="", max_length=5_000)


class SecretBody(BaseModel):
    value: str | None = Field(default=None, min_length=1, max_length=10_000)  # vai para o keyring
    env: str | None = Field(default=None, pattern=r"^[A-Z_][A-Z0-9_]*$")  # ou ${VAR}


class NewFlow(BaseModel):
    name: str
    title: str = Field(min_length=1, max_length=120)
    copy_from: str | None = None


def _blank(name: str, title: str) -> dict[str, Any]:
    return {
        "version": 1, "name": name, "title": title, "task": "Executa o flow para {input}.",
        "start": "primeiro",
        "agents": {"primeiro": {"description": "Primeiro passo do flow.",
                                "prompt": "Descreve aqui o que este agente faz.",
                                "tools": ["artifacts.read_artifact"]}},
        "gates": {"fim": {"kind": "human", "label": "Revisão humana"}},
        "edges": [{"from": "primeiro", "to": "fim", "when": "terminou"}],
        "layout": {"primeiro": {"x": 0, "y": 0}, "fim": {"x": 0, "y": 160}},
    }


# --- ficheiros -----------------------------------------------------------------------------------
def _flow_dir(base: Path, name: str, must_exist: bool = True) -> Path:
    if not FLOW_NAME.fullmatch(name):
        raise HTTPException(400, "nome de flow inválido")
    d = base / name
    if must_exist and not (d / "flow.yaml").is_file():
        raise HTTPException(404, f"flow {name!r} não existe")
    return d


def _disk_prompts(d: Path) -> dict[str, str]:
    p = d / "prompts"
    if not p.is_dir():
        return {}
    return {f"prompts/{f.name}": f.read_text(encoding="utf-8")
            for f in sorted(p.glob("*.md")) if PROMPT_PATH.fullmatch(f"prompts/{f.name}")}


def _check_prompts(prompts: dict[str, str]) -> None:
    for k, v in prompts.items():
        if not PROMPT_PATH.fullmatch(k):
            raise HTTPException(400, f"caminho de prompt inválido: {k!r} (use prompts/<nome>.md)")
        if len(v) > MAX_PROMPT:
            raise HTTPException(400, f"{k}: prompt demasiado longo")


# --- aplicação -----------------------------------------------------------------------------------
def create_app(base: Path | None = None, allowed_hosts: list[str] | None = None) -> FastAPI:
    base = (base or flows_dir()).resolve()
    app = FastAPI(title="Editor de flows", docs_url=None, redoc_url=None, openapi_url=None)
    app.add_middleware(TrustedHostMiddleware,
                       allowed_hosts=allowed_hosts or ["localhost", "127.0.0.1"])

    def csrf(request: Request) -> None:
        if request.headers.get(CSRF_HEADER) != "1":
            raise HTTPException(403, "pedido sem cabeçalho X-Flow-Editor")

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
                    "access": c.access}
                for n, c in conns.items()}

    def _conn_name(name: str) -> str:
        if not CONN_SLUG.fullmatch(name):
            raise HTTPException(400, "nome de conector inválido")
        return name

    # --- conectores ---------------------------------------------------------------------------
    @app.get("/api/connections")
    def connections() -> dict[str, Any]:
        conns = _conns()
        return {**types_meta(), "file": str(conn_store.connections_file()),
                "connections": {n: c.model_dump(mode="json") | {
                    "secret": conn_store.secret_status(n, c)} for n, c in conns.items()}}

    @app.put("/api/connections/{name}", dependencies=[Depends(csrf)])
    def save_connection(name: str, body: dict[str, Any]) -> dict[str, Any]:
        conns = _conns()
        old = conns.get(_conn_name(name))
        body.pop("secret", None)
        if old:  # as referências aos segredos não vêm da UI: mantêm-se
            body["secrets"] = dict(old.secrets)
        else:
            body["secrets"] = {}
        try:
            conns[name] = Connection.model_validate(body)
        except ValidationError as e:
            raise HTTPException(422, _structured(e, {})) from e
        conn_store.save(conns)
        return {"saved": name, "secret": conn_store.secret_status(name, conns[name])}

    @app.put("/api/connections/{name}/secret", dependencies=[Depends(csrf)])
    def set_secret(name: str, body: SecretBody) -> dict[str, Any]:
        conns = _conns()
        c = conns.get(_conn_name(name))
        if c is None:
            raise HTTPException(404, "conector não existe")
        field = c.secret_field
        if field is None:
            raise HTTPException(400, "este método de autenticação não usa segredo")
        if bool(body.value) == bool(body.env):
            raise HTTPException(400, "indica 'value' (guardado no keyring) ou 'env'")
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
            raise HTTPException(404, "conector não existe")
        conn_store.delete_secrets(name, c)
        conn_store.save(conns)
        return {"deleted": name}

    @app.post("/api/connections/{name}/test", dependencies=[Depends(csrf)])
    async def test_connection(name: str) -> dict[str, Any]:
        return await conn_client.test(_conn_name(name), _conns())

    @app.get("/api/flows")
    def list_all() -> list[dict[str, Any]]:
        out = []
        for f in sorted(base.glob("*/flow.yaml")):
            name = f.parent.name
            if not FLOW_NAME.fullmatch(name):
                continue
            item: dict[str, Any] = {"name": name, "title": name, "valid": False}
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
        d = _flow_dir(base, name)
        try:
            raw = yaml.safe_load((d / "flow.yaml").read_text(encoding="utf-8")) or {}
        except yaml.YAMLError as e:
            raise HTTPException(422, f"flow.yaml não é YAML válido: {e}") from e
        if not isinstance(raw, dict):
            raise HTTPException(422, "flow.yaml tem de ser um mapa")
        prompts = _disk_prompts(d)
        return {"spec": raw, "prompts": prompts, **_public(_validate(raw, prompts))}

    @app.post("/api/flows/{name}/validate")
    def validate(name: str, body: FlowBody) -> dict[str, Any]:
        d = _flow_dir(base, name)
        _check_prompts(body.prompts)
        return _public(_validate(body.spec, {**_disk_prompts(d), **body.prompts}))

    @app.put("/api/flows/{name}", dependencies=[Depends(csrf)])
    def save(name: str, body: FlowBody) -> JSONResponse:
        d = _flow_dir(base, name)
        _check_prompts(body.prompts)
        if body.spec.get("name") != name:
            raise HTTPException(400, "o nome do flow não pode mudar (duplica para renomear)")
        disk = _disk_prompts(d)
        result = _validate(body.spec, {**disk, **body.prompts})
        if not result["ok"]:
            return JSONResponse(_public(result), status_code=422)
        for k, v in body.prompts.items():
            if disk.get(k) != v:
                _atomic_write(d / k, v)
        flow = d / "flow.yaml"
        shutil.copyfile(flow, d / "flow.yaml.bak")
        _atomic_write(flow, dump_flow(result["spec"]))
        return JSONResponse(_public(result) | {"saved": True,
                                               "spec": spec_to_data(result["spec"])})

    @app.post("/api/assist", dependencies=[Depends(csrf)])
    def assist(body: AssistBody) -> StreamingResponse:
        """Chat com o assistente: devolve eventos NDJSON; nunca grava (o editor aplica o rascunho)."""
        d = _flow_dir(base, body.flow)
        _check_prompts(body.prompts)
        prompts = {**_disk_prompts(d), **body.prompts}

        async def events():  # type: ignore[no-untyped-def]
            async for ev in assistant.chat(body.message, body.flow, body.spec, prompts,
                                           body.session_id):
                yield assistant.encode(ev)

        return StreamingResponse(events(), media_type="application/x-ndjson")

    # --- execuções (fase 3) -----------------------------------------------------------------
    runner = Runner(base)
    app.state.runner = runner

    def _run_call(fn, *a):  # type: ignore[no-untyped-def]
        try:
            return fn(*a).summary()
        except RunError as e:
            raise HTTPException(e.status, str(e)) from e
        except FlowError as e:
            raise HTTPException(400, str(e)) from e

    @app.get("/api/runs")
    def runs() -> list[dict[str, Any]]:
        return [r.summary() for r in sorted(runner.runs.values(), key=lambda r: -r.started)]

    @app.post("/api/runs", dependencies=[Depends(csrf)], status_code=201)
    async def start_run(body: RunBody) -> dict[str, Any]:
        _flow_dir(base, body.flow)
        return _run_call(runner.start, body.flow, body.input, body.mode, body.target,
                         body.instruction)

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
            raise HTTPException(404, "artefacto não existe") from e

    @app.post("/api/flows", dependencies=[Depends(csrf)], status_code=201)
    def create(body: NewFlow) -> dict[str, Any]:
        d = _flow_dir(base, body.name, must_exist=False)
        if d.exists():
            raise HTTPException(409, f"já existe um flow {body.name!r}")
        if body.copy_from:
            src = _flow_dir(base, body.copy_from)
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
        return {"name": body.name}

    @app.delete("/api/flows/{name}", dependencies=[Depends(csrf)])
    def delete(name: str) -> dict[str, Any]:
        d = _flow_dir(base, name)
        trash = base / ".trash"
        trash.mkdir(exist_ok=True)
        dest = trash / f"{name}-{time.strftime('%Y%m%d-%H%M%S')}"
        shutil.move(str(d), dest)  # recuperável: nada é apagado em definitivo
        return {"trashed": str(dest.relative_to(base))}

    if (STATIC / "index.html").is_file():
        app.mount("/", StaticFiles(directory=STATIC, html=True), name="static")
    else:
        @app.get("/", response_class=HTMLResponse)
        def no_build() -> str:
            return ("<h1>Editor por compilar</h1><p>Corre <code>npm install && npm run build</code>"
                    " na pasta <code>editor/</code>.</p>")

    return app


def main() -> None:
    p = argparse.ArgumentParser(prog="agent-flow-studio-editor")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8765)
    p.add_argument("--flows", type=Path, help="pasta dos flows (por defeito FLOWS_DIR ou flows/)")
    p.add_argument("--no-browser", action="store_true")
    a = p.parse_args()
    import uvicorn

    hosts = ["localhost", "127.0.0.1"] + ([a.host] if a.host not in ("127.0.0.1", "0.0.0.0") else [])
    base = (a.flows or flows_dir()).resolve()
    app = create_app(base, allowed_hosts=hosts)
    url = f"http://127.0.0.1:{a.port}/"
    print(f"Editor de flows em {url}  (flows: {base})")
    if not a.no_browser:
        webbrowser.open(url)
    uvicorn.run(app, host=a.host, port=a.port, log_level="warning")


if __name__ == "__main__":
    main()

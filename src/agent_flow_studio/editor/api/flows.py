"""Flows (list, open, validate, save, create, move, delete), templates, the assistants,
conversations, saved commands and the agent library."""
from __future__ import annotations

import json
import shutil
import time
from pathlib import Path
from typing import Any

import yaml
from fastapi import Depends, FastAPI, HTTPException
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import ValidationError

from ... import workspaces
from ...builder import convert
from ...builder.compile import find_flow, flow_locations
from ...builder.library import LibraryError, agents_dict, delete_agent, move_agent, save_local
from ...builder.spec import FlowSpec
from ...builder.templates import TemplateError, templates_dict
from ...builder.templates import render as render_template
from ...builder.types import IOType
from ...messages import to_json
from ...publish import (
    PUBLIC,
    PrivateTermsError,
    set_flow_public,
    terms_in,
)
from .. import assistant, ws_assistant
from .. import chats as assist_chats
from .. import commands as assist_commands
from ..runner import (
    input_visible,
)
from ..validation import _atomic_write, _public, _structured, _validate
from ..yaml_io import dump_flow, dump_raw, spec_to_data
from .common import (
    FLOW_NAME,
    PROMPT_PATH,
    SECRET_ERROR,
    AssistBody,
    Ctx,
    FlowBody,
    LibraryBody,
    NewFlow,
    WsAssistBody,
    _blank,
    _check_prompts,
    _disk_prompts,
    _flow_dir,
    _flow_place,
    _flow_text,
    _in_public_repo,
    _location_dir,
)


def register(app: FastAPI, ctx: Ctx) -> None:
    base = ctx.base
    fb = ctx.fb
    csrf = ctx.csrf
    runner = ctx.runner
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

    @app.post("/api/flows/{name}/to-graph")
    def to_graph_plan(name: str, body: FlowBody) -> dict[str, Any]:
        """What converting the draft to the graph engine would change (nothing is saved): the
        converted spec, its validation problems as a graph flow, and notes on what works
        differently. The editor shows them and, if the person agrees, uses `spec` as the draft."""
        d = _flow_dir(fb(), name)
        _check_prompts(body.prompts)
        raw = convert.to_graph(body.spec)
        prompts = {**_disk_prompts(d), **body.prompts}
        checked = _validate(raw, prompts)
        notes = []
        try:
            for level, m, nodes in convert.graph_notes(FlowSpec.model_validate(body.spec)):
                notes.append({"level": level, "msg": str(m), "nodes": nodes, **(to_json(m) or {})})
        except ValidationError:
            pass  # a draft that does not parse: its problems are in `errors`
        return {"spec": raw, "errors": checked.get("errors", []), "notes": notes}

    @app.post("/api/flows/{name}/validate")
    def validate(name: str, body: FlowBody) -> dict[str, Any]:
        d = _flow_dir(fb(), name)
        _check_prompts(body.prompts)
        return _public(_validate(body.spec, {**_disk_prompts(d), **body.prompts}))

    @app.put("/api/flows/{name}", dependencies=[Depends(csrf)])
    async def save(name: str, body: FlowBody) -> JSONResponse:
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
            # runs of this flow that are not over take its autonomy now (also mid-step)
            refreshed = runner.refresh_autonomy(name)
            return JSONResponse(_public(result) | {"saved": True, "runs_updated": refreshed,
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

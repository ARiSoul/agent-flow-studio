"""Studio workspaces, folders, the workspace context and learning, uploads, the Board,
copying between workspaces and hand-offs."""
from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from fastapi import Depends, FastAPI, HTTPException, Request
from pydantic import ValidationError

from ... import context, workspaces
from ...builder.compile import flow_locations
from ...builder.library import LibraryError
from ...builder.spec import FlowSpec
from ...builder.types import KINDS as IO_KINDS_ALL
from ...connections import store as conn_store
from ...messages import MsgError, reason, to_json
from ...tools import io as io_tool
from .. import copying, uploads
from ..runner import (
    RunError,
)
from ..validation import _structured
from .common import (
    FLOW_NAME,
    SLUG_RE,
    CopyBody,
    Ctx,
    DuplicateBody,
    NewWorkspace,
    _audit_event,
    _flow_dir,
    _run_call,
)


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


def register(app: FastAPI, ctx: Ctx) -> None:
    base = ctx.base
    fb = ctx.fb
    csrf = ctx.csrf
    runner = ctx.runner
    # --- workspaces ---------------------------------------------------------------------------



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
        waits = {(w["flow"], w["input"]) for w in runner.waiting()
                 if not w.get("dismissed") and not w.get("stale")}  # as the Run panel counts
        past = runner.past_by_flow()  # one scan of the runs folder for every card
        for _, d in flow_locations(fb()):
            for f in sorted(d.glob("*/flow.yaml")):
                name = f.parent.name
                if not FLOW_NAME.fullmatch(name) or name in seen:
                    continue
                seen.add(name)
                item: dict[str, Any] = {"name": name, "title": name, "valid": False, "inputs": {},
                                        "outputs": [], "subflows": [], "handoffs": []}
                raw: Any = {}
                try:
                    raw = yaml.safe_load(f.read_text(encoding="utf-8")) or {}
                    item["title"] = str(raw.get("title") or name)
                    item["description"] = str(raw.get("description") or "")
                    data, unknown = workspaces.substitute(raw)
                    if unknown:
                        raise workspaces.unknown_error(unknown)
                    spec = FlowSpec.model_validate(data)
                    item |= {"valid": True, "execution": spec.execution}
                    item["inputs"] = {k: {"type": t.type, "label": t.label or k,
                                          **({"source": t.source.model_dump()} if t.source
                                             else {})} for k, t in spec.inputs.items()}
                    item["outputs"] = [{"agent": an, "name": on, "type": o.type}
                                       for an, a in spec.agents.items()
                                       for on, o in a.outputs.items()]
                    item["subflows"] = sorted({a.subflow.flow for a in spec.agents.values()
                                               if a.subflow})
                except (ValidationError, MsgError) as e:
                    # the first reason, with its code (the card shows it in the UI language)
                    first = (_structured(e, raw if isinstance(raw, dict) else {}) or [{}])[0]
                    item["error"] = {k: first[k] for k in ("msg", "code", "params") if k in first}
                except (yaml.YAMLError, AttributeError, ValueError) as e:
                    item["error"] = {"msg": str(e).splitlines()[0][:200] if str(e) else type(e).__name__}
                runs = past.get(name, [])[:10]
                if runs:
                    item["last_run"] = {k: runs[0][k] for k in ("input", "status", "started")}
                    item["last_run"]["waiting"] = (name, runs[0]["input"]) in waits
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

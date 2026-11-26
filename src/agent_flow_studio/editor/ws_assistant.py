"""Workspace assistant (chat in the Board of a studio workspace).

It reads the open workspace (settings, context, flows, connectors, library) and answers questions,
and it **proposes** changes: workspace settings, a new flow, a connector. It never writes: each
proposal is checked here and shown in the chat as a card; the person applies it with the editor's
own API calls. Same safety net as the flow assistant: no built-in tools, `dontAsk`,
`setting_sources=[]` and a hook with a closed list. Connector proposals never carry secrets and
are read-only (the person gives write access and secrets in the Connectors window).
"""
from __future__ import annotations

import copy
import json
import re
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

from claude_agent_sdk import (
    ClaudeAgentOptions,
    create_sdk_mcp_server,
    tool,
)
from pydantic import ValidationError

from .. import context, workspaces
from ..builder.catalog import BUILTIN_TOOLS, CONTRACTS
from ..builder.compile import DISALLOWED, find_flow
from ..builder.library import LibraryError, list_agents
from ..builder.templates import TemplateError, templates_dict
from ..builder.templates import render as render_template
from ..config import settings
from ..hooks import DELEGATION_TOOLS, make_guard, make_hooks
from ..messages import MsgError
from ..tools.common import err, ok
from .assistant import (
    LANGS,
    _connections_info,
    _contract_fields,
    _workspace_info,
    normalize_lang,
    stream,
    with_command,
)
from .validation import _validate
from .yaml_io import spec_to_data

SERVER = "workspace_builder"
PROMPT = (Path(__file__).parent / "ws_assistant.md").read_text(encoding="utf-8")
FLOW_NAME = r"^[a-z0-9][a-z0-9_-]{0,39}$"
CHAT_KEY = "@workspace"  # the conversation history of the workspace assistant (chats.py)
MAX_TURNS = 60  # model turns per message; at the limit the chat offers to continue
MAX_TEXT = 6000  # per prompt file or context section shown to the model

_STR = {"type": "string"}
_STRS = {"type": "array", "items": _STR}
_OBJ = {"type": "object"}


def _schema(props: dict[str, Any], required: list[str] | None = None) -> dict[str, Any]:
    return {"type": "object", "properties": props, "required": required or []}


def _cut(text: str, n: int = MAX_TEXT) -> str:
    return text if len(text) <= n else text[:n] + f"\n… ({len(text) - n} more characters)"


def _problems(errors: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """All the problems at once, each with where it is (field path and nodes), so the model can
    fix them in one go."""
    return [{"where": e.get("loc") or "flow", "nodes": e.get("nodes") or [],
             "problem": e["msg"]} for e in errors]


def fill_defaults(spec: dict[str, Any], prompts: dict[str, str]) -> list[str]:
    """Makes a short flow (agents with a description, edges) complete, in place: version,
    graph execution, the start (the first agent), a task that names the input, and a prompt file
    for each agent that has no prompt (its description, to be refined). Returns what was filled."""
    filled: list[str] = []
    agents = spec.get("agents")
    if not isinstance(agents, dict) or not agents:
        return filled
    spec.setdefault("version", 1)
    if "execution" not in spec:
        spec["execution"] = "graph"
        filled.append("execution: graph")
    if not spec.get("start"):
        spec["start"] = next(iter(agents))
        filled.append(f"start: {spec['start']}")
    task = spec.get("task")
    if not spec.get("inputs") and (not isinstance(task, str) or "{input}" not in task):
        spec["task"] = (f"{task.rstrip()} " if isinstance(task, str) and task.strip() else
                        "Work on ") + "{input}."
        filled.append("task with {input}")
    for n, ag in agents.items():
        if not isinstance(ag, dict) or ag.get("action") or ag.get("subflow"):
            continue
        if not ag.get("prompt") and not ag.get("prompt_file"):
            ag["prompt_file"] = f"prompts/{n}.md"
            if ag["prompt_file"] not in prompts:
                prompts[ag["prompt_file"]] = (
                    f"# {n}\n\n{ag.get('description') or ''}\n\n"
                    "Cite the evidence of every conclusion (file:line, request, record).\n")
                filled.append(f"prompt of {n}")
    return filled


class Proposals:
    """What the assistant proposed in this message (id → card), and the tools."""

    def __init__(self) -> None:
        self.items: dict[str, dict[str, Any]] = {}

    def add(self, kind: str, name: str, data: dict[str, Any]) -> str:
        pid = f"{kind}:{name}"
        self.items[pid] = {"id": pid, "kind": kind, "name": name, **data}
        return pid

    def tools(self) -> list[Any]:
        p = self

        @tool("view_workspace", "The open workspace: title, description, roots (name, folder, "
              "whether it exists and whether its context was explored), variables, environments "
              "(kind and variables), the current environment and the default one.", _schema({}))
        async def view_workspace(_: dict) -> dict:
            ws = workspaces.current()
            if ws.studio or ws.spec is None:
                return ok({"workspace": ws.name, "studio": True, "note": (
                    "the studio's own workspace: no settings; the person can create a workspace "
                    "in the editor (⋯ → Workspaces)")})
            st = context.state(ws)
            folders = ws.resolve(None)[1]
            roots = {n: {"path": r.path, "description": r.description,
                         "exists": Path(folders[n]).is_dir(), "explored": n in st}
                     for n, r in ws.spec.roots.items()}
            return ok({"workspace": ws.name, "title": ws.spec.title,
                       "description": ws.spec.description, "where": ws.where, "roots": roots,
                       "vars": ws.spec.vars,
                       "environments": {n: e.model_dump() for n, e in ws.spec.environments.items()},
                       "default_environment": ws.spec.default_environment,
                       "current_environment": workspaces.current_env()})

        @tool("view_context", "The workspace context (what the agents of its flows read: one "
              "section per root, written by the context keeper, plus the person's notes). "
              "Without 'section': the names and sizes; with 'section': that text.",
              _schema({"section": _STR}))
        async def view_context(a: dict) -> dict:
            ws = workspaces.current()
            if ws.studio:
                return ok("the studio's own workspace has no context")
            secs = context.sections(ws)
            if a.get("section"):
                if a["section"] not in secs:
                    return err(f"no section {a['section']!r}; sections: {sorted(secs)}")
                return ok(_cut(secs[a["section"]]))
            return ok({n: f"{len(t)} characters" for n, t in secs.items()}
                      or "(empty: the person can explore the roots in the Board)")

        @tool("view_flows", "The flows of the workspace: title, execution, human gates, typed "
              "inputs (and where each usually comes from) and outputs.", _schema({}))
        async def view_flows(_: dict) -> dict:
            return ok(_workspace_info(None)["flows"] or "(no flows yet)")

        @tool("view_flow", "One flow of the workspace in full: its flow.yaml and prompt files "
              "(to answer questions about it, or as an example for a new one).",
              _schema({"name": _STR}, ["name"]))
        async def view_flow(a: dict) -> dict:
            found = find_flow(a["name"]) if isinstance(a.get("name"), str) else None
            if found is None:
                return err(f"flow {a.get('name')!r} does not exist; see view_flows")
            d = found[1]
            prompts = {f"prompts/{f.name}": _cut(f.read_text(encoding="utf-8"))
                       for f in sorted((d / "prompts").glob("*.md"))}
            return ok({"flow_yaml": (d / "flow.yaml").read_text(encoding="utf-8"),
                       "prompts": prompts})

        @tool("view_connectors", "The connectors the workspace can use (its own and the "
              "studio's): type, environment, access, instructions and named actions.",
              _schema({}))
        async def view_connectors(_: dict) -> dict:
            return ok(_connections_info() or "(no connectors)")

        @tool("view_catalog", "What flows can use: built-in tools, contracts, flow templates "
              "(with their parameters), library agents, and the connector types with their "
              "sign-in methods.", _schema({}))
        async def view_catalog(_: dict) -> dict:
            from ..connections.model import METHOD_LABEL, TYPES
            try:
                lib = {x.id: {"title": x.title, "description": " ".join(x.spec.description.split())}
                       for x in list_agents().values()}
            except LibraryError as e:
                lib = {"error": str(e)}
            return ok({
                "tools": {t.id: t.description for t in BUILTIN_TOOLS.values()},
                "contracts": {c: _contract_fields(c) for c in CONTRACTS},
                "templates": templates_dict(),
                "library_agents": lib,
                "connector_types": {k: {"label": v["label"], "methods": {
                    m: METHOD_LABEL.get(m, m) for m in v["methods"]}} for k, v in TYPES.items()},
            })

        @tool("propose_workspace_change",
              "Proposes changes to the workspace settings. Only the given fields change: title, "
              "description, default_environment; set_roots {name: {path, description}} adds or "
              "replaces roots (path absolute or relative to the workspace home); remove_roots; "
              "set_vars {name: value} (no secrets) and remove_vars; set_environments {name: "
              "{kind: dev|staging|prod, vars}} and remove_environments. Names: lowercase letters, "
              "digits and _. Nothing is saved: the person applies it from the card in the chat.",
              _schema({"title": _STR, "description": _STR, "default_environment": _STR,
                       "set_roots": _OBJ, "remove_roots": _STRS, "set_vars": _OBJ,
                       "remove_vars": _STRS, "set_environments": _OBJ,
                       "remove_environments": _STRS}))
        async def propose_workspace_change(a: dict) -> dict:
            ws = workspaces.current()
            if ws.studio or ws.spec is None:
                return err("the studio's own workspace has no settings")
            # build on the last proposal of this message, so several calls add up
            prev = p.items.get(f"workspace:{ws.name}")
            data = copy.deepcopy(prev["after"] if prev else ws.spec.model_dump(mode="json"))
            for k in ("title", "description", "default_environment"):
                if k in a:
                    data[k] = a[k] or (None if k == "default_environment" else "")
            for key in ("roots", "vars", "environments"):
                part = data.setdefault(key, {})
                for n in a.get(f"remove_{key}") or []:
                    if n not in part:
                        return err(f"{key}: {n!r} does not exist")
                    part.pop(n)
                part.update(a.get(f"set_{key}") or {})
            try:
                spec = workspaces.WorkspaceSpec.model_validate(data)
            except (MsgError, ValidationError) as e:
                return err(f"not valid: {e}")
            before = ws.spec.model_dump(mode="json")
            after = spec.model_dump(mode="json")
            changes = {k: after[k] for k in ("title", "description", "roots", "vars",
                                             "environments", "default_environment")
                       if after[k] != before[k]}
            if not changes:
                p.items.pop(f"workspace:{ws.name}", None)
                return ok("nothing would change")
            p.add("workspace", ws.name, {"changes": changes, "after": after,
                                         "before": {k: before[k] for k in changes}})
            return ok(f"Proposal ready ({', '.join(sorted(changes))}). The person applies it "
                      "with the button in the chat; it is not saved yet.")

        @tool("propose_flow",
              "Proposes a NEW flow for this workspace. Either from a template (template + params; "
              "see view_catalog) or written in full (spec = the flow.yaml content as an object, "
              "prompts = {'prompts/<agent>.md': text}). A short spec is enough: agents with a "
              "description (and tools, inputs, outputs), gates and edges; the studio fills in "
              "execution (graph), start (the first agent), a task with {input} and a prompt "
              "file per agent from its description. It must be valid: ALL the problems come "
              "back at once, each with where it is, so fix them together in the next call. Keep "
              "it a solid starting point: the person refines it in the "
              "flow editor (with the flow assistant). Autonomy is always 'approve' or "
              "'approve_prod'. Nothing is saved: the person creates it from the card.",
              _schema({"name": _STR, "title": _STR, "template": _STR, "params": _OBJ,
                       "spec": _OBJ, "prompts": _OBJ}, ["name", "title"]))
        async def propose_flow(a: dict) -> dict:
            name, title = a["name"], a["title"]
            if not re.fullmatch(FLOW_NAME, name):
                return err("invalid flow name: lowercase letters, digits, _ and -")
            if find_flow(name):
                return err(f"a flow {name!r} already exists")
            if bool(a.get("template")) == bool(a.get("spec")):
                return err("give either 'template' (with params) or 'spec' (with prompts)")
            filled: list[str] = []
            if a.get("template"):
                try:
                    fs, prompts = render_template(a["template"], name, title,
                                                  {k: str(v) for k, v in
                                                   (a.get("params") or {}).items()})
                except TemplateError as e:
                    return err(str(e))
                spec = spec_to_data(fs)
            else:
                spec = copy.deepcopy(a["spec"]) | {"name": name, "title": title}
                prompts = {str(k): str(v) for k, v in (a.get("prompts") or {}).items()}
                bad = [k for k in prompts if not re.fullmatch(r"prompts/[a-z0-9_-]+\.md", k)]
                if bad:
                    return err(f"prompt files must be prompts/<name>.md: {bad}")
            filled = fill_defaults(spec, prompts) if not a.get("template") else []
            if spec.get("autonomy") == "auto" or any(
                    isinstance(x, dict) and x.get("autonomy") == "auto"
                    for x in (spec.get("agents") or {}).values()):
                return err("'auto' autonomy is only chosen by a person in the editor")
            for m in (spec.get("mcp_servers") or {}).values():  # a person approves external MCPs
                if isinstance(m, dict):
                    m.update({"approved": False, "read_only": False})
            r = _validate(spec, prompts)
            if not r["ok"]:
                return err(json.dumps({"problems": _problems(r["errors"]),
                                       **({"filled_in": filled} if filled else {}),
                                       "hint": "fix every problem listed here in one call"},
                                      ensure_ascii=False))
            if r.get("warnings"):  # e.g. a flow input no agent receives: fix it before proposing
                return err(json.dumps({"warnings": _problems(r["warnings"]),
                                       "hint": "fix these in one call: a flow input must reach "
                                               "an agent (inputs: {x: {from: input.x}})"},
                                      ensure_ascii=False))
            data = spec_to_data(r["spec"])
            p.add("flow", name, {"title": title, "spec": data, "prompts": prompts,
                                 "agents": sorted(data.get("agents") or {}),
                                 "description": data.get("description", ""),
                                 "template": a.get("template")})
            done = f" Filled in: {', '.join(filled)}." if filled else ""
            return ok(f"Flow {name!r} is valid and ready.{done} The person creates it with the "
                      "button in the chat; it is not saved yet.")

        @tool("propose_connector",
              "Proposes a connector for this workspace (new, or a change to one of its own). "
              "definition: {type, description, environment, base_url, organization, auth: "
              "{method, …no secrets}, allowed_paths, instructions, actions: {name: {description, "
              "access: read, method: GET, path, query, params}}, actions_only}. Read access only "
              "and no secrets: the person gives the secret (keyring or ${VAR}), signs in, and "
              "gives write access in the Connectors window. Nothing is saved: the person saves "
              "it from the card.",
              _schema({"name": _STR, "definition": _OBJ}, ["name", "definition"]))
        async def propose_connector(a: dict) -> dict:
            from ..connections import store
            from ..connections.model import SLUG, Connection
            name = a["name"]
            if not SLUG.fullmatch(name):
                return err("invalid name: lowercase letters, digits and _, starting with a letter")
            body = copy.deepcopy(a["definition"])
            if not isinstance(body, dict):
                return err("definition: an object")
            if "secret" in body or body.get("secrets"):
                return err("no secrets in a proposal: the person adds them in the Connectors "
                           "window")
            body.pop("secrets", None)
            if body.get("access", "read") != "read" or any(
                    isinstance(x, dict) and x.get("access", "read") != "read"
                    for x in (body.get("actions") or {}).values()):
                return err("read access only: the person gives write access in the Connectors "
                           "window")
            try:
                c = Connection.model_validate(body | {"secrets": {}})
            except ValidationError as e:
                return err(f"not valid: {e}")
            ws = workspaces.current()
            try:
                existing = store.load().get(name)
                where = store.location(name) if existing else None
            except store.ConnectorConfigError as e:
                return err(str(e))
            if existing and not ws.studio and where != "workspace":
                return err(f"{name!r} is a studio connector: propose a new name for the "
                           "workspace's own one")
            p.add("connector", name, {
                "exists": existing is not None,
                "connector": c.model_dump(mode="json", exclude={"secrets"}, exclude_defaults=True)
                | {"type": c.type},
                "secret_field": c.secret_field, "sign_in": c.type == "website"
                or c.auth.method == "oauth_browser"})
            needs = [f"a secret ({c.secret_field})"] if c.secret_field else []
            if c.type == "website" or c.auth.method == "oauth_browser":
                needs.append("a sign-in")
            return ok(f"Connector {name!r} is valid and ready. The person saves it with the "
                      "button in the chat" + (f", then adds {' and '.join(needs)} in the "
                                              "Connectors window" if needs else "") + ".")

        return [view_workspace, view_context, view_flows, view_flow, view_connectors,
                view_catalog, propose_workspace_change, propose_flow, propose_connector]


READ_ONLY_TOOLS = {"view_workspace", "view_context", "view_flows", "view_flow",
                   "view_connectors", "view_catalog"}


def system_prompt(lang: str | None = None) -> str:
    name = LANGS[normalize_lang(lang)]
    return (f"{PROMPT.rstrip()}\n\n## Language\nReply to the user in {name}. Content you write "
            "(flow prompts, descriptions) follows what the user asks; if the user does not say, "
            "keep the language the workspace already uses.\n")


def build_options(props: Proposals, session_id: str | None,
                  lang: str | None = None) -> ClaudeAgentOptions:
    tools = props.tools()
    allowed = [f"mcp__{SERVER}__{t.name}" for t in tools]
    return ClaudeAgentOptions(
        system_prompt=system_prompt(lang),
        tools=[],  # no built-in tools (no delegation either)
        mcp_servers={SERVER: create_sdk_mcp_server(name=SERVER, version="0.1.0", tools=tools)},
        allowed_tools=allowed,
        disallowed_tools=[*DISALLOWED, *DELEGATION_TOOLS],
        permission_mode="dontAsk",
        setting_sources=[],
        hooks=make_hooks(make_guard(allowed)),
        max_turns=MAX_TURNS,
        model=settings.model,
        resume=session_id,
    )


async def chat(message: str, session_id: str | None = None, lang: str | None = None,
               command: Any = None) -> AsyncIterator[dict[str, Any]]:
    """Events: text, tool, turn, done (with the proposals) or error."""
    props = Proposals()
    ws = workspaces.current()
    prompt = with_command(f"[Workspace open in the Board: {ws.name}]", message, command)
    async for ev in stream(prompt, build_options(props, session_id, lang), SERVER, READ_ONLY_TOOLS,
                           lambda _: {"proposals": [{k: v for k, v in x.items() if k != "after"}
                                                    for x in props.items.values()]}):
        yield ev

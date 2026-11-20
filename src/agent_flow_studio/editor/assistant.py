"""Flow-building assistant (chat in the editor).

The model only has the `flow_builder` tools, which change an **in-memory draft** of the open flow
(never the disk). The editor receives the draft at the end of each message; the user reviews and
saves it. Same safety net as the runtime: no built-in tools, `dontAsk`, `setting_sources=[]` and a
hook with a closed list. The assistant does not approve external MCPs: any MCP it creates or changes
gets `approved: false` and `read_only: false` until the user ticks them in the panel.
"""
from __future__ import annotations

import copy
import json
import re
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ResultMessage,
    TextBlock,
    ToolUseBlock,
    create_sdk_mcp_server,
    query,
    tool,
)

from ..builder.catalog import BUILTIN_TOOLS, CONTRACTS
from ..builder.compile import DISALLOWED
from ..config import settings
from ..hooks import DELEGATION_TOOLS, make_guard, make_hooks
from ..tools.common import err, ok
from .validation import _validate

SERVER = "flow_builder"
PROMPT = (Path(__file__).parent / "assistant.md").read_text(encoding="utf-8")
SLUG = re.compile(r"^[a-z][a-z0-9_]{0,39}$")
MAX_TURNS = 40
LANGS = {"pt": "European Portuguese (pt-PT)", "en": "English", "es": "Spanish"}
DEFAULT_LANG = "en"

_STR = {"type": "string"}
_STRS = {"type": "array", "items": _STR}
_WRITES = {"type": "array", "items": {"type": "object", "properties": {
    "name": _STR, "contract": _STR, "draft": {"type": "boolean"}}, "required": ["name"]}}
_KINDS = ["text", "markdown", "number", "integer", "boolean", "date", "datetime", "url", "path",
          "file", "choice", "json"]
# a typed value (flow input or agent output); the flow validation checks the details
_IOTYPE = {"type": "object", "properties": {
    "type": {"type": "string", "enum": _KINDS}, "label": _STR, "description": _STR,
    "required": {"type": "boolean"}, "default": {}, "multiple": {"type": "boolean"},
    "min": {"type": "number"}, "max": {"type": "number"}, "pattern": _STR, "options": _STRS,
    "accept": _STRS, "contract": _STR, "draft": {"type": "boolean"}}}
_IOMAP = {"type": "object", "additionalProperties": _IOTYPE}
_AGENT_INPUTS = {"type": "object", "additionalProperties": {"type": "object", "properties": {
    "from": _STR, "type": {"type": "string", "enum": _KINDS}, "description": _STR,
    "required": {"type": "boolean"}}}}


def _rename_refs(text: str, old: str, new: str) -> str:
    """`old.x` → `new.x` in `from` refs and `if` expressions (whole words only)."""
    return re.sub(rf"(?<![A-Za-z0-9_.]){re.escape(old)}\.", f"{new}.", text)


def _schema(props: dict[str, Any], required: list[str] | None = None) -> dict[str, Any]:
    return {"type": "object", "properties": props, "required": required or []}


def _contract_fields(name: str) -> dict[str, Any]:
    s = CONTRACTS[name].json_schema()
    if s.get("type") == "array":
        ref = s["items"].get("$ref", "").rsplit("/", 1)[-1]
        s = s.get("$defs", {}).get(ref, s["items"])
        return {"list_of": sorted(s.get("properties", {}))}
    return {"fields": sorted(s.get("properties", {}))}


def _connections_info() -> dict[str, Any]:
    from ..connections import store

    try:
        return {n: f"{c.type} · {c.environment} · {c.description}".strip(" ·")
                for n, c in store.load().items()}
    except store.ConnectorConfigError as e:
        return {"error": str(e)}


class Draft:
    """Flow draft ({spec, prompts}) and the tools that change it."""

    def __init__(self, spec: dict[str, Any], prompts: dict[str, str]):
        self.spec = copy.deepcopy(spec)
        self.prompts = dict(prompts)
        self.changed = False

    # --- helpers
    def doc(self) -> dict[str, Any]:
        return {"spec": self.spec, "prompts": self.prompts}

    def validation(self) -> dict[str, Any]:
        r = _validate(self.spec, self.prompts)
        return {"ok": r["ok"], "errors": r["errors"], "flow_section": r.get("flow_section"),
                "allowed_tools": r.get("allowed_tools")}

    def _summary(self, what: str) -> dict:
        self.changed = True
        v = self.validation()
        state = "flow valid" if v["ok"] else "problems: " + "; ".join(e["msg"] for e in v["errors"])
        return ok(f"{what}. State: {state}")

    def _nodes(self) -> set[str]:
        return set(self.spec.get("agents") or {}) | set(self.spec.get("gates") or {})

    def _edges(self) -> list[dict[str, Any]]:
        return self.spec.setdefault("edges", [])

    # --- tools
    def tools(self) -> list[Any]:
        d = self

        @tool("view_flow", "Shows the open flow (current draft) and its validation problems.",
              _schema({}))
        async def view_flow(_: dict) -> dict:
            v = d.validation()
            return ok({"flow": d.spec, "prompt_files": sorted(d.prompts),
                       "valid": v["ok"], "problems": [e["msg"] for e in v["errors"]]})

        @tool("view_catalog", "Built-in tools, contracts (with fields) and available models.",
              _schema({}))
        async def view_catalog(_: dict) -> dict:
            return ok({
                "tools": {t.id: t.description for t in BUILTIN_TOOLS.values()},
                "contracts": {c: _contract_fields(c) for c in CONTRACTS},
                "models": ["inherit", "sonnet", "opus", "haiku"],
                "configured_connections": _connections_info(),
                "flow_external_mcps": {n: m.get("tools", [])
                                       for n, m in (d.spec.get("mcp_servers") or {}).items()},
            })

        @tool("view_prompt", "Reads an agent's prompt or the orchestrator's (node='orchestrator').",
              _schema({"node": _STR}, ["node"]))
        async def view_prompt(a: dict) -> dict:
            owner = (d.spec.get("orchestrator") or {}) if a["node"] == "orchestrator" \
                else (d.spec.get("agents") or {}).get(a["node"])
            if owner is None:
                return err(f"agent {a['node']!r} does not exist")
            if owner.get("prompt_file"):
                return ok(d.prompts.get(owner["prompt_file"], "(empty file)"))
            return ok(owner.get("prompt") or "(no prompt)")

        @tool("update_flow", "Changes general flow fields: title, description, model, max_turns, "
              "task, start, execution ('orchestrator' = an LLM orchestrator follows the graph; "
              "'graph' = the studio runs the graph, in parallel where it can), max_parallel, and "
              "inputs: the typed flow inputs {name: {type, label, required, default, options…}} "
              "(the run form). Legacy flows without inputs use input_label/input_pattern and "
              "{input} in the task; with inputs, the task may use {name} and {run_id}.",
              _schema({"title": _STR, "description": _STR, "model": _STR,
                       "max_turns": {"type": "integer"}, "task": _STR, "input_label": _STR,
                       "input_pattern": _STR, "start": _STR,
                       "execution": {"type": "string", "enum": ["orchestrator", "graph"]},
                       "max_parallel": {"type": "integer"}, "inputs": _IOMAP,
                       "autonomy": {"type": "string", "enum": ["approve", "approve_prod"]}}))
        async def update_flow(a: dict) -> dict:
            for k in ("title", "description", "model", "max_turns", "task", "start", "autonomy",
                      "execution", "max_parallel", "inputs"):
                if k in a:
                    d.spec[k] = a[k]
            if "input_label" in a or "input_pattern" in a:
                inp = d.spec.setdefault("input", {})
                if "input_label" in a:
                    inp["label"] = a["input_label"]
                if "input_pattern" in a:
                    inp["pattern"] = a["input_pattern"]
            return d._summary("Flow updated")

        @tool("set_agent",
              "Creates or updates an agent. Only the given fields change. tools: catalog ids "
              "('server.tool'); writes: [{name, contract?, draft?}]; reads: names of artifacts "
              "written by other agents; connections: configured connectors the agent can read "
              "(GET). The prompt of a new agent goes to prompts/<name>.md. inputs: {name: "
              "{from: 'input.<x>' or '<agent>.<output>', type?, required?, description?}}; "
              "outputs: {name: {type, required?, options?, contract?, draft?, multiple?…}} — the "
              "agent gets them with io.get_inputs and saves them with io.set_output (implicit). "
              "join ('all'|'any') and max_runs only matter in graph execution.",
              _schema({"name": _STR, "description": _STR, "prompt": _STR, "tools": _STRS,
                       "model": _STR, "max_turns": {"type": "integer"}, "skills": _STRS,
                       "reads": _STRS, "writes": _WRITES, "connections": _STRS,
                       "write_connections": _STRS, "inputs": _AGENT_INPUTS, "outputs": _IOMAP,
                       "join": {"type": "string", "enum": ["all", "any"]},
                       "max_runs": {"type": "integer"}, "icon": _STR,
                       "autonomy": {"type": "string", "enum": ["approve", "approve_prod"]}},
                      ["name"]))
        async def set_agent(a: dict) -> dict:
            name = a["name"]
            if not SLUG.fullmatch(name):
                return err("invalid name: lowercase letters, digits and _, starting with a letter")
            agents = d.spec.setdefault("agents", {})
            if name in (d.spec.get("gates") or {}):
                return err(f"a gate {name!r} already exists")
            new = name not in agents
            if new and not a.get("description"):
                return err("a new agent needs a description")
            ag = agents.setdefault(name, {})
            if new:
                ag["prompt_file"] = f"prompts/{name}.md"
                d.prompts.setdefault(ag["prompt_file"], a.get("prompt") or "")
            if "connections" in a:  # connectors imply the read tool
                tools = list(a.get("tools", ag.get("tools", [])))
                if a["connections"] and "connections.http_get" not in tools:
                    tools.append("connections.http_get")
                if not a["connections"]:
                    tools = [t for t in tools if t != "connections.http_get"]
                a = {**a, "tools": tools}
            if "write_connections" in a:  # write access implies the http_write tool
                tools = list(a.get("tools", ag.get("tools", [])))
                if a["write_connections"] and "connections.http_write" not in tools:
                    tools.append("connections.http_write")
                if not a["write_connections"]:
                    tools = [t for t in tools if t != "connections.http_write"]
                a = {**a, "tools": tools}
            for k in ("description", "tools", "model", "max_turns", "skills", "reads", "writes",
                      "connections", "write_connections", "autonomy", "inputs", "outputs", "join",
                      "max_runs", "icon"):
                if k in a:
                    ag[k] = a[k]
            if "prompt" in a:
                if ag.get("prompt_file"):
                    d.prompts[ag["prompt_file"]] = a["prompt"]
                else:
                    ag["prompt"] = a["prompt"]
            return d._summary(f"Agent {name} {'created' if new else 'updated'}")

        @tool("set_gate", "Creates or updates a gate: kind 'human' (human approval, resumable) "
              "or 'stop' (halt); label is the text shown.",
              _schema({"name": _STR, "kind": {"type": "string", "enum": ["human", "stop"]},
                       "label": _STR}, ["name", "kind", "label"]))
        async def set_gate(a: dict) -> dict:
            if not SLUG.fullmatch(a["name"]):
                return err("invalid name: lowercase letters, digits and _, starting with a letter")
            if a["name"] in (d.spec.get("agents") or {}):
                return err(f"an agent {a['name']!r} already exists")
            d.spec.setdefault("gates", {})[a["name"]] = {"kind": a["kind"], "label": a["label"]}
            return d._summary(f"Gate {a['name']} defined")

        @tool("add_edge", "Connects two nodes (agent or gate). 'if' is an expression on the "
              "inputs/outputs, checked by the studio (e.g. triage.severity == \"high\", "
              "tests.failed > 0, approve_pr.decision == \"approve\"); 'when' is a condition in "
              "natural language. Prefer 'if' when the data is in an output. If the edge already "
              "exists, it updates the conditions.",
              _schema({"from": _STR, "to": _STR, "when": _STR, "if": _STR}, ["from", "to"]))
        async def add_edge(a: dict) -> dict:
            missing = [n for n in (a["from"], a["to"]) if n not in d._nodes()]
            if missing:
                return err(f"nodes that do not exist: {missing}")
            for e in d._edges():
                if e["from"] == a["from"] and e["to"] == a["to"]:
                    e["when"] = a.get("when", e.get("when", ""))
                    if "if" in a:
                        e["if"] = a["if"]
                    return d._summary("Conditions updated")
            edge = {"from": a["from"], "to": a["to"], "when": a.get("when", "")}
            if a.get("if"):
                edge["if"] = a["if"]
            d._edges().append(edge)
            return d._summary(f"Edge {a['from']} → {a['to']} created")

        @tool("remove_edge", "Removes the edge between two nodes.",
              _schema({"from": _STR, "to": _STR}, ["from", "to"]))
        async def remove_edge(a: dict) -> dict:
            before = len(d._edges())
            d.spec["edges"] = [e for e in d._edges()
                               if not (e["from"] == a["from"] and e["to"] == a["to"])]
            if len(d.spec["edges"]) == before:
                return err("that edge does not exist")
            return d._summary("Edge removed")

        @tool("rename_node", "Renames an agent or gate (updates edges, start and positions).",
              _schema({"old": _STR, "new": _STR}, ["old", "new"]))
        async def rename_node(a: dict) -> dict:
            old, new = a["old"], a["new"]
            if old not in d._nodes():
                return err(f"{old!r} does not exist")
            if not SLUG.fullmatch(new) or new in d._nodes():
                return err(f"name {new!r} is invalid or already used")
            for key in ("agents", "gates", "layout"):
                if d.spec.get(key) and old in d.spec[key]:
                    d.spec[key] = {(new if k == old else k): v for k, v in d.spec[key].items()}
            if d.spec.get("start") == old:
                d.spec["start"] = new
            for e in d._edges():
                e["from"] = new if e["from"] == old else e["from"]
                e["to"] = new if e["to"] == old else e["to"]
                if e.get("if"):
                    e["if"] = _rename_refs(e["if"], old, new)
            for ag in (d.spec.get("agents") or {}).values():
                for i in (ag.get("inputs") or {}).values():
                    if isinstance(i, dict) and isinstance(i.get("from"), str):
                        i["from"] = _rename_refs(i["from"], old, new)
            return d._summary(f"{old} renamed to {new}")

        @tool("remove_node", "Removes an agent or gate and its edges.",
              _schema({"name": _STR}, ["name"]))
        async def remove_node(a: dict) -> dict:
            n = a["name"]
            if n not in d._nodes():
                return err(f"{n!r} does not exist")
            if n == d.spec.get("start"):
                return err("it is the start node; change the start (update_flow start) first")
            for key in ("agents", "gates", "layout"):
                (d.spec.get(key) or {}).pop(n, None)
            d.spec["edges"] = [e for e in d._edges() if n not in (e["from"], e["to"])]
            return d._summary(f"{n} removed")

        @tool("set_orchestrator_prompt", "Replaces the orchestrator prompt (role and rules; "
              "the flow section is generated).", _schema({"prompt": _STR}, ["prompt"]))
        async def set_orchestrator_prompt(a: dict) -> dict:
            orch = d.spec.setdefault("orchestrator", {})
            if orch.get("prompt_file"):
                d.prompts[orch["prompt_file"]] = a["prompt"]
            else:
                orch["prompt"] = a["prompt"]
            return d._summary("Orchestrator prompt updated")

        @tool("set_mcp",
              "Declares or changes an external MCP. type stdio (command, args, env) or http/sse "
              "(https url, headers). env/headers only with ${VAR}. tools: exact names of the "
              "allowed read tools. It stays unapproved: the user ticks Approved and Read-only in "
              "the panel.",
              _schema({"name": _STR, "type": {"type": "string", "enum": ["stdio", "http", "sse"]},
                       "description": _STR, "command": _STR, "args": _STRS,
                       "env": {"type": "object", "additionalProperties": _STR}, "url": _STR,
                       "headers": {"type": "object", "additionalProperties": _STR},
                       "tools": _STRS}, ["name", "type", "tools"]))
        async def set_mcp(a: dict) -> dict:
            if not SLUG.fullmatch(a["name"]):
                return err("invalid name: lowercase letters, digits and _, starting with a letter")
            cfg = {k: a[k] for k in ("type", "description", "command", "args", "env", "url",
                                     "headers", "tools") if k in a}
            # approval is always human: any change requires a new review
            cfg |= {"approved": False, "read_only": False}
            d.spec.setdefault("mcp_servers", {})[a["name"]] = cfg
            return d._summary(f"MCP {a['name']} declared (to be approved in the flow panel)")

        @tool("validate", "Validates the draft with the runtime rules and returns the problems.",
              _schema({}))
        async def validate(_: dict) -> dict:
            v = d.validation()
            return ok({"valid": v["ok"], "problems": [e["msg"] for e in v["errors"]]})

        return [view_flow, view_catalog, view_prompt, update_flow, set_agent, set_gate,
                add_edge, remove_edge, rename_node, remove_node, set_orchestrator_prompt,
                set_mcp, validate]


READ_ONLY_TOOLS = {"view_flow", "view_catalog", "view_prompt", "validate"}


def normalize_lang(lang: str | None) -> str:
    """UI language code ("pt" | "en" | "es"); anything else falls back to English."""
    return lang if lang in LANGS else DEFAULT_LANG


def system_prompt(lang: str | None = None) -> str:
    """The assistant prompt, ending with the language to use with the user."""
    name = LANGS[normalize_lang(lang)]
    return (f"{PROMPT.rstrip()}\n\n## Language\n"
            f"Reply to the user in {name}. Flow content that you write (prompts, descriptions, "
            "labels, conditions) follows what the user asks; if the user does not say, keep the "
            "language that the flow already uses.\n")


def build_options(draft: Draft, session_id: str | None,
                  lang: str | None = None) -> ClaudeAgentOptions:
    tools = draft.tools()
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


async def chat(message: str, flow: str, spec: dict[str, Any], prompts: dict[str, str],
               session_id: str | None = None,
               lang: str | None = None) -> AsyncIterator[dict[str, Any]]:
    """Events: text, tool, done (with the draft and the validation) or error."""
    draft = Draft(spec, prompts)
    prompt = f"[Flow open in the editor: {flow}]\n\n{message}"
    try:
        async for msg in query(prompt=prompt, options=build_options(draft, session_id, lang)):
            if isinstance(msg, AssistantMessage):
                for block in msg.content:
                    if isinstance(block, TextBlock) and block.text.strip():
                        yield {"type": "text", "text": block.text}
                    elif isinstance(block, ToolUseBlock):
                        name = block.name.removeprefix(f"mcp__{SERVER}__")
                        arg = block.input.get("name") or block.input.get("node") or (
                            f"{block.input.get('from')} → {block.input.get('to')}"
                            if "from" in block.input else "")
                        yield {"type": "tool", "name": name, "arg": str(arg)[:60],
                               "edit": name not in READ_ONLY_TOOLS}
            elif isinstance(msg, ResultMessage):
                yield {"type": "done", "session_id": msg.session_id, "cost": msg.total_cost_usd,
                       "turns": msg.num_turns, "is_error": msg.is_error,
                       "changed": draft.changed, "doc": draft.doc() if draft.changed else None,
                       "validation": draft.validation()}
    except Exception as e:  # noqa: BLE001 — the editor shows the error; do not hang the stream
        yield {"type": "error", "message": f"{type(e).__name__}: {e}"}


def encode(event: dict[str, Any]) -> bytes:
    return (json.dumps(event, ensure_ascii=False) + "\n").encode("utf-8")

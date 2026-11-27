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
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from typing import Any

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ResultError,
    ResultMessage,
    TextBlock,
    ToolUseBlock,
    create_sdk_mcp_server,
    query,
    tool,
)

from ..builder.catalog import BUILTIN_TOOLS, CONTRACTS
from ..builder.compile import DISALLOWED
from ..builder.library import LibraryError, agent_data, copy_into, list_agents, refresh_copy
from ..builder.library import check as check_library_agent
from ..claude_sessions import mark_ours
from ..config import settings
from ..hooks import DELEGATION_TOOLS, make_guard, make_hooks
from ..providers import summary as provider_summary
from ..tools.common import err, ok
from .validation import _validate

SERVER = "flow_builder"
PROMPT = (Path(__file__).parent / "assistant.md").read_text(encoding="utf-8")
SLUG = re.compile(r"^[a-z][a-z0-9_]{0,39}$")
MAX_TURNS = 60  # model turns per message; at the limit the chat offers to continue
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
    "accept": _STRS, "contract": _STR, "draft": {"type": "boolean"},
    "show_if": _STR,  # flow inputs: shown only when this expression on earlier inputs holds
    # flow inputs: the output of another flow that usually gives the value ("Fill from a run")
    "source": {"type": "object", "properties": {"flow": _STR, "output": _STR}}}}
_IOMAP = {"type": "object", "additionalProperties": _IOTYPE}
_ACTION = {"type": "object", "properties": {
    "connector": {"type": "string"}, "name": {"type": "string"},
    "params": {"type": "object", "additionalProperties": {"type": "string"}},
    "outputs": {"type": "object", "additionalProperties": {
        "type": "object", "properties": {"from": {"type": "string"}, "match": {"type": "string"}}}},
}, "required": ["connector", "name"]}
_SUBFLOW = {"type": "object", "properties": {
    "flow": {"type": "string"},
    "inputs": {"type": "object", "additionalProperties": {"type": "string"}},
    "outputs": {"type": "object", "additionalProperties": {
        "type": "object", "properties": {"from": {"type": "string"}}, "required": ["from"]}},
}, "required": ["flow"]}
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


def _last_tests(name: str) -> dict[str, Any]:
    """The values each action of a connector returned in its last Test (Connectors window),
    cut short: enough to choose an output's `from` and `match`."""
    from .server import _read_json_file, _tests_file

    out: dict[str, Any] = {}
    for action, t in _read_json_file(_tests_file(name)).items():
        result = t.get("result") if isinstance(t, dict) and t.get("ok") else None
        values = result.get("values") if isinstance(result, dict) else None
        if isinstance(values, dict):
            out[action] = {k: str(v)[:80] for k, v in list(values.items())[:20]}
    return out


def _connections_info() -> dict[str, Any]:
    from ..connections import store

    try:
        out: dict[str, Any] = {}
        for n, c in store.load().items():
            out[n] = {"summary": f"{c.type} · {c.environment} · {c.access} · {c.description}"
                      .strip(" ·")}
            if c.instructions.strip():  # how to use it (agents with this connector get the same)
                out[n]["instructions"] = c.instructions.strip()[:4000]
            if c.actions:  # named operations: agents with the connector get run_action for them
                out[n]["actions"] = {k: f"{x.access}: {x.description} (params: "
                                        f"{', '.join(x.params) or '—'})" for k, x in c.actions.items()}
                out[n]["actions_only"] = c.actions_only
                seen = _last_tests(n)
                if seen:  # what each action returned in its last Test (to map outputs)
                    out[n]["last_test_values"] = seen
            out[n]["direct_requests"] = c.type != "website" and not c.actions_only
        return out
    except store.ConnectorConfigError as e:
        return {"error": str(e)}


class Draft:
    """Flow draft ({spec, prompts}) and the tools that change it."""

    def __init__(self, spec: dict[str, Any], prompts: dict[str, str]):
        self.spec = copy.deepcopy(spec)
        self.prompts = dict(prompts)
        self.changed = False
        # agents proposed for the library (id → proposal); the user saves them from the chat
        self.library_proposals: dict[str, dict[str, Any]] = {}

    # --- helpers
    def doc(self) -> dict[str, Any]:
        return {"spec": self.spec, "prompts": self.prompts}

    def validation(self) -> dict[str, Any]:
        r = _validate(self.spec, self.prompts)
        return {"ok": r["ok"], "errors": r["errors"], "warnings": r.get("warnings", []),
                "flow_section": r.get("flow_section"), "allowed_tools": r.get("allowed_tools")}

    def _summary(self, what: str) -> dict:
        self.changed = True
        v = self.validation()
        state = "flow valid" if v["ok"] else "problems: " + "; ".join(e["msg"] for e in v["errors"])
        warn = "".join(f" WARNING: {w['msg']}." for w in v["warnings"])
        return ok(f"{what}. State: {state}.{warn}")

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
                       "valid": v["ok"], "problems": [e["msg"] for e in v["errors"]],
                       "warnings": [w["msg"] for w in v["warnings"]]})

        @tool("view_catalog", "Built-in tools, contracts (with fields) and available models.",
              _schema({}))
        async def view_catalog(_: dict) -> dict:
            return ok({
                "tools": {t.id: t.description for t in BUILTIN_TOOLS.values()},
                "contracts": {c: _contract_fields(c) for c in CONTRACTS},
                "models": ["inherit", "sonnet", "opus", "haiku"]
                + [f"{pv['name']}/{m}" for pv in provider_summary() if not pv.get("error")
                   for m in pv["models"]],
                "providers": [{k: pv.get(k) for k in ("name", "kind", "title", "key", "priced")}
                              for pv in provider_summary() if not pv.get("error")],
                "model_rules": "A model `<provider>/<model>` runs on that provider of this "
                               "machine; without a prefix it runs on the default sign-in. Only "
                               "use a provider listed here, and only when the person asks for "
                               "it. Orchestrator flows: all agents on the flow's provider.",
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
                       "autonomy": {"type": "string", "enum": ["approve", "approve_prod"]},
                       "use_context": {"type": "boolean"},
                       "context_update": {"type": "string",
                                          "enum": ["suggest", "always", "never"]}}))
        async def update_flow(a: dict) -> dict:
            for k in ("title", "description", "model", "max_turns", "task", "start", "autonomy",
                      "execution", "max_parallel", "inputs", "use_context", "context_update"):
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
              "join ('all'|'any') and max_runs only matter in graph execution. "
              "action: makes it an ACTION NODE (graph execution only): no prompt, no model — the "
              "studio runs one read action {connector, name, params: {param: text with "
              "{expression} parts}, outputs: {output: {from: value name in the result, match: "
              "regex whose first group is the value}}}; declare the typed outputs too. Use "
              "'previous.<agent>.<output>' in 'from' or expressions for the last finished run. "
              "subflow: makes it a SUBFLOW NODE (graph execution only): another flow of the "
              "workspace runs as one step {flow, inputs: {child input: text with {expression} "
              "parts}, outputs: {output: {from: '<child agent>.<output>' or 'message' (the "
              "child's stop message)}}}; the child must be graph mode without human gates; "
              "declare the typed outputs too (see view_workspace for the other flows).",
              _schema({"name": _STR, "description": _STR, "prompt": _STR, "tools": _STRS,
                       "model": _STR, "max_turns": {"type": "integer"}, "skills": _STRS,
                       "reads": _STRS, "writes": _WRITES, "connections": _STRS,
                       "write_connections": _STRS, "inputs": _AGENT_INPUTS, "outputs": _IOMAP,
                       "join": {"type": "string", "enum": ["all", "any"]},
                       "max_runs": {"type": "integer"}, "icon": _STR, "action": _ACTION,
                       "subflow": _SUBFLOW,
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
            if a.get("action") or a.get("subflow"):  # the studio runs it: no prompt or tools
                ag.pop("prompt", None)
                d.prompts.pop(ag.pop("prompt_file", "") or "", None)
                for k in ("tools", "model", "max_turns", "skills", "connections"):
                    ag.pop(k, None)
                a = {k: v for k, v in a.items() if k not in ("prompt", "tools", "connections")}
            elif new:
                ag["prompt_file"] = f"prompts/{name}.md"
                d.prompts.setdefault(ag["prompt_file"], a.get("prompt") or "")
            if "connections" in a:  # direct-request connectors imply the read tool (websites and
                # "only these actions" are used through run_action, given with the connector)
                info = _connections_info()
                direct = [c for c in a["connections"]
                          if isinstance(info.get(c), dict) and info[c].get("direct_requests")]
                tools = [t for t in a.get("tools", ag.get("tools", []))
                         if t != "connections.http_get"]
                if direct:
                    tools.append("connections.http_get")
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
                      "max_runs", "icon", "action", "subflow"):
                if k in a:
                    ag[k] = a[k]
            if "prompt" in a:
                if ag.get("prompt_file"):
                    d.prompts[ag["prompt_file"]] = a["prompt"]
                else:
                    ag["prompt"] = a["prompt"]
            return d._summary(f"Agent {name} {'created' if new else 'updated'}")

        @tool("set_gate", "Creates or updates a gate: kind 'human' (human approval, resumable), "
              "'stop' (halt) or 'decision' (no pause: the first outgoing edge whose 'if' holds is "
              "followed, in edge order; an edge without 'if' is 'otherwise'). label is the text "
              "shown (for a decision, its question). A stop can have a 'message': the result "
              "shown at the end, with {expression} parts filled in from the values, e.g. "
              "\"{counter.count - previous.counter.count} new users\" (previous.<agent>.<output> "
              "= the last finished run).",
              _schema({"name": _STR, "kind": {"type": "string",
                                                "enum": ["human", "stop", "decision"]},
                       "label": _STR, "message": _STR}, ["name", "kind", "label"]))
        async def set_gate(a: dict) -> dict:
            if not SLUG.fullmatch(a["name"]):
                return err("invalid name: lowercase letters, digits and _, starting with a letter")
            if a["name"] in (d.spec.get("agents") or {}):
                return err(f"an agent {a['name']!r} already exists")
            gate = {"kind": a["kind"], "label": a["label"]}
            if a.get("message") and a["kind"] == "stop":
                gate["message"] = a["message"]
            d.spec.setdefault("gates", {})[a["name"]] = gate
            return d._summary(f"Gate {a['name']} defined")

        @tool("add_edge", "Connects two nodes (agent or gate). 'if' is an expression on the "
              "inputs/outputs, checked by the studio (e.g. triage.severity == \"high\", "
              "tests.failed > 0, approve_pr.decision == \"approve\"); 'when' is a condition in "
              "natural language. Prefer 'if' when the data is in an output. 'label' is the "
              "name shown on the canvas (e.g. yes / no on the branches of a decision; decision "
              "branches use only 'if', and one without 'if' is 'otherwise'). If the edge already "
              "exists, it updates the conditions.",
              _schema({"from": _STR, "to": _STR, "when": _STR, "if": _STR, "label": _STR},
                      ["from", "to"]))
        async def add_edge(a: dict) -> dict:
            missing = [n for n in (a["from"], a["to"]) if n not in d._nodes()]
            if missing:
                return err(f"nodes that do not exist: {missing}")
            for e in d._edges():
                if e["from"] == a["from"] and e["to"] == a["to"]:
                    e["when"] = a.get("when", e.get("when", ""))
                    if "if" in a:
                        e["if"] = a["if"]
                    if "label" in a:
                        e["label"] = a["label"]
                    return d._summary("Conditions updated")
            edge = {"from": a["from"], "to": a["to"], "when": a.get("when", "")}
            if a.get("if"):
                edge["if"] = a["if"]
            if a.get("label"):
                edge["label"] = a["label"]
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

        @tool("view_library",
              "The agent library: generic agents that can be copied into the flow and tuned "
              "there. Without 'id': a summary of each one (title, description, tools, inputs, "
              "outputs). With 'id': that agent in full, prompt included.",
              _schema({"id": _STR}))
        async def view_library(a: dict) -> dict:
            try:
                lib = list_agents()
            except LibraryError as e:
                return err(str(e))
            if a.get("id"):
                la = lib.get(a["id"])
                if la is None:
                    return err(f"library agent {a['id']!r} does not exist; see view_library")
                return ok({"id": la.id, "title": la.title, "source": la.source,
                           "agent": agent_data(la)})
            return ok({x.id: {"title": x.title, "source": x.source,
                              "description": " ".join(x.spec.description.split()),
                              "tools": x.spec.tools,
                              "inputs": {n: i.type or "text" for n, i in x.spec.inputs.items()},
                              "outputs": {n: o.type for n, o in x.spec.outputs.items()}}
                       for x in lib.values()})

        @tool("add_library_agent",
              "Copies a library agent into the flow as a new agent (name: default the library "
              "id). The copy remembers its origin; tune it afterwards with set_agent (the editor "
              "then shows it as changed in this flow). Link its inputs with set_agent "
              "(inputs: {x: {from: ...}}) and connect it with add_edge.",
              _schema({"id": _STR, "name": _STR}, ["id"]))
        async def add_library_agent(a: dict) -> dict:
            try:
                la = list_agents().get(a["id"])
                if la is None:
                    return err(f"library agent {a['id']!r} does not exist; see view_library")
                if a.get("name") in (d.spec.get("gates") or {}):
                    return err(f"a gate {a['name']!r} already exists")
                node = copy_into(d.spec, d.prompts, la, a.get("name") or None)
            except LibraryError as e:
                return err(str(e))
            return d._summary(f"Agent {node} copied from the library agent {la.id}")

        @tool("update_from_library",
              "Replaces a copied agent with the current version of its library agent (it must "
              "have an origin). Keeps where each input comes from, join, max_runs and the icon; "
              "changes made in this flow to its prompt, tools, outputs… are lost, so ask the "
              "user first when the agent was changed here.",
              _schema({"name": _STR}, ["name"]))
        async def update_from_library(a: dict) -> dict:
            ag = (d.spec.get("agents") or {}).get(a["name"])
            origin = ag.get("origin") if isinstance(ag, dict) else None
            if not isinstance(origin, dict):
                return err(f"agent {a['name']!r} was not copied from the library")
            try:
                la = list_agents().get(origin.get("agent", ""))
                if la is None:
                    return err(f"library agent {origin.get('agent')!r} no longer exists")
                refresh_copy(d.spec, d.prompts, a["name"], la)
            except LibraryError as e:
                return err(str(e))
            return d._summary(f"Agent {a['name']} updated from the library agent {la.id}")

        @tool("propose_library_agent",
              "Proposes an agent for the user's library, to create a new one or to edit one. "
              "Start from scratch, from an agent of this flow (from_agent) or from a library "
              "agent (from_library); the other fields change that base. Editing a local library "
              "agent = the same id; built-in and extension agents cannot be changed (use a new "
              "id). Nothing is saved: the chat shows the proposal and the user saves it. Library "
              "inputs have no 'from'.",
              _schema({"id": _STR, "title": _STR, "from_agent": _STR, "from_library": _STR,
                       "description": _STR, "prompt": _STR, "tools": _STRS, "model": _STR,
                       "max_turns": {"type": "integer"}, "skills": _STRS, "writes": _WRITES,
                       "inputs": _AGENT_INPUTS, "outputs": _IOMAP, "icon": _STR,
                       "autonomy": {"type": "string", "enum": ["approve", "approve_prod"]}},
                      ["id"]))
        async def propose_library_agent(a: dict) -> dict:
            base: dict[str, Any] = {}
            prompt = ""
            title = a.get("title") or ""
            try:
                if a.get("from_agent") and a.get("from_library"):
                    return err("give either from_agent or from_library")
                if a.get("from_agent"):
                    src = (d.spec.get("agents") or {}).get(a["from_agent"])
                    if not isinstance(src, dict):
                        return err(f"agent {a['from_agent']!r} does not exist in the flow")
                    base = copy.deepcopy(src)
                    prompt = (d.prompts.get(src["prompt_file"], "") if src.get("prompt_file")
                              else src.get("prompt") or "")
                elif a.get("from_library"):
                    la = list_agents().get(a["from_library"])
                    if la is None:
                        return err(f"library agent {a['from_library']!r} does not exist")
                    base = {k: v for k, v in agent_data(la).items() if k != "prompt"}
                    prompt, title = la.prompt, title or la.title
                for k in ("description", "tools", "model", "max_turns", "skills", "writes",
                          "inputs", "outputs", "icon", "autonomy"):
                    if k in a:
                        base[k] = a[k]
                prompt = a.get("prompt", prompt)
                c = check_library_agent(a["id"], title, base, prompt)
            except LibraryError as e:
                return err(str(e))
            d.library_proposals[c.id] = {
                "id": c.id, "title": c.title, "exists": c.exists, "notes": c.notes,
                "agent": c.data, "prompt": c.prompt}
            what = "replaces the local library agent" if c.exists else "new library agent"
            notes = f" Notes: {'; '.join(c.notes)}." if c.notes else ""
            return ok(f"Proposal {c.id!r} ready ({what}). The user saves it with the button in "
                      f"the chat; it is not saved yet.{notes}")

        @tool("view_workspace", "The studio workspace this flow is in: its variables "
              "({vars.x}), roots ({roots.x}, workspace: {root: x}), environments (names and "
              "kind), and its other flows with their inputs (and where each usually comes from) "
              "and outputs — to use a flow as a subflow, or to give an input a `source` hint.",
              _schema({}))
        async def view_workspace(_: dict) -> dict:
            return ok(_workspace_info(d.spec.get("name")))

        @tool("validate", "Validates the draft with the runtime rules and returns the problems.",
              _schema({}))
        async def validate(_: dict) -> dict:
            v = d.validation()
            return ok({"valid": v["ok"], "problems": [e["msg"] for e in v["errors"]],
                       "warnings": [w["msg"] for w in v["warnings"]]})

        return [view_flow, view_catalog, view_prompt, update_flow, set_agent, set_gate,
                add_edge, remove_edge, rename_node, remove_node, set_orchestrator_prompt,
                set_mcp, view_library, add_library_agent, update_from_library,
                propose_library_agent, view_workspace, validate]


READ_ONLY_TOOLS = {"view_flow", "view_catalog", "view_prompt", "view_library", "view_workspace",
                   "validate"}


def _workspace_info(this_flow: str | None) -> dict[str, Any]:
    """Variables, roots, environments and the other flows of the open studio workspace."""
    import yaml

    from .. import workspaces
    from ..builder.compile import flow_locations
    from ..builder.spec import FlowSpec

    ws = workspaces.current()
    out: dict[str, Any] = {"workspace": ws.name, "environment": workspaces.current_env()}
    if ws.spec is not None:
        from .. import context
        # what the agents of the workspace's flows already get in their prompts
        out["context"] = context.context_text(ws) or "(none yet: the person can explore the roots)"
        out["vars"] = {"base": ws.spec.vars,
                       "per_environment": {n: e.vars for n, e in ws.spec.environments.items()}}
        out["environments"] = {n: e.kind for n, e in ws.spec.environments.items()}
        out["roots"] = {n: r.path for n, r in ws.spec.roots.items()}
    else:
        out["note"] = ("the studio's own workspace: no variables, roots or environments (the "
                       "person can create a workspace in the editor)")
    flows: dict[str, Any] = {}
    for _, base in flow_locations():
        for f in sorted(base.glob("*/flow.yaml")):
            name = f.parent.name
            if name == this_flow or name in flows:
                continue
            try:
                raw = yaml.safe_load(f.read_text(encoding="utf-8")) or {}
                spec = FlowSpec.model_validate(workspaces.substitute(raw)[0])
            except (yaml.YAMLError, ValueError):
                flows[name] = {"valid": False}
                continue
            flows[name] = {
                "title": spec.title, "execution": spec.execution,
                "human_gates": sorted(g for g, x in spec.gates.items() if x.kind == "human"),
                "inputs": {k: {"type": t.type, **({"source": t.source.model_dump()}
                                                  if t.source else {})}
                           for k, t in spec.inputs.items()},
                "outputs": {f"{an}.{on}": o.type for an, a in spec.agents.items()
                            for on, o in a.outputs.items()},
            }
    out["flows"] = flows
    return out


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


def with_command(header: str, message: str, command: Any = None) -> str:
    """The user turn: header, the instructions of a saved command (if one is used), message."""
    if command is None or not command.instructions.strip():
        return f"{header}\n\n{message}"
    return (f"{header}\n\n[Saved command {command.name!r}: instructions the user saved with it]\n"
            f"{command.instructions.strip()}\n[End of the command instructions]\n\n{message}")


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


async def stream(prompt: str, options: ClaudeAgentOptions, server: str, read_only: set[str],
                 done: Callable[[ResultMessage | None], dict[str, Any]]) -> AsyncIterator[dict[str, Any]]:
    """The events of one assistant message (both assistants): text, tool, turn (how many model
    turns so far, of `max_turns`), done (with `done(result)`) or error. At the turn limit the done
    event says `limit: true` (the session can go on with another message) instead of an error."""
    turns: set[str] = set()
    limit = options.max_turns or 0
    finished = False
    try:
        async for msg in query(prompt=prompt, options=options):
            if isinstance(msg, AssistantMessage):
                key = msg.message_id or msg.uuid or str(len(turns))
                if key not in turns and msg.parent_tool_use_id is None:
                    turns.add(key)
                    yield {"type": "turn", "n": len(turns), "max": limit}
                for block in msg.content:
                    if isinstance(block, TextBlock) and block.text.strip():
                        yield {"type": "text", "text": block.text}
                    elif isinstance(block, ToolUseBlock):
                        name = block.name.removeprefix(f"mcp__{server}__")
                        arg = (block.input.get("name") or block.input.get("node")
                               or block.input.get("id") or block.input.get("section") or (
                                   f"{block.input.get('from')} → {block.input.get('to')}"
                                   if "from" in block.input else ""))
                        yield {"type": "tool", "name": name, "arg": str(arg)[:60],
                               "edit": name not in read_only}
            elif isinstance(msg, ResultMessage):
                at_limit = msg.subtype == "error_max_turns"
                finished = True
                mark_ours(msg.session_id, getattr(options, "cwd", None))
                yield {"type": "done", "session_id": msg.session_id, "cost": msg.total_cost_usd,
                       "turns": msg.num_turns, "max_turns": limit,
                       "is_error": msg.is_error and not at_limit, "limit": at_limit,
                       **done(msg)}
    except ResultError as e:  # the CLI exits non-zero after an error result it already sent
        at_limit = e.subtype == "error_max_turns" or e.terminal_reason == "max_turns"
        if at_limit and not finished and e.session_id:  # the result came only in the error
            yield {"type": "done", "session_id": e.session_id, "cost": None, "turns": limit,
                   "max_turns": limit, "is_error": False, "limit": True, **done(None)}
        elif not at_limit:
            yield {"type": "error", "message": f"{type(e).__name__}: {e}"}
    except Exception as e:  # noqa: BLE001 — the editor shows the error; do not hang the stream
        yield {"type": "error", "message": f"{type(e).__name__}: {e}"}


async def chat(message: str, flow: str, spec: dict[str, Any], prompts: dict[str, str],
               session_id: str | None = None,
               lang: str | None = None, command: Any = None) -> AsyncIterator[dict[str, Any]]:
    """Events: text, tool, turn, done (with the draft and the validation) or error. `command` is a
    saved command (editor/commands.py) whose instructions go with the message."""
    draft = Draft(spec, prompts)
    prompt = with_command(f"[Flow open in the editor: {flow}]", message, command)
    async for ev in stream(prompt, build_options(draft, session_id, lang), SERVER, READ_ONLY_TOOLS,
                           lambda _: {"changed": draft.changed,
                                      "doc": draft.doc() if draft.changed else None,
                                      "library": list(draft.library_proposals.values()),
                                      "validation": draft.validation()}):
        yield ev


def encode(event: dict[str, Any]) -> bytes:
    return (json.dumps(event, ensure_ascii=False) + "\n").encode("utf-8")

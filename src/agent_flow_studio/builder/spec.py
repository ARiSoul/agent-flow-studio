"""Format of a flow (flows/<name>/flow.yaml) and its validation rules.

A flow is a graph: `agents` nodes (subagents) and `gates` (human approval or stop), linked by
`edges` with a condition: an expression (`if`, evaluated by the studio) and/or text (`when`).
The flow declares typed `inputs`; each agent declares typed `inputs` (from a flow input or from
an earlier agent's output) and typed `outputs`. `execution` chooses who runs the graph: the LLM
orchestrator (`orchestrator`, it receives the graph as instructions) or the studio's engine
(`graph`, parallel branches, joins, conditions).
"""
from __future__ import annotations

import re
from collections import deque
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from ..messages import MsgError, msg, reason
from . import expr
from .catalog import BUILTIN_TOOLS, CONTRACTS
from .types import IOKind, IOType, check_type, compatible

SLUG = r"^[a-z][a-z0-9_]{0,39}$"
_SLUG_RE = re.compile(SLUG)
_ARTIFACT_RE = re.compile(r"^[A-Za-z0-9_.-]{1,80}$")
_ENV_REF = re.compile(r"^\$\{[A-Z_][A-Z0-9_]*\}$")
_ENV_IN = re.compile(r"\$\{([A-Z_][A-Z0-9_]*)\}")
_TASK_VAR = re.compile(r"\{([a-z_][a-z0-9_]*)\}")
LEGACY_INPUT = "input"  # the one implicit input of flows without `inputs`
RESERVED_NAMES = {"input", "previous"}  # roots of references: not names of agents or gates


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)


class ArtifactSpec(_Strict):
    name: str
    contract: str | None = None  # name in catalog.CONTRACTS (.json only)
    draft: bool = False  # .md draft for human approval (marked automatically)

    @model_validator(mode="after")
    def _check(self) -> ArtifactSpec:
        if not _ARTIFACT_RE.fullmatch(self.name) or ".." in self.name:
            raise MsgError(msg("spec.artifact_name", "invalid artifact name: {name}",
                               name=repr(self.name)))
        if self.contract and self.contract not in CONTRACTS:
            raise MsgError(msg("spec.unknown_contract", "unknown contract {contract}; available: {available}",
                               contract=repr(self.contract), available=", ".join(CONTRACTS)))
        if self.contract and not self.name.endswith(".json"):
            raise MsgError(msg("spec.contract_json", "{name}: contracts only apply to .json",
                               name=self.name))
        if self.draft and not self.name.endswith(".md"):
            raise MsgError(msg("spec.draft_md", "{name}: drafts must be .md", name=self.name))
        return self


class AgentInput(_Strict):
    """An agent input: `from` a flow input (`input.<name>`), an agent output (`<agent>.<out>`), the
    same output in the last finished run of the flow (`previous.<agent>.<out>`: empty on the first
    run, so never missing), or free (no `from`: given by the orchestrator, or in the editor when testing the agent).
    Without `type`, it takes the type of its source (text when free)."""

    from_: str | None = Field(default=None, alias="from")
    type: IOKind | None = None
    description: str = ""
    required: bool = True

    @field_validator("from_")
    @classmethod
    def _ref(cls, v: str | None) -> str | None:
        name = r"[a-z][a-z0-9_]{0,39}"
        if v is not None and not re.fullmatch(rf"(previous\.)?{name}\.{name}", v):
            raise MsgError(msg("spec.from_format",
                               "'from' must be input.<name>, <agent>.<output> or "
                               "previous.<agent>.<output>, not {value}", value=repr(v)))
        return v


# Write autonomy: approve = every write asks for approval; approve_prod = only in production;
# auto = no stops (everything stays in the audit).
Autonomy = Literal["approve", "approve_prod", "auto"]


class AgentOrigin(_Strict):
    """Where a copied agent came from: a library agent (builder/library.py) and the fingerprint
    of that library version when it was copied. Only used to show differences and updates."""

    agent: str = Field(pattern=SLUG)
    hash: str = Field(pattern=r"^[0-9a-f]{8,64}$")


class ActionOutput(_Strict):
    """Where an output of an action node comes from: a value of the action's result (`from`, a
    name or a field path such as `a.b`; default: the output's own name), and optionally `match`,
    a regular expression whose first group (or whole match) is the value, e.g. `(\\d+)\\D*$`
    takes 14 out of "1–14 of 14"."""
    from_: str | None = Field(default=None, alias="from", max_length=200)
    match: str | None = Field(default=None, max_length=200)

    @field_validator("match")
    @classmethod
    def _regex(cls, v: str | None) -> str | None:
        if v is not None:
            try:
                re.compile(v)
            except re.error as e:
                raise MsgError(msg("spec.action_match", "match: invalid regular expression: "
                                   "{reason}", reason=str(e))) from None
        return v


class ActionCall(_Strict):
    """An action node: the studio runs one read action of a connector, without a model, and saves
    the values as the node's outputs. `params` are texts with {expression} parts (flow inputs,
    outputs of earlier nodes, previous.*)."""
    connector: str = Field(pattern=r"^[A-Za-z0-9_-]{1,64}$")
    name: str = Field(pattern=r"^[a-z][a-z0-9_]{0,39}$")
    params: dict[str, str] = Field(default_factory=dict)
    outputs: dict[str, ActionOutput] = Field(default_factory=dict)


class AgentSpec(_Strict):
    description: str = Field(min_length=1)
    prompt: str | None = None
    prompt_file: str | None = None  # relative to the flow folder
    tools: list[str] = Field(default_factory=list)  # "server.tool"
    model: str | None = None  # "inherit", "sonnet", "opus", "haiku" or full ID
    skills: list[str] = Field(default_factory=list)
    max_turns: int | None = Field(default=None, ge=1, le=200)
    reads: list[str] = Field(default_factory=list)
    writes: list[ArtifactSpec] = Field(default_factory=list)
    connections: list[str] = Field(default_factory=list)  # connectors (connections.yaml) for http_get
    write_connections: list[str] = Field(default_factory=list)  # connectors for http_write
    autonomy: Autonomy | None = None  # None = the flow's
    inputs: dict[str, AgentInput] = Field(default_factory=dict)
    outputs: dict[str, IOType] = Field(default_factory=dict)
    icon: str | None = Field(default=None, max_length=16)  # editor only: built-in id or emoji
    join: Literal["all", "any"] = "all"  # graph: wait for all incoming branches, or the first
    max_runs: int = Field(default=3, ge=1, le=20)  # graph: limit for loops (back edges)
    origin: AgentOrigin | None = None  # copied from the agent library (editor only)
    action: ActionCall | None = None  # an action node (graph mode): no prompt, no model

    @field_validator("icon")
    @classmethod
    def _icon(cls, v: str | None) -> str | None:
        if v is not None and (not v.strip() or any(ord(c) < 32 for c in v)):
            raise MsgError(msg("spec.icon",
                               "icon: a built-in icon name or a short text such as an emoji"))
        return v

    @model_validator(mode="after")
    def _one_prompt(self) -> AgentSpec:
        if self.action is not None:
            if self.prompt is not None or self.prompt_file is not None or self.tools:
                raise MsgError(msg("spec.action_no_prompt",
                                   "an action node has no prompt and no tools (the studio runs "
                                   "the action)"))
            return self
        if (self.prompt is None) == (self.prompt_file is None):
            raise MsgError(msg("spec.one_prompt", "give exactly one of 'prompt' or 'prompt_file'"))
        return self


class GateSpec(_Strict):
    """human: pauses for a person · stop: ends the run · decision: no pause, takes the first
    branch (outgoing edge, in order) whose `if` holds; a branch without `if` is "otherwise"."""
    kind: Literal["human", "stop", "decision"]
    label: str = Field(min_length=1)
    # stop: the result shown at the end; the studio fills in its {expression} parts from the saved
    # values, e.g. "{checker.count - previous.checker.count} new users"
    message: str = Field(default="", max_length=2000)


class Edge(_Strict):
    from_: str = Field(alias="from")
    to: str
    when: str = ""  # condition in natural language (orchestrator, or the router in graph mode)
    if_: str = Field(default="", alias="if")  # expression on inputs/outputs (see expr.py)
    label: str = Field(default="", max_length=40)  # shown on the canvas, e.g. "yes" / "no"


class ExternalMcp(_Strict):
    """External MCP server. Only enters a flow with explicit approval and a closed tool list."""

    type: Literal["stdio", "http", "sse"]
    description: str = ""
    command: str | None = None
    args: list[str] = Field(default_factory=list)
    env: dict[str, str] = Field(default_factory=dict)  # values only "${VAR}"
    url: str | None = None
    headers: dict[str, str] = Field(default_factory=dict)  # values only with "${VAR}"
    tools: list[str] = Field(min_length=1)  # exact names of the allowed tools
    approved: bool = False  # explicit human approval to use this server
    read_only: bool = False  # declares that the listed tools only read

    @model_validator(mode="after")
    def _check(self) -> ExternalMcp:
        if self.type == "stdio" and not self.command:
            raise MsgError(msg("spec.mcp_command", "stdio MCP needs 'command'"))
        if self.type in ("http", "sse") and not (self.url and self.url.startswith("https://")):
            raise MsgError(msg("spec.mcp_url", "http/sse MCP needs an https:// 'url'"))
        for t in self.tools:
            if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", t):
                raise MsgError(msg("spec.mcp_tool_name", "invalid tool name (no wildcards): {tool}",
                                   tool=repr(t)))
        for k, v in self.env.items():
            if not _ENV_REF.fullmatch(v):
                raise MsgError(msg("spec.mcp_env",
                                   "env {key}: use a {ref} reference; secrets stay out of the flow",
                                   key=k, ref="${VAR}"))
        for k, v in self.headers.items():
            if _ENV_IN.sub("", v).strip() not in ("", "Bearer", "Basic", "Token"):
                raise MsgError(msg("spec.mcp_header",
                                   "header {key}: use {ref} references; secrets stay out of the flow",
                                   key=k, ref="${VAR}"))
        return self


class WorkspaceSpec(_Strict):
    """Repository/folder where agents with workspace.* tools work."""

    path: str = Field(min_length=1)  # absolute, or relative to the folder where the studio runs
    environment: Literal["dev", "staging", "prod"] = "dev"
    description: str = ""
    protected_branches: list[str] = Field(default_factory=lambda: ["main", "master"])


class InputSpec(_Strict):
    label: str = "Input"
    pattern: str = r"^[A-Za-z0-9_-]{1,40}$"  # also the run id (folder in runs/)


class OrchestratorSpec(_Strict):
    prompt: str | None = None
    prompt_file: str | None = None


class Position(_Strict):
    x: float
    y: float


class FlowSpec(_Strict):
    version: Literal[1] = 1
    name: str
    title: str
    description: str = ""
    model: str | None = None
    max_turns: int = Field(default=60, ge=1, le=500)
    execution: Literal["orchestrator", "graph"] = "orchestrator"
    max_parallel: int = Field(default=4, ge=1, le=16)  # graph: agents running at the same time
    inputs: dict[str, IOType] = Field(default_factory=dict)  # typed flow inputs (run form)
    input: InputSpec = Field(default_factory=InputSpec)  # legacy: one text input = run id
    task: str = "Run the flow for {input}."
    orchestrator: OrchestratorSpec = Field(default_factory=OrchestratorSpec)
    start: str
    agents: dict[str, AgentSpec]
    gates: dict[str, GateSpec] = Field(default_factory=dict)
    edges: list[Edge] = Field(default_factory=list)
    mcp_servers: dict[str, ExternalMcp] = Field(default_factory=dict)
    autonomy: Autonomy = "approve"
    workspace: WorkspaceSpec | None = None
    layout: dict[str, Position] = Field(default_factory=dict)  # positions in the editor

    @field_validator("name")
    @classmethod
    def _slug(cls, v: str) -> str:
        if not re.fullmatch(r"^[a-z0-9][a-z0-9_-]{0,39}$", v):
            raise MsgError(msg("spec.flow_name", "flow name: lowercase, digits, '-' or '_'"))
        return v

    @model_validator(mode="after")
    def _graph(self) -> FlowSpec:
        errors: list[str] = []
        for kind, names in (("agent", self.agents), ("gate", self.gates)):
            errors += [msg("spec.reserved_name", "{kind} {name}: this name is reserved (input, previous)",
                           kind=kind, name=repr(n)) for n in names if n in RESERVED_NAMES]
        for kind, names in (("agent", self.agents), ("gate", self.gates), ("mcp", self.mcp_servers)):
            errors += [msg("spec.node_name", "{kind} {name}: invalid name (use {rule})", kind=kind,
                           name=repr(n), rule=SLUG) for n in names if not _SLUG_RE.fullmatch(n)]
        clash = set(self.agents) & set(self.gates)
        if clash:
            errors.append(msg("spec.names_repeated",
                              "names repeated between agents and gates: {names}", names=sorted(clash)))
        clash = set(self.mcp_servers) & {t.server for t in BUILTIN_TOOLS.values()}
        if clash:
            errors.append(msg("spec.mcp_builtin_name",
                              "external MCP with the name of a built-in server: {names}",
                              names=sorted(clash)))
        if not self.inputs and "{input}" not in self.task:
            errors.append(msg("spec.task_input", "'task' must contain {placeholder}",
                              placeholder="{input}"))
        if self.inputs:
            known = set(self.inputs) | {"input", "run_id"}
            errors += [msg("spec.task_unknown", "'task' uses {placeholder}, which is not a flow input",
                           placeholder="{" + v + "}")
                       for v in _TASK_VAR.findall(self.task) if v not in known]
        nodes = set(self.agents) | set(self.gates)
        if self.start not in self.agents:
            errors.append(msg("spec.start", "start {start} is not an agent", start=repr(self.start)))
        for e in self.edges:
            for end in (e.from_, e.to):
                if end not in nodes:
                    errors.append(msg("spec.edge_node", "edge {edge}: node {node} does not exist",
                                      edge=f"{e.from_}→{e.to}", node=repr(end)))
        errors += self._tool_errors()
        errors += self._io_errors()
        seen: dict[str, str] = {}
        for an, a in self.agents.items():
            for w in a.writes:
                if w.name in seen:
                    errors.append(msg("spec.artifact_twice", "artifact {name} written by {first} and {second}",
                                      name=w.name, first=seen[w.name], second=an))
                seen[w.name] = an
            if a.writes and "artifacts.save_artifact" not in a.tools:
                errors.append(msg("spec.writes_no_save",
                                  "agent {agent} writes artifacts but does not have artifacts.save_artifact",
                                  agent=repr(an)))            # (connectors without named actions need http_get / http_write: checked when the
            # flow compiles, where connections.yaml and its actions are known)
            has_write = "connections.http_write" in a.tools
            if has_write and not a.write_connections:
                errors.append(msg("spec.http_write_no_conn",
                                  "agent {agent} has connections.http_write but no write connector assigned",
                                  agent=repr(an)))
            if any(t.startswith("workspace.") for t in a.tools) and self.workspace is None:
                errors.append(msg("spec.no_workspace",
                                  "agent {agent} uses workspace.* tools but the flow has no workspace",
                                  agent=repr(an)))
            errors += [msg("spec.connector_name", "agent {agent}: connector {connector} has an invalid name",
                           agent=repr(an), connector=repr(c))
                       for c in a.write_connections if not _SLUG_RE.fullmatch(c)]
            has_get = "connections.http_get" in a.tools
            if has_get and not a.connections:
                errors.append(msg("spec.http_get_no_conn",
                                  "agent {agent} has connections.http_get but no connector assigned",
                                  agent=repr(an)))
            errors += [msg("spec.connector_name", "agent {agent}: connector {connector} has an invalid name",
                           agent=repr(an), connector=repr(c))
                       for c in a.connections if not _SLUG_RE.fullmatch(c)]
        if not errors and self.start in self.agents:
            unreachable = set(self.agents) - self.reachable()
            if unreachable:
                errors.append(msg("spec.unreachable", "agents not reachable from {start}: {agents}",
                                  start=repr(self.start), agents=sorted(unreachable)))
        if errors:
            raise MsgError(*errors)
        return self

    def flow_inputs(self) -> dict[str, IOType]:
        """The typed inputs; a legacy flow has one text input `input` (its pattern applies)."""
        if self.inputs:
            return self.inputs
        return {LEGACY_INPUT: IOType(type="text", label=self.input.label, pattern=self.input.pattern)}

    def input_type(self, agent: str, name: str) -> str:
        """Resolved type of an agent input (from its source when it has no `type`)."""
        i = self.agents[agent].inputs[name]
        if i.type:
            return i.type
        src = self.source_type(i.from_) if i.from_ else None
        return src or "text"

    def source_io(self, ref: str) -> IOType | None:
        ref = ref.removeprefix("previous.")  # the same output, in the last finished run
        root, name = ref.split(".", 1)
        if root == "input":
            return self.flow_inputs().get(name)
        return self.agents[root].outputs.get(name) if root in self.agents else None

    def source_type(self, ref: str) -> str | None:
        t = self.source_io(ref)
        return t.type if t else None

    def ancestors(self, node: str) -> set[str]:
        """Nodes with a path to `node` (edges into `start` are loops and do not count)."""
        into: dict[str, list[str]] = {}
        for e in self.edges:
            if e.to != self.start:
                into.setdefault(e.to, []).append(e.from_)
        seen: set[str] = set()
        q = deque(into.get(node, []))
        while q:
            n = q.popleft()
            if n not in seen:
                seen.add(n)
                q.extend(into.get(n, []))
        return seen

    def _io_errors(self) -> list[str]:  # Msg items (str subclass)
        errors: list[str] = []
        for n, t in self.inputs.items():
            if not _SLUG_RE.fullmatch(n):
                errors.append(msg("spec.input_name", "flow input {name}: invalid name (use {rule})",
                                  name=repr(n), rule=SLUG))
            try:
                check_type(t, CONTRACTS)
            except ValueError as e:
                errors.append(msg("spec.input_reason", "flow input {name}: {reason}", name=repr(n),
                                  reason=reason(e)))
        before: list[str] = []
        for n, t in self.inputs.items():  # show_if: only on the inputs declared before it
            if t.show_if:
                try:
                    used = expr.refs(t.show_if)
                except expr.ExprError as e:
                    errors.append(msg("spec.show_if_reason", "flow input {name}: show_if: {reason}",
                                      name=repr(n), reason=reason(e)))
                    used = []
                for r in used:
                    if r[0] != "input" or len(r) < 2 or r[1] not in before:
                        errors.append(msg("spec.show_if_before",
                                          "flow input {name}: show_if can only use the flow inputs "
                                          "declared before it (input.<name>), not {ref}",
                                          name=repr(n), ref=".".join(map(str, r))))
            before.append(n)
        flow_in = self.flow_inputs()
        for an, a in self.agents.items():
            for n, t in a.outputs.items():
                if not _SLUG_RE.fullmatch(n):
                    errors.append(msg("spec.output_name", "agent {agent}: output {name} has an invalid name",
                                      agent=repr(an), name=repr(n)))
                if t.show_if:
                    errors.append(msg("spec.output_show_if",
                                      "agent {agent}, output {name}: show_if is only for flow inputs",
                                      agent=repr(an), name=repr(n)))
                try:
                    check_type(t, CONTRACTS)
                except ValueError as e:
                    errors.append(msg("spec.output_reason", "agent {agent}, output {name}: {reason}",
                                      agent=repr(an), name=repr(n), reason=reason(e)))
            for n, i in a.inputs.items():
                if not _SLUG_RE.fullmatch(n):
                    errors.append(msg("spec.agent_input_name", "agent {agent}: input {name} has an invalid name",
                                      agent=repr(an), name=repr(n)))
                if i.from_ is None:
                    continue
                root, src = i.from_.split(".", 1)
                if root == "previous":  # any agent, itself included: it ran in an earlier run
                    pa, _, po = src.partition(".")
                    if pa not in self.agents or po not in self.agents[pa].outputs:
                        errors.append(msg("spec.from_no_previous",
                                          "agent {agent}, input {name}: {source} is not an output of "
                                          "an agent of this flow", agent=repr(an), name=repr(n),
                                          source=repr(src)))
                        continue
                elif root == "input":
                    if src not in flow_in:
                        errors.append(msg("spec.from_no_input",
                                          "agent {agent}, input {name}: flow input {source} does not exist",
                                          agent=repr(an), name=repr(n), source=repr(src)))
                        continue
                elif root not in self.agents:
                    errors.append(msg("spec.from_no_agent",
                                      "agent {agent}, input {name}: agent {source} does not exist",
                                      agent=repr(an), name=repr(n), source=repr(root)))
                    continue
                elif src not in self.agents[root].outputs:
                    errors.append(msg("spec.from_no_output",
                                      "agent {agent}, input {name}: agent {source} has no output {output}",
                                      agent=repr(an), name=repr(n), source=repr(root), output=repr(src)))
                    continue
                elif root == an:
                    errors.append(msg("spec.from_self",
                                      "agent {agent}, input {name}: an agent cannot read its own output",
                                      agent=repr(an), name=repr(n)))
                    continue
                elif self.execution == "graph" and root not in self.ancestors(an):
                    errors.append(msg("spec.from_order",
                                      "agent {agent}, input {name}: {source} does not run before {agent} "
                                      "(add an edge path from {source} to {agent})",
                                      agent=repr(an), name=repr(n), source=repr(root)))
                st = self.source_type(i.from_)
                if i.type and st and not compatible(st, i.type):
                    errors.append(msg("spec.from_type", "agent {agent}, input {name}: a {source} cannot feed a {target}",
                                      agent=repr(an), name=repr(n), source=st, target=i.type))
        for e in self.edges:
            if e.if_:
                errors += [msg("spec.edge_reason", "edge {edge}: {reason}", edge=f"{e.from_}→{e.to}",
                               reason=m) for m in self._expr_errors(e.if_)]
        for an, a in self.agents.items():
            if a.action is None:
                continue
            if self.execution != "graph":
                errors.append(msg("spec.action_needs_graph",
                                  "agent {agent}: an action node runs only with execution: graph",
                                  agent=repr(an)))
            for o in a.action.outputs:
                if o not in a.outputs:
                    errors.append(msg("spec.action_output_unknown",
                                      "agent {agent}: the action maps {output}, which is not one of "
                                      "its outputs", agent=repr(an), output=repr(o)))
            for pn, pv in a.action.params.items():
                try:
                    parts = expr.template_parts(pv)
                except expr.ExprError as x:
                    parts = []
                    errors.append(msg("spec.action_param_reason",
                                      "agent {agent}, parameter {name}: {reason}", agent=repr(an),
                                      name=repr(pn), reason=reason(x)))
                for p in parts:
                    errors += [msg("spec.action_param_reason",
                                   "agent {agent}, parameter {name}: {reason}", agent=repr(an),
                                   name=repr(pn), reason=m) for m in self._expr_errors(p)]
        for gn, g in self.gates.items():
            if g.message.strip():
                if g.kind != "stop":
                    errors.append(msg("spec.message_only_stop",
                                      "gate {gate}: only a stop shows a message", gate=repr(gn)))
                try:
                    parts = expr.template_parts(g.message)
                except expr.ExprError as x:
                    parts = []
                    errors.append(msg("spec.message_reason", "gate {gate}: message: {reason}",
                                      gate=repr(gn), reason=reason(x)))
                for p in parts:
                    errors += [msg("spec.message_reason", "gate {gate}: message: {reason}",
                                   gate=repr(gn), reason=m) for m in self._expr_errors(p)]
            if g.kind == "decision":
                outs = self.outgoing(gn)
                if not outs:
                    errors.append(msg("spec.decision_no_branch", "decision {gate} has no branches",
                                      gate=repr(gn)))
                if sum(1 for e in outs if not e.if_) > 1:
                    errors.append(msg("spec.decision_else",
                                      "decision {gate}: only one branch can have no `if` (the "
                                      "\"otherwise\")", gate=repr(gn)))
                if any(e.when for e in outs):
                    errors.append(msg("spec.decision_when",
                                      "decision {gate}: its branches use `if` expressions, not "
                                      "`when` text", gate=repr(gn)))
        return errors

    def _expr_errors(self, text: str) -> list[str]:
        try:
            used = expr.refs(text)
        except expr.ExprError as e:
            return [msg("spec.condition_syntax", "condition: {reason}", reason=reason(e))]
        out = []
        for r in used:
            if len(r) < 2:
                out.append(msg("spec.condition_field", "condition: {root} needs a field (e.g. {example})",
                               root=repr(r[0]), example=f"{r[0]}.x"))
            elif r[0] == "previous":
                if len(r) < 3 or r[1] not in self.agents or r[2] not in self.agents[r[1]].outputs:
                    out.append(msg("spec.condition_previous",
                                   "condition: {ref} is not an output of an agent (use "
                                   "previous.<agent>.<output>)", ref=".".join(map(str, r))))
            elif r[0] == "input":
                if r[1] not in self.flow_inputs():
                    out.append(msg("spec.condition_input", "condition: flow input {name} does not exist",
                                   name=repr(r[1])))
            elif r[0] in self.agents:
                if r[1] not in self.agents[r[0]].outputs:
                    out.append(msg("spec.condition_output", "condition: agent {agent} has no output {output}",
                                   agent=repr(r[0]), output=repr(r[1])))
            elif r[0] in self.gates:
                if r[1] not in ("decision", "comment"):
                    out.append(msg("spec.condition_gate",
                                   "condition: a gate has only .decision and .comment, not {field}",
                                   field=repr(r[1])))
            else:
                out.append(msg("spec.condition_root",
                               "condition: {root} is not input, previous, an agent or a gate",
                               root=repr(r[0])))
        return out

    def _tool_errors(self) -> list[str]:
        errors: list[str] = []
        for an, a in self.agents.items():
            for t in a.tools:
                if "*" in t or "." not in t:
                    errors.append(msg("spec.tool_format", "{agent}: invalid tool {tool} (format server.tool, no *)",
                                      agent=an, tool=repr(t)))
                    continue
                server, name = t.split(".", 1)
                if t in BUILTIN_TOOLS:
                    continue
                ext = self.mcp_servers.get(server)
                if ext is None:
                    errors.append(msg("spec.tool_unknown",
                                      "{agent}: tool {tool} does not exist in the catalog or in the flow's MCPs",
                                      agent=an, tool=repr(t)))
                elif name not in ext.tools:
                    errors.append(msg("spec.tool_not_allowed", "{agent}: {tool} is not in the allowlist of MCP {server}",
                                      agent=an, tool=repr(name), server=repr(server)))
                elif not ext.approved:
                    errors.append(msg("spec.mcp_not_approved", "MCP {server} not approved (approved: true is required)",
                                      server=repr(server)))
                elif not ext.read_only:
                    errors.append(msg("spec.mcp_not_read_only", "MCP {server} without read_only: true (rule 1: read-only)",
                                      server=repr(server)))
        return errors

    # --- helpers used by the compiler and the editor
    def reachable(self) -> set[str]:
        out: dict[str, list[str]] = {}
        for e in self.edges:
            out.setdefault(e.from_, []).append(e.to)
        seen, q = {self.start}, deque([self.start])
        while q:
            for nxt in out.get(q.popleft(), []):
                if nxt not in seen:
                    seen.add(nxt)
                    q.append(nxt)
        return seen

    def agent_order(self) -> list[str]:
        """Topological order of the agents (direct agent→agent edges), with ties broken by the
        breadth-first traversal from start. Cycles do not block: the rest follows the traversal."""
        out: dict[str, list[str]] = {}
        for e in self.edges:
            out.setdefault(e.from_, []).append(e.to)
        bfs, seen, q = [], {self.start}, deque([self.start])
        while q:
            n = q.popleft()
            bfs.append(n)
            for nxt in out.get(n, []):
                if nxt not in seen:
                    seen.add(nxt)
                    q.append(nxt)
        rank = {n: i for i, n in enumerate(bfs)}
        agents = [n for n in bfs if n in self.agents]
        indeg = {a: 0 for a in agents}
        for e in self.edges:
            if e.from_ in indeg and e.to in indeg and e.to != self.start:
                indeg[e.to] += 1
        order: list[str] = []
        ready = [a for a in agents if indeg[a] == 0]
        while ready:
            ready.sort(key=rank.__getitem__)
            a = ready.pop(0)
            order.append(a)
            for nxt in out.get(a, []):
                if nxt in indeg and nxt != self.start:
                    indeg[nxt] -= 1
                    if indeg[nxt] == 0:
                        ready.append(nxt)
        return order + [a for a in agents if a not in order]

    def outgoing(self, node: str) -> list[Edge]:
        return [e for e in self.edges if e.from_ == node]

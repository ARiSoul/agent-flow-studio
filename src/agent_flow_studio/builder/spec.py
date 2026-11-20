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


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)


class ArtifactSpec(_Strict):
    name: str
    contract: str | None = None  # name in catalog.CONTRACTS (.json only)
    draft: bool = False  # .md draft for human approval (marked automatically)

    @model_validator(mode="after")
    def _check(self) -> ArtifactSpec:
        if not _ARTIFACT_RE.fullmatch(self.name) or ".." in self.name:
            raise ValueError(f"invalid artifact name: {self.name!r}")
        if self.contract and self.contract not in CONTRACTS:
            raise ValueError(f"unknown contract {self.contract!r}; available: "
                             f"{', '.join(CONTRACTS)}")
        if self.contract and not self.name.endswith(".json"):
            raise ValueError(f"{self.name}: contracts only apply to .json")
        if self.draft and not self.name.endswith(".md"):
            raise ValueError(f"{self.name}: drafts must be .md")
        return self


class AgentInput(_Strict):
    """An agent input: `from` a flow input (`input.<name>`) or an agent output (`<agent>.<out>`),
    or free (no `from`: given by the orchestrator, or in the editor when testing the agent).
    Without `type`, it takes the type of its source (text when free)."""

    from_: str | None = Field(default=None, alias="from")
    type: IOKind | None = None
    description: str = ""
    required: bool = True

    @field_validator("from_")
    @classmethod
    def _ref(cls, v: str | None) -> str | None:
        if v is not None and not re.fullmatch(r"[a-z][a-z0-9_]{0,39}\.[a-z][a-z0-9_]{0,39}", v):
            raise ValueError(f"'from' must be input.<name> or <agent>.<output>, not {v!r}")
        return v


# Write autonomy: approve = every write asks for approval; approve_prod = only in production;
# auto = no stops (everything stays in the audit).
Autonomy = Literal["approve", "approve_prod", "auto"]


class AgentOrigin(_Strict):
    """Where a copied agent came from: a library agent (builder/library.py) and the fingerprint
    of that library version when it was copied. Only used to show differences and updates."""

    agent: str = Field(pattern=SLUG)
    hash: str = Field(pattern=r"^[0-9a-f]{8,64}$")


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

    @field_validator("icon")
    @classmethod
    def _icon(cls, v: str | None) -> str | None:
        if v is not None and (not v.strip() or any(ord(c) < 32 for c in v)):
            raise ValueError("icon: a built-in icon name or a short text such as an emoji")
        return v

    @model_validator(mode="after")
    def _one_prompt(self) -> AgentSpec:
        if (self.prompt is None) == (self.prompt_file is None):
            raise ValueError("give exactly one of 'prompt' or 'prompt_file'")
        return self


class GateSpec(_Strict):
    kind: Literal["human", "stop"]
    label: str = Field(min_length=1)


class Edge(_Strict):
    from_: str = Field(alias="from")
    to: str
    when: str = ""  # condition in natural language (orchestrator, or the router in graph mode)
    if_: str = Field(default="", alias="if")  # expression on inputs/outputs (see expr.py)


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
            raise ValueError("stdio MCP needs 'command'")
        if self.type in ("http", "sse") and not (self.url and self.url.startswith("https://")):
            raise ValueError("http/sse MCP needs an https:// 'url'")
        for t in self.tools:
            if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", t):
                raise ValueError(f"invalid tool name (no wildcards): {t!r}")
        for k, v in self.env.items():
            if not _ENV_REF.fullmatch(v):
                raise ValueError(f"env {k}: use a ${{VAR}} reference; secrets stay out of the flow")
        for k, v in self.headers.items():
            if _ENV_IN.sub("", v).strip() not in ("", "Bearer", "Basic", "Token"):
                raise ValueError(f"header {k}: use ${{VAR}} references; secrets stay out of the flow")
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
            raise ValueError("flow name: lowercase, digits, '-' or '_'")
        return v

    @model_validator(mode="after")
    def _graph(self) -> FlowSpec:
        errors: list[str] = []
        for kind, names in (("agent", self.agents), ("gate", self.gates), ("mcp", self.mcp_servers)):
            errors += [f"{kind} {n!r}: invalid name (use {SLUG})" for n in names
                       if not _SLUG_RE.fullmatch(n)]
        clash = set(self.agents) & set(self.gates)
        if clash:
            errors.append(f"names repeated between agents and gates: {sorted(clash)}")
        clash = set(self.mcp_servers) & {t.server for t in BUILTIN_TOOLS.values()}
        if clash:
            errors.append(f"external MCP with the name of a built-in server: {sorted(clash)}")
        if not self.inputs and "{input}" not in self.task:
            errors.append("'task' must contain {input}")
        if self.inputs:
            known = set(self.inputs) | {"input", "run_id"}
            errors += [f"'task' uses {{{v}}}, which is not a flow input" for v in
                       _TASK_VAR.findall(self.task) if v not in known]
        nodes = set(self.agents) | set(self.gates)
        if self.start not in self.agents:
            errors.append(f"start {self.start!r} is not an agent")
        for e in self.edges:
            for end in (e.from_, e.to):
                if end not in nodes:
                    errors.append(f"edge {e.from_}→{e.to}: node {end!r} does not exist")
        errors += self._tool_errors()
        errors += self._io_errors()
        seen: dict[str, str] = {}
        for an, a in self.agents.items():
            for w in a.writes:
                if w.name in seen:
                    errors.append(f"artifact {w.name} written by {seen[w.name]} and {an}")
                seen[w.name] = an
            if a.writes and "artifacts.save_artifact" not in a.tools:
                errors.append(f"agent {an!r} writes artifacts but does not have artifacts.save_artifact")
            has_write = "connections.http_write" in a.tools
            if a.write_connections and not has_write:
                errors.append(f"agent {an!r} has write connectors but does not have "
                              "connections.http_write")
            if has_write and not a.write_connections:
                errors.append(f"agent {an!r} has connections.http_write but no write connector "
                              "assigned")
            if any(t.startswith("workspace.") for t in a.tools) and self.workspace is None:
                errors.append(f"agent {an!r} uses workspace.* tools but the flow has no "
                              "workspace")
            errors += [f"agent {an!r}: connector {c!r} has an invalid name"
                       for c in a.write_connections if not _SLUG_RE.fullmatch(c)]
            has_get = "connections.http_get" in a.tools
            if a.connections and not has_get:
                errors.append(f"agent {an!r} has connectors but does not have connections.http_get")
            if has_get and not a.connections:
                errors.append(f"agent {an!r} has connections.http_get but no connector assigned")
            errors += [f"agent {an!r}: connector {c!r} has an invalid name" for c in a.connections
                       if not _SLUG_RE.fullmatch(c)]
        if not errors and self.start in self.agents:
            unreachable = set(self.agents) - self.reachable()
            if unreachable:
                errors.append(f"agents not reachable from {self.start!r}: {sorted(unreachable)}")
        if errors:
            raise ValueError("; ".join(errors))
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

    def _io_errors(self) -> list[str]:
        errors: list[str] = []
        for n, t in self.inputs.items():
            if not _SLUG_RE.fullmatch(n):
                errors.append(f"flow input {n!r}: invalid name (use {SLUG})")
            try:
                check_type(t, CONTRACTS)
            except ValueError as e:
                errors.append(f"flow input {n!r}: {e}")
        flow_in = self.flow_inputs()
        for an, a in self.agents.items():
            for n, t in a.outputs.items():
                if not _SLUG_RE.fullmatch(n):
                    errors.append(f"agent {an!r}: output {n!r} has an invalid name")
                try:
                    check_type(t, CONTRACTS)
                except ValueError as e:
                    errors.append(f"agent {an!r}, output {n!r}: {e}")
            for n, i in a.inputs.items():
                if not _SLUG_RE.fullmatch(n):
                    errors.append(f"agent {an!r}: input {n!r} has an invalid name")
                if i.from_ is None:
                    continue
                root, src = i.from_.split(".", 1)
                if root == "input":
                    if src not in flow_in:
                        errors.append(f"agent {an!r}, input {n!r}: flow input {src!r} does not "
                                      "exist")
                        continue
                elif root not in self.agents:
                    errors.append(f"agent {an!r}, input {n!r}: agent {root!r} does not exist")
                    continue
                elif src not in self.agents[root].outputs:
                    errors.append(f"agent {an!r}, input {n!r}: agent {root!r} has no output "
                                  f"{src!r}")
                    continue
                elif root == an:
                    errors.append(f"agent {an!r}, input {n!r}: an agent cannot read its own output")
                    continue
                elif self.execution == "graph" and root not in self.ancestors(an):
                    errors.append(f"agent {an!r}, input {n!r}: {root!r} does not run before "
                                  f"{an!r} (add an edge path from {root!r} to {an!r})")
                st = self.source_type(i.from_)
                if i.type and st and not compatible(st, i.type):
                    errors.append(f"agent {an!r}, input {n!r}: a {st} cannot feed a {i.type}")
        for e in self.edges:
            if e.if_:
                errors += [f"edge {e.from_}→{e.to}: {m}" for m in self._expr_errors(e.if_)]
        return errors

    def _expr_errors(self, text: str) -> list[str]:
        try:
            used = expr.refs(text)
        except expr.ExprError as e:
            return [f"condition: {e}"]
        out = []
        for r in used:
            if len(r) < 2:
                out.append(f"condition: {r[0]!r} needs a field (e.g. {r[0]}.x)")
            elif r[0] == "input":
                if r[1] not in self.flow_inputs():
                    out.append(f"condition: flow input {r[1]!r} does not exist")
            elif r[0] in self.agents:
                if r[1] not in self.agents[r[0]].outputs:
                    out.append(f"condition: agent {r[0]!r} has no output {r[1]!r}")
            elif r[0] in self.gates:
                if r[1] not in ("decision", "comment"):
                    out.append(f"condition: a gate has only .decision and .comment, not {r[1]!r}")
            else:
                out.append(f"condition: {r[0]!r} is not input, an agent or a gate")
        return out

    def _tool_errors(self) -> list[str]:
        errors: list[str] = []
        for an, a in self.agents.items():
            for t in a.tools:
                if "*" in t or "." not in t:
                    errors.append(f"{an}: invalid tool {t!r} (format server.tool, no *)")
                    continue
                server, name = t.split(".", 1)
                if t in BUILTIN_TOOLS:
                    continue
                ext = self.mcp_servers.get(server)
                if ext is None:
                    errors.append(f"{an}: tool {t!r} does not exist in the catalog or in the flow's MCPs")
                elif name not in ext.tools:
                    errors.append(f"{an}: {name!r} is not in the allowlist of MCP {server!r}")
                elif not ext.approved:
                    errors.append(f"MCP {server!r} not approved (approved: true is required)")
                elif not ext.read_only:
                    errors.append(f"MCP {server!r} without read_only: true (rule 1: read-only)")
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

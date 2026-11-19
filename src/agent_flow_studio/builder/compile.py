"""Loads flows/<name>/flow.yaml and compiles it for the Claude Agent SDK.

The orchestrator receives the graph (agents, gates, edges) as a generated "Flow" section; each
subagent receives its tools, artifacts and common rules. Security does not depend on the model:
closed tool list in the hook, per flow and per subagent, contracts in save_artifact,
`dontAsk`, no native tools besides delegation, and no user settings.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml
from claude_agent_sdk import AgentDefinition, ClaudeAgentOptions, create_sdk_mcp_server, tool

from ..config import settings
from ..connections import store as connection_store
from ..hooks import DELEGATION_TOOL, make_guard, make_hooks
from ..policy import WritePolicy
from ..tools import artifacts
from ..tools import git as git_tool
from ..tools.common import err, ok
from .catalog import BUILTIN_SERVERS, BUILTIN_TOOLS, CONTRACTS
from .spec import AgentSpec, ExternalMcp, FlowSpec

DISALLOWED = [
    "Bash", "Write", "Edit", "MultiEdit", "NotebookEdit", "WebFetch", "WebSearch",
    "Read", "Glob", "Grep",
]
SUBAGENT_RULES = (
    "\n\n## Common rules\nRead-only; do not make up data; every conclusion points to an "
    "`evidence_ref`. If `save_artifact` returns a contract or name ERROR, fix the JSON (only the "
    "expected fields, with the expected types) and save again. Always use the run id that "
    "the orchestrator gives you as `run_id`."
)
_ENV_IN = re.compile(r"\$\{([A-Z_][A-Z0-9_]*)\}")
# Orchestrator-only tool: signals in a structured way that the flow reached a gate
# (the editor uses it to show the pending approval). It runs nothing.
GATE_SERVER = "flow_control"
GATE_TOOL = f"mcp__{GATE_SERVER}__reach_gate"


class FlowError(ValueError):
    pass


@dataclass(frozen=True)
class CompiledFlow:
    spec: FlowSpec
    options: ClaudeAgentOptions
    agent_tools: dict[str, list[str]]  # subagent → SDK names
    policy: WritePolicy | None = None  # the editor runner attaches the `approver` to it

    def task(self, run_input: str) -> str:
        if not re.fullmatch(self.spec.input.pattern, run_input):
            raise FlowError(f"invalid {self.spec.input.label}: {run_input!r}")
        return self.spec.task.replace("{input}", run_input)


def flows_dir() -> Path:
    return Path(os.getenv("FLOWS_DIR", "flows"))


def list_flows(base: Path | None = None) -> list[str]:
    base = base or flows_dir()
    return sorted(p.parent.name for p in base.glob("*/flow.yaml"))


def load_spec(name_or_path: str | Path, base: Path | None = None) -> tuple[FlowSpec, Path]:
    p = Path(name_or_path)
    path = p if p.suffix in (".yaml", ".yml") else (base or flows_dir()) / str(name_or_path) / "flow.yaml"
    if not path.exists():
        raise FlowError(f"flow not found: {path}")
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        spec = FlowSpec.model_validate(data)
    except (yaml.YAMLError, ValueError) as e:
        raise FlowError(f"{path}: {e}") from e
    return spec, path.parent


def _read_prompt(flow_dir: Path, inline: str | None, file: str | None, what: str) -> str:
    if inline is not None:
        return inline
    assert file is not None
    p = (flow_dir / file).resolve()
    if flow_dir.resolve() not in p.parents:
        raise FlowError(f"{what}: prompt_file outside the flow folder")
    if not p.exists():
        raise FlowError(f"{what}: prompt_file does not exist ({file})")
    return p.read_text(encoding="utf-8")


def _sdk_tool(spec: FlowSpec, tool_id: str) -> str:
    if tool_id in BUILTIN_TOOLS:
        return BUILTIN_TOOLS[tool_id].sdk_name
    server, name = tool_id.split(".", 1)
    return f"mcp__{server}__{name}"


def _artifacts_section(a: AgentSpec) -> str:
    lines = []
    if a.reads:
        lines.append("You read with `read_artifact`: " + ", ".join(f"`{r}`" for r in a.reads) + ".")
    for w in a.writes:
        extra = f" (contract {w.contract})" if w.contract else (" (draft)" if w.draft else "")
        lines.append(f"You save with `save_artifact`: `{w.name}`{extra}.")
    return ("\n\n## Artifacts\n" + "\n".join(lines)) if lines else ""


def _describe_target(spec: FlowSpec, node: str) -> str:
    if node in spec.agents:
        return f"delegate to `{node}`"
    g = spec.gates[node]
    if g.kind == "human":
        return f"STOP and ask for human approval: **{g.label}** (gate `{node}`)"
    return f"STOP and report: **{g.label}** (gate `{node}`)"


def flow_section(spec: FlowSpec) -> str:
    """The "Flow" section of the orchestrator prompt, generated from the graph."""
    out = ["## Flow (generated from the flow graph)", f"Start at `{spec.start}`."]
    for i, name in enumerate(spec.agent_order(), 1):
        a = spec.agents[name]
        writes = ", ".join(f"`{w.name}`" for w in a.writes) or "—"
        out.append(f"\n{i}. `{name}`: {a.description} Saves: {writes}.")
        for e in spec.outgoing(name):
            cond = f"if {e.when}" if e.when else "next"
            out.append(f"   - {cond}: {_describe_target(spec, e.to)}.")
    gates_out = [g for g in spec.gates if spec.outgoing(g)]
    if gates_out:
        out.append("\nAfter a human stop (in a run resumed with the answer):")
        for g in gates_out:
            for e in spec.outgoing(g):
                cond = f" and {e.when}" if e.when else ""
                out.append(f"- after **{spec.gates[g].label}**{cond}: {_describe_target(spec, e.to)}.")
    out.append("\nNever skip a step or use an agent outside this graph. When you reach a "
               "stop, call `reach_gate` with the gate name and the reason, then end the "
               "run and explain what is left to decide.")
    return "\n".join(out)


def _external_config(name: str, m: ExternalMcp) -> dict[str, Any]:
    def expand(v: str) -> str:
        def rep(mt: re.Match[str]) -> str:
            val = os.getenv(mt.group(1))
            if val is None:
                raise FlowError(f"MCP {name!r}: environment variable {mt.group(1)} is not set")
            return val
        return _ENV_IN.sub(rep, v)

    if m.type == "stdio":
        cfg: dict[str, Any] = {"type": "stdio", "command": m.command, "args": list(m.args)}
        if m.env:
            cfg["env"] = {k: expand(v) for k, v in m.env.items()}
        return cfg
    cfg = {"type": m.type, "url": m.url}
    if m.headers:
        cfg["headers"] = {k: expand(v) for k, v in m.headers.items()}
    return cfg


def _connections(spec: FlowSpec) -> dict[str, Any]:
    wanted = {c for a in spec.agents.values() for c in a.connections}
    if not wanted:
        return {}
    try:
        conns = connection_store.load()
    except connection_store.ConnectorConfigError as e:
        raise FlowError(str(e)) from e
    missing = sorted(wanted - set(conns))
    if missing:
        users = [n for n, a in spec.agents.items() if set(a.connections) & set(missing)]
        raise FlowError(f"agent {', '.join(users)}: connector(s) not configured on this machine: "
                        f"{', '.join(missing)}")
    return conns


def _connections_section(a: AgentSpec, conns: dict[str, Any]) -> str:
    if not a.connections:
        return ""
    lines = [f"- `{n}`: {conns[n].type}, environment {conns[n].environment}"
             + (f" — {conns[n].description}" if conns[n].description else "")
             + (f" (paths: {', '.join(conns[n].allowed_paths)})" if conns[n].allowed_paths else "")
             for n in a.connections]
    return "\n\n## Connectors (read-only, with `http_get`)\n" + "\n".join(lines)


def _workspace_dir(spec: FlowSpec, check: bool) -> Path | None:
    if spec.workspace is None:
        return None
    p = Path(spec.workspace.path).expanduser().resolve()
    if check and not p.is_dir():
        raise FlowError(f"workspace does not exist: {p}")
    return p


def _autonomy_text(level: str) -> str:
    return {"approve": "every write waits for human approval",
            "approve_prod": "writes in production wait for human approval",
            "auto": "writes run without approval (they are audited)"}[level]


def _write_section(spec: FlowSpec, a: AgentSpec, ws: Path | None) -> str:
    lines = []
    if ws is not None and any(t.startswith("workspace.") for t in a.tools):
        w = spec.workspace
        assert w is not None
        lines.append(f"You work in the workspace `{ws}` (environment {w.environment}); do not "
                     "leave this folder. Protected branches: "
                     f"{', '.join(w.protected_branches) or '—'}: create a work branch and propose "
                     "the changes in a PR (as a draft).")
    if a.write_connections:
        lines.append("Connectors with write (`http_write`): "
                     + ", ".join(f"`{c}`" for c in a.write_connections) + ".")
    if lines:
        lines.append(f"Autonomy: {_autonomy_text(a.autonomy or spec.autonomy)}. If a write "
                     "is refused, do not insist: continue without it or report.")
        return "\n\n## Writes\n" + "\n".join(lines)
    return ""


def _gate_server(spec: FlowSpec) -> Any:
    gates = dict(spec.gates)

    @tool("reach_gate", "Signals that the flow reached a gate (human approval or stop). "
          "Call it once, before you finish.", {"gate": str, "reason": str})
    async def reach_gate(args: dict[str, Any]) -> dict[str, Any]:
        g = gates.get(args["gate"])
        if g is None:
            return err(f"unknown gate {args['gate']!r}; gates: {', '.join(gates)}")
        return ok(f"Recorded: {g.label}. End the run now and explain what is left to decide.")

    return create_sdk_mcp_server(name=GATE_SERVER, version="0.1.0", tools=[reach_gate])


def compile_flow(spec: FlowSpec, flow_dir: Path, *, resume: str | None = None,
                 resolve_secrets: bool = True, configure_artifacts: bool = True) -> CompiledFlow:
    """configure_artifacts=False: validate only (leaves the global state of `artifacts` as is)."""
    agents: dict[str, AgentDefinition] = {}
    agent_tools: dict[str, list[str]] = {}
    conns = _connections(spec)
    ws = _workspace_dir(spec, check=configure_artifacts)
    for name, a in spec.agents.items():
        body = _read_prompt(flow_dir, a.prompt, a.prompt_file, f"agent {name}")
        tools = [_sdk_tool(spec, t) for t in a.tools]
        agent_tools[name] = tools
        agents[name] = AgentDefinition(
            description=a.description,
            prompt=(body + _artifacts_section(a) + _connections_section(a, conns)
                    + _write_section(spec, a, ws) + SUBAGENT_RULES),
            tools=tools,
            model=a.model,
            skills=a.skills or None,
            maxTurns=a.max_turns,
        )

    used = {t for ts in spec.agents.values() for t in ts.tools}
    used_builtin = {BUILTIN_TOOLS[t].server for t in used
                    if t in BUILTIN_TOOLS and BUILTIN_TOOLS[t].native is None}
    natives = sorted({str(BUILTIN_TOOLS[t].native) for t in used
                      if t in BUILTIN_TOOLS and BUILTIN_TOOLS[t].native})
    servers: dict[str, Any] = {s: BUILTIN_SERVERS[s] for s in sorted(used_builtin)}
    for name, m in spec.mcp_servers.items():
        if any(t.startswith(f"{name}.") for t in used):
            servers[name] = _external_config(name, m) if resolve_secrets else {"type": m.type}

    allowed = sorted({t for ts in agent_tools.values() for t in ts})
    if spec.gates:  # orchestrator only: the subagents have their own list in the hook
        servers[GATE_SERVER] = _gate_server(spec)
        allowed.append(GATE_TOOL)
    orch = _read_prompt(flow_dir, spec.orchestrator.prompt, spec.orchestrator.prompt_file,
                        "orchestrator") if (spec.orchestrator.prompt or spec.orchestrator.prompt_file) \
        else "You orchestrate this flow. You delegate to the subagents and never change anything."

    names = {w.name for a in spec.agents.values() for w in a.writes}
    contracts = {w.name: CONTRACTS[w.contract] for a in spec.agents.values()
                 for w in a.writes if w.contract}
    if configure_artifacts:
        artifacts.configure(names, contracts)
        git_tool.configure(ws)

    policy = WritePolicy(
        flow_autonomy=spec.autonomy,
        agent_autonomy={n: a.autonomy for n, a in spec.agents.items() if a.autonomy},
        workspace=ws,
        workspace_env=spec.workspace.environment if spec.workspace else "dev",
        protected_branches=list(spec.workspace.protected_branches) if spec.workspace else [],
        write_connections={n: set(a.write_connections) for n, a in spec.agents.items()},
        connection_env={n: c.environment for n, c in conns.items()},
    )
    options = ClaudeAgentOptions(
        system_prompt=orch.rstrip() + "\n\n" + flow_section(spec),
        tools=[DELEGATION_TOOL, *natives],
        cwd=str(ws) if ws and natives else None,
        mcp_servers=servers,
        allowed_tools=[*allowed, DELEGATION_TOOL],
        disallowed_tools=[d for d in DISALLOWED if d not in natives],
        permission_mode="dontAsk",
        setting_sources=[],
        agents=agents,
        hooks=make_hooks(make_guard(allowed, agent_tools,
                                    {n: a.connections for n, a in spec.agents.items()}, policy)),
        max_turns=spec.max_turns,
        model=spec.model or settings.model,
        resume=resume,
    )
    return CompiledFlow(spec, options, agent_tools, policy)


def load_flow(name: str, *, resume: str | None = None, base: Path | None = None) -> CompiledFlow:
    spec, flow_dir = load_spec(name, base)
    return compile_flow(spec, flow_dir, resume=resume)

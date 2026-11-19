"""Carrega flows/<nome>/flow.yaml e compila-o para o Claude Agent SDK.

O orquestrador recebe o grafo (agentes, gates, ligações) como secção "Fluxo" gerada; cada subagente
recebe as suas ferramentas, artefactos e regras comuns. A segurança não depende do modelo:
lista fechada de ferramentas no hook, por flow e por subagente, contratos no save_artifact,
`dontAsk`, sem ferramentas nativas além da delegação, e sem settings do utilizador.
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
from ..tools.common import err, ok
from .catalog import BUILTIN_SERVERS, BUILTIN_TOOLS, CONTRACTS
from .spec import AgentSpec, ExternalMcp, FlowSpec

DISALLOWED = [
    "Bash", "Write", "Edit", "MultiEdit", "NotebookEdit", "WebFetch", "WebSearch",
    "Read", "Glob", "Grep",
]
SUBAGENT_RULES = (
    "\n\n## Regras comuns\nSó leitura; não inventes dados; cada conclusão aponta para uma "
    "`evidence_ref`. Se `save_artifact` devolver ERRO de contrato ou de nome, corrige o JSON (só os "
    "campos previstos, com os tipos previstos) e volta a guardar. Usa sempre o id de execução que "
    "o orquestrador te passar como `run_id`."
)
_ENV_IN = re.compile(r"\$\{([A-Z_][A-Z0-9_]*)\}")
# Ferramenta só do orquestrador: assinala de forma estruturada que o flow chegou a um gate
# (o editor usa-a para mostrar a aprovação pendente). Não executa nada.
GATE_SERVER = "flow_control"
GATE_TOOL = f"mcp__{GATE_SERVER}__reach_gate"


class FlowError(ValueError):
    pass


@dataclass(frozen=True)
class CompiledFlow:
    spec: FlowSpec
    options: ClaudeAgentOptions
    agent_tools: dict[str, list[str]]  # subagente → nomes SDK
    policy: WritePolicy | None = None  # o runner do editor liga-lhe o `approver`

    def task(self, run_input: str) -> str:
        if not re.fullmatch(self.spec.input.pattern, run_input):
            raise FlowError(f"{self.spec.input.label} inválido: {run_input!r}")
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
        raise FlowError(f"flow não encontrado: {path}")
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
        raise FlowError(f"{what}: prompt_file fora da pasta do flow")
    if not p.exists():
        raise FlowError(f"{what}: prompt_file não existe ({file})")
    return p.read_text(encoding="utf-8")


def _sdk_tool(spec: FlowSpec, tool_id: str) -> str:
    if tool_id in BUILTIN_TOOLS:
        return BUILTIN_TOOLS[tool_id].sdk_name
    server, name = tool_id.split(".", 1)
    return f"mcp__{server}__{name}"


def _artifacts_section(a: AgentSpec) -> str:
    lines = []
    if a.reads:
        lines.append("Lês com `read_artifact`: " + ", ".join(f"`{r}`" for r in a.reads) + ".")
    for w in a.writes:
        extra = f" (contrato {w.contract})" if w.contract else (" (rascunho)" if w.draft else "")
        lines.append(f"Guardas com `save_artifact`: `{w.name}`{extra}.")
    return ("\n\n## Artefactos\n" + "\n".join(lines)) if lines else ""


def _describe_target(spec: FlowSpec, node: str) -> str:
    if node in spec.agents:
        return f"delega em `{node}`"
    g = spec.gates[node]
    if g.kind == "human":
        return f"PÁRA e pede aprovação humana: **{g.label}** (gate `{node}`)"
    return f"PÁRA e reporta: **{g.label}** (gate `{node}`)"


def flow_section(spec: FlowSpec) -> str:
    """Secção "Fluxo" do prompt do orquestrador, gerada a partir do grafo."""
    out = ["## Fluxo (gerado a partir do grafo do flow)", f"Começa em `{spec.start}`."]
    for i, name in enumerate(spec.agent_order(), 1):
        a = spec.agents[name]
        writes = ", ".join(f"`{w.name}`" for w in a.writes) or "—"
        out.append(f"\n{i}. `{name}`: {a.description} Guarda: {writes}.")
        for e in spec.outgoing(name):
            cond = f"se {e.when}" if e.when else "a seguir"
            out.append(f"   - {cond}: {_describe_target(spec, e.to)}.")
    gates_out = [g for g in spec.gates if spec.outgoing(g)]
    if gates_out:
        out.append("\nDepois de uma paragem humana (numa execução retomada com a resposta):")
        for g in gates_out:
            for e in spec.outgoing(g):
                cond = f" e {e.when}" if e.when else ""
                out.append(f"- após **{spec.gates[g].label}**{cond}: {_describe_target(spec, e.to)}.")
    out.append("\nNunca saltes um passo nem uses um agente fora deste grafo. Quando chegares a uma "
               "paragem, chama `reach_gate` com o nome do gate e o motivo, depois termina a "
               "execução e explica o que falta decidir.")
    return "\n".join(out)


def _external_config(name: str, m: ExternalMcp) -> dict[str, Any]:
    def expand(v: str) -> str:
        def rep(mt: re.Match[str]) -> str:
            val = os.getenv(mt.group(1))
            if val is None:
                raise FlowError(f"MCP {name!r}: variável de ambiente {mt.group(1)} não definida")
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
        raise FlowError(f"agente {', '.join(users)}: conector(es) não configurado(s) nesta máquina: "
                        f"{', '.join(missing)}")
    return conns


def _connections_section(a: AgentSpec, conns: dict[str, Any]) -> str:
    if not a.connections:
        return ""
    lines = [f"- `{n}`: {conns[n].type}, ambiente {conns[n].environment}"
             + (f" — {conns[n].description}" if conns[n].description else "")
             + (f" (caminhos: {', '.join(conns[n].allowed_paths)})" if conns[n].allowed_paths else "")
             for n in a.connections]
    return "\n\n## Conectores (só leitura, com `http_get`)\n" + "\n".join(lines)


def _workspace_dir(spec: FlowSpec, check: bool) -> Path | None:
    if spec.workspace is None:
        return None
    p = Path(spec.workspace.path).expanduser().resolve()
    if check and not p.is_dir():
        raise FlowError(f"workspace não existe: {p}")
    return p


def _autonomy_text(level: str) -> str:
    return {"approve": "cada escrita espera aprovação humana",
            "approve_prod": "escritas em produção esperam aprovação humana",
            "auto": "as escritas correm sem aprovação (ficam auditadas)"}[level]


def _write_section(spec: FlowSpec, a: AgentSpec, ws: Path | None) -> str:
    lines = []
    if ws is not None and any(t.startswith("workspace.") for t in a.tools):
        w = spec.workspace
        assert w is not None
        lines.append(f"Trabalhas no workspace `{ws}` (ambiente {w.environment}); não saias desta "
                     f"pasta. Branches protegidas: {', '.join(w.protected_branches) or '—'}: cria "
                     "uma branch de trabalho e propõe as alterações por PR (como rascunho).")
    if a.write_connections:
        lines.append("Conectores com escrita (`http_write`): "
                     + ", ".join(f"`{c}`" for c in a.write_connections) + ".")
    if lines:
        lines.append(f"Autonomia: {_autonomy_text(a.autonomy or spec.autonomy)}. Se uma escrita "
                     "for recusada, não insistas: segue sem ela ou reporta.")
        return "\n\n## Escrita\n" + "\n".join(lines)
    return ""


def _gate_server(spec: FlowSpec) -> Any:
    gates = dict(spec.gates)

    @tool("reach_gate", "Assinala que o flow chegou a um gate (aprovação humana ou paragem). "
          "Chama-a uma vez, antes de terminar.", {"gate": str, "reason": str})
    async def reach_gate(args: dict[str, Any]) -> dict[str, Any]:
        g = gates.get(args["gate"])
        if g is None:
            return err(f"gate desconhecido {args['gate']!r}; gates: {', '.join(gates)}")
        return ok(f"Registado: {g.label}. Termina agora a execução e explica o que falta decidir.")

    return create_sdk_mcp_server(name=GATE_SERVER, version="0.1.0", tools=[reach_gate])


def compile_flow(spec: FlowSpec, flow_dir: Path, *, resume: str | None = None,
                 resolve_secrets: bool = True, configure_artifacts: bool = True) -> CompiledFlow:
    """configure_artifacts=False: só validar (não mexe no estado global de `artifacts`)."""
    agents: dict[str, AgentDefinition] = {}
    agent_tools: dict[str, list[str]] = {}
    conns = _connections(spec)
    ws = _workspace_dir(spec, check=configure_artifacts)
    for name, a in spec.agents.items():
        body = _read_prompt(flow_dir, a.prompt, a.prompt_file, f"agente {name}")
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
    if spec.gates:  # só o orquestrador a pode usar: os subagentes têm lista própria no hook
        servers[GATE_SERVER] = _gate_server(spec)
        allowed.append(GATE_TOOL)
    orch = _read_prompt(flow_dir, spec.orchestrator.prompt, spec.orchestrator.prompt_file,
                        "orquestrador") if (spec.orchestrator.prompt or spec.orchestrator.prompt_file) \
        else "És o orquestrador deste flow. Delegas o trabalho aos subagentes e nunca alteras nada."

    names = {w.name for a in spec.agents.values() for w in a.writes}
    contracts = {w.name: CONTRACTS[w.contract] for a in spec.agents.values()
                 for w in a.writes if w.contract}
    if configure_artifacts:
        artifacts.configure(names, contracts)

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

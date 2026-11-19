"""Formato de um flow (flows/<nome>/flow.yaml) e respetivas regras de validação.

Um flow é um grafo: nós `agents` (subagentes) e `gates` (aprovação humana ou paragem), ligados por
`edges` com uma condição em linguagem natural. O orquestrador recebe o grafo como instruções.
"""
from __future__ import annotations

import re
from collections import deque
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .catalog import BUILTIN_TOOLS, CONTRACTS

SLUG = r"^[a-z][a-z0-9_]{0,39}$"
_SLUG_RE = re.compile(SLUG)
_ARTIFACT_RE = re.compile(r"^[A-Za-z0-9_.-]{1,80}$")
_ENV_REF = re.compile(r"^\$\{[A-Z_][A-Z0-9_]*\}$")
_ENV_IN = re.compile(r"\$\{([A-Z_][A-Z0-9_]*)\}")


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)


class ArtifactSpec(_Strict):
    name: str
    contract: str | None = None  # nome em catalog.CONTRACTS (só para .json)
    draft: bool = False  # rascunho .md para aprovação humana (marcado automaticamente)

    @model_validator(mode="after")
    def _check(self) -> ArtifactSpec:
        if not _ARTIFACT_RE.fullmatch(self.name) or ".." in self.name:
            raise ValueError(f"nome de artefacto inválido: {self.name!r}")
        if self.contract and self.contract not in CONTRACTS:
            raise ValueError(f"contrato desconhecido {self.contract!r}; disponíveis: "
                             f"{', '.join(CONTRACTS)}")
        if self.contract and not self.name.endswith(".json"):
            raise ValueError(f"{self.name}: contratos só se aplicam a .json")
        if self.draft and not self.name.endswith(".md"):
            raise ValueError(f"{self.name}: rascunhos têm de ser .md")
        return self


# Autonomia nas escritas: approve = cada escrita pede aprovação; approve_prod = só em produção;
# auto = sem paragens (fica tudo no audit).
Autonomy = Literal["approve", "approve_prod", "auto"]


class AgentSpec(_Strict):
    description: str = Field(min_length=1)
    prompt: str | None = None
    prompt_file: str | None = None  # relativo à pasta do flow
    tools: list[str] = Field(default_factory=list)  # "servidor.ferramenta"
    model: str | None = None  # "inherit", "sonnet", "opus", "haiku" ou ID completo
    skills: list[str] = Field(default_factory=list)
    max_turns: int | None = Field(default=None, ge=1, le=200)
    reads: list[str] = Field(default_factory=list)
    writes: list[ArtifactSpec] = Field(default_factory=list)
    connections: list[str] = Field(default_factory=list)  # conectores (connections.yaml) para http_get
    write_connections: list[str] = Field(default_factory=list)  # conectores para http_write
    autonomy: Autonomy | None = None  # None = a do flow

    @model_validator(mode="after")
    def _one_prompt(self) -> AgentSpec:
        if (self.prompt is None) == (self.prompt_file is None):
            raise ValueError("indica exatamente um de 'prompt' ou 'prompt_file'")
        return self


class GateSpec(_Strict):
    kind: Literal["human", "stop"]
    label: str = Field(min_length=1)


class Edge(_Strict):
    from_: str = Field(alias="from")
    to: str
    when: str = ""


class ExternalMcp(_Strict):
    """Servidor MCP externo. Só entra num flow com aprovação explícita e lista fechada de ferramentas."""

    type: Literal["stdio", "http", "sse"]
    description: str = ""
    command: str | None = None
    args: list[str] = Field(default_factory=list)
    env: dict[str, str] = Field(default_factory=dict)  # valores só "${VAR}"
    url: str | None = None
    headers: dict[str, str] = Field(default_factory=dict)  # valores só com "${VAR}"
    tools: list[str] = Field(min_length=1)  # nomes exatos das ferramentas permitidas
    approved: bool = False  # aprovação humana explícita para usar este servidor
    read_only: bool = False  # declaração de que as ferramentas listadas só leem

    @model_validator(mode="after")
    def _check(self) -> ExternalMcp:
        if self.type == "stdio" and not self.command:
            raise ValueError("MCP stdio precisa de 'command'")
        if self.type in ("http", "sse") and not (self.url and self.url.startswith("https://")):
            raise ValueError("MCP http/sse precisa de 'url' https://")
        for t in self.tools:
            if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", t):
                raise ValueError(f"nome de ferramenta inválido (sem curingas): {t!r}")
        for k, v in self.env.items():
            if not _ENV_REF.fullmatch(v):
                raise ValueError(f"env {k}: use uma referência ${{VAR}}; segredos não ficam no flow")
        for k, v in self.headers.items():
            if _ENV_IN.sub("", v).strip() not in ("", "Bearer", "Basic", "Token"):
                raise ValueError(f"header {k}: use referências ${{VAR}}; segredos não ficam no flow")
        return self


class WorkspaceSpec(_Strict):
    """Repositório/pasta onde os agentes com ferramentas workspace.* trabalham."""

    path: str = Field(min_length=1)  # absoluto, ou relativo à pasta onde o investigator corre
    environment: Literal["dev", "staging", "prod"] = "dev"
    description: str = ""
    protected_branches: list[str] = Field(default_factory=lambda: ["main", "master"])


class InputSpec(_Strict):
    label: str = "Entrada"
    pattern: str = r"^[A-Za-z0-9_-]{1,40}$"  # é também o id da execução (pasta em runs/)


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
    input: InputSpec = Field(default_factory=InputSpec)
    task: str = "Executa o flow para {input}."
    orchestrator: OrchestratorSpec = Field(default_factory=OrchestratorSpec)
    start: str
    agents: dict[str, AgentSpec]
    gates: dict[str, GateSpec] = Field(default_factory=dict)
    edges: list[Edge] = Field(default_factory=list)
    mcp_servers: dict[str, ExternalMcp] = Field(default_factory=dict)
    autonomy: Autonomy = "approve"
    workspace: WorkspaceSpec | None = None
    layout: dict[str, Position] = Field(default_factory=dict)  # posições no editor

    @field_validator("name")
    @classmethod
    def _slug(cls, v: str) -> str:
        if not re.fullmatch(r"^[a-z0-9][a-z0-9_-]{0,39}$", v):
            raise ValueError("nome do flow: minúsculas, dígitos, '-' ou '_'")
        return v

    @model_validator(mode="after")
    def _graph(self) -> FlowSpec:
        errors: list[str] = []
        for kind, names in (("agente", self.agents), ("gate", self.gates), ("mcp", self.mcp_servers)):
            errors += [f"{kind} {n!r}: nome inválido (use {SLUG})" for n in names
                       if not _SLUG_RE.fullmatch(n)]
        clash = set(self.agents) & set(self.gates)
        if clash:
            errors.append(f"nomes repetidos entre agentes e gates: {sorted(clash)}")
        clash = set(self.mcp_servers) & {t.server for t in BUILTIN_TOOLS.values()}
        if clash:
            errors.append(f"MCP externo com nome de servidor interno: {sorted(clash)}")
        if "{input}" not in self.task:
            errors.append("'task' tem de conter {input}")
        nodes = set(self.agents) | set(self.gates)
        if self.start not in self.agents:
            errors.append(f"start {self.start!r} não é um agente")
        for e in self.edges:
            for end in (e.from_, e.to):
                if end not in nodes:
                    errors.append(f"ligação {e.from_}→{e.to}: nó {end!r} não existe")
        errors += self._tool_errors()
        seen: dict[str, str] = {}
        for an, a in self.agents.items():
            for w in a.writes:
                if w.name in seen:
                    errors.append(f"artefacto {w.name} escrito por {seen[w.name]} e {an}")
                seen[w.name] = an
            if a.writes and "artifacts.save_artifact" not in a.tools:
                errors.append(f"agente {an!r} escreve artefactos mas não tem artifacts.save_artifact")
            has_write = "connections.http_write" in a.tools
            if a.write_connections and not has_write:
                errors.append(f"agente {an!r} tem conectores de escrita mas não tem "
                              "connections.http_write")
            if has_write and not a.write_connections:
                errors.append(f"agente {an!r} tem connections.http_write mas nenhum conector "
                              "de escrita atribuído")
            if any(t.startswith("workspace.") for t in a.tools) and self.workspace is None:
                errors.append(f"agente {an!r} usa ferramentas workspace.* mas o flow não tem "
                              "workspace")
            errors += [f"agente {an!r}: conector {c!r} com nome inválido"
                       for c in a.write_connections if not _SLUG_RE.fullmatch(c)]
            has_get = "connections.http_get" in a.tools
            if a.connections and not has_get:
                errors.append(f"agente {an!r} tem conectores mas não tem connections.http_get")
            if has_get and not a.connections:
                errors.append(f"agente {an!r} tem connections.http_get mas nenhum conector atribuído")
            errors += [f"agente {an!r}: conector {c!r} com nome inválido" for c in a.connections
                       if not _SLUG_RE.fullmatch(c)]
        if not errors and self.start in self.agents:
            unreachable = set(self.agents) - self.reachable()
            if unreachable:
                errors.append(f"agentes sem ligação a partir de {self.start!r}: {sorted(unreachable)}")
        if errors:
            raise ValueError("; ".join(errors))
        return self

    def _tool_errors(self) -> list[str]:
        errors: list[str] = []
        for an, a in self.agents.items():
            for t in a.tools:
                if "*" in t or "." not in t:
                    errors.append(f"{an}: ferramenta {t!r} inválida (formato servidor.ferramenta, sem *)")
                    continue
                server, name = t.split(".", 1)
                if t in BUILTIN_TOOLS:
                    continue
                ext = self.mcp_servers.get(server)
                if ext is None:
                    errors.append(f"{an}: ferramenta {t!r} não existe no catálogo nem nos MCPs do flow")
                elif name not in ext.tools:
                    errors.append(f"{an}: {name!r} não está na lista permitida do MCP {server!r}")
                elif not ext.approved:
                    errors.append(f"MCP {server!r} não aprovado (approved: true é obrigatório)")
                elif not ext.read_only:
                    errors.append(f"MCP {server!r} sem read_only: true (regra 1: só leitura)")
        return errors

    # --- utilitários usados pelo compilador e pelo editor
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
        """Ordem topológica dos agentes (ligações diretas agente→agente), com desempate pela
        travessia em largura a partir de start. Ciclos não bloqueiam: o resto segue a travessia."""
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

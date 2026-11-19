"""Política de escrita aplicada pelo hook de guarda (não depende do modelo).

- Ferramentas de ficheiros (Read/Edit/Write/Glob/Grep) só dentro do workspace do flow; Bash corre
  com o workspace como pasta de trabalho e recusa comandos perigosos (push forçado, push para
  branches protegidas, apagar a raiz/home, descarregar e executar scripts…).
- Escritas (Edit, Write, Bash, connections.http_write) seguem a autonomia do agente ou do flow:
  `approve` pede aprovação a cada escrita, `approve_prod` só quando o alvo é produção, `auto` não
  pára. Sem quem aprove (ex.: CLI), uma escrita que precise de aprovação é recusada.
- O orquestrador nunca usa ferramentas de ficheiros nem de escrita: só os subagentes.

Nota: Bash não é uma sandbox. Um comando pode sair da pasta; por isso Bash é uma escrita (pede
aprovação nos modos approve/approve_prod) e a lista de recusas é só uma rede extra.
"""
from __future__ import annotations

import re
import uuid
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .builder.catalog import NATIVE_TOOLS, WRITE_TOOLS

CONN_WRITE_TOOL = "mcp__connections__http_write"
_FILE_TOOLS = {"Read": "file_path", "Edit": "file_path", "Write": "file_path",
               "Glob": "path", "Grep": "path"}
_DENY_BASH = [
    (r"\bgit\s+push\b[^\n]*(\s--force\b|\s-f\b|\s--force-with-lease\b|\s\+\S)", "push forçado"),
    (r"\brm\s+-[a-zA-Z]*r[a-zA-Z]*f?\s+(/|~|\$HOME|[A-Za-z]:[\\/]?)(\s|$)", "apagar a raiz/home"),
    (r"Remove-Item[^\n]*-Recurse[^\n]*\s([A-Za-z]:\\?|~|/)(\s|$)", "apagar a raiz/home"),
    (r"\b(curl|wget|iwr|Invoke-WebRequest)\b[^\n|]*\|\s*(sh|bash|iex|pwsh|powershell)\b",
     "descarregar e executar"),
    (r"\b(shutdown|format|mkfs|diskpart)\b", "comando de sistema"),
    (r"\bgit\s+config\s+--global\b", "alterar configuração global do git"),
]

Approver = Callable[["ApprovalRequest"], Awaitable[tuple[bool, str]]]


@dataclass
class ApprovalRequest:
    agent: str | None
    tool: str
    environment: str
    summary: str
    detail: dict[str, Any]
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:10])


def _clip(v: Any, n: int = 20_000) -> Any:
    return v[:n] + "…" if isinstance(v, str) and len(v) > n else v


def describe(tool: str, args: Mapping[str, Any]) -> tuple[str, dict[str, Any]]:
    """Resumo legível e detalhe (truncado) do que a escrita vai fazer."""
    if tool == "Write":
        size = len(str(args.get("content", "")))
        return (f"Criar/substituir {args.get('file_path')} ({size} caracteres)",
                {"file_path": args.get("file_path"), "content": _clip(args.get("content", ""))})
    if tool == "Edit":
        return (f"Editar {args.get('file_path')}",
                {"file_path": args.get("file_path"), "old_string": _clip(args.get("old_string", "")),
                 "new_string": _clip(args.get("new_string", "")),
                 "replace_all": args.get("replace_all", False)})
    if tool == "Bash":
        return f"$ {str(args.get('command', ''))[:300]}", {"command": _clip(args.get("command"))}
    if tool == CONN_WRITE_TOOL:
        return (f"{str(args.get('method', '')).upper()} {args.get('connection')}{args.get('path')}",
                {"connection": args.get("connection"), "method": args.get("method"),
                 "path": args.get("path"), "query": args.get("query"),
                 "body": _clip(args.get("body"))})
    return tool, {}


@dataclass
class WritePolicy:
    flow_autonomy: str = "approve"
    agent_autonomy: dict[str, str] = field(default_factory=dict)
    workspace: Path | None = None
    workspace_env: str = "dev"
    protected_branches: list[str] = field(default_factory=lambda: ["main", "master"])
    write_connections: dict[str, set[str]] = field(default_factory=dict)  # agente → conectores
    connection_env: dict[str, str] = field(default_factory=dict)
    approver: Approver | None = None
    # Testes passo a passo (o runner do editor ajusta estes campos antes de cada segmento):
    delegation_limit: int | None = None  # nº de delegações permitidas neste segmento
    delegation_only: frozenset[str] | None = None  # subagentes permitidos (None = todos)
    delegations: int = 0

    def limit_segment(self, limit: int | None, only: frozenset[str] | None = None) -> None:
        self.delegation_limit, self.delegation_only, self.delegations = limit, only, 0

    def _delegation(self, args: Mapping[str, Any]) -> str | None:
        sub = str(args.get("subagent_type", ""))
        if self.delegation_only is not None and sub not in self.delegation_only:
            return (f"modo de teste: só podes delegar em {', '.join(sorted(self.delegation_only))}."
                    " Termina agora e reporta o estado.")
        if self.delegation_limit is not None and self.delegations >= self.delegation_limit:
            return ("modo passo a passo: este passo terminou. Não delegues mais; termina agora "
                    "com um resumo curto do passo e do que vem a seguir.")
        self.delegations += 1
        return None

    def _inside(self, raw: str | None, required: bool) -> str | None:
        if not raw:
            return "falta o caminho" if required else None
        assert self.workspace is not None
        p = Path(raw)
        p = (p if p.is_absolute() else self.workspace / p).resolve()
        ws = self.workspace.resolve()
        if p != ws and ws not in p.parents:
            return f"caminho fora do workspace ({ws})"
        return None

    def _bash_denied(self, cmd: str) -> str | None:
        for pattern, why in _DENY_BASH:
            if re.search(pattern, cmd, re.IGNORECASE):
                return why
        for b in self.protected_branches:
            if re.search(rf"\bgit\s+push\b[^\n]*\b(origin\s+)?(HEAD:)?{re.escape(b)}\b", cmd):
                return f"push para a branch protegida {b!r} (abre um PR)"
        return None

    def level(self, agent: str | None) -> str:
        return self.agent_autonomy.get(agent or "", self.flow_autonomy)

    async def check(self, tool: str, agent: str | None, args: Mapping[str, Any]) -> str | None:
        """Devolve o motivo da recusa, ou None se a chamada pode seguir."""
        if tool in ("Agent", "Task") and agent is None:
            return self._delegation(args)
        if tool not in NATIVE_TOOLS and tool not in WRITE_TOOLS:
            return None
        if agent is None:
            return "o orquestrador não escreve nem mexe em ficheiros; delega num subagente"
        env = self.workspace_env
        if tool in NATIVE_TOOLS:
            if self.workspace is None:
                return "o flow não tem workspace"
            if tool in _FILE_TOOLS:
                why = self._inside(args.get(_FILE_TOOLS[tool]), tool in ("Read", "Edit", "Write"))
                if why:
                    return why
                if tool == "Glob" and ".." in str(args.get("pattern", "")):
                    return "padrão com '..'"
            if tool == "Bash":
                why = self._bash_denied(str(args.get("command", "")))
                if why:
                    return f"comando recusado: {why}"
        if tool == CONN_WRITE_TOOL:
            conn = str(args.get("connection", ""))
            if conn not in self.write_connections.get(agent, set()):
                return f"conector {conn!r} sem escrita atribuída ao subagente {agent!r}"
            env = self.connection_env.get(conn, "prod")  # desconhecido = o mais restrito
        if tool not in WRITE_TOOLS:
            return None
        level = self.level(agent)
        if level == "auto" or (level == "approve_prod" and env != "prod"):
            return None
        if self.approver is None:
            return ("esta escrita requer aprovação humana; corre o flow no editor (Executar) "
                    "ou muda a autonomia")
        summary, detail = describe(tool, args)
        ok, comment = await self.approver(ApprovalRequest(agent, tool, env, summary, detail))
        if not ok:
            return "recusado pelo humano" + (f": {comment}" if comment else "") + \
                ". Não repitas esta escrita; segue sem ela ou reporta."
        return None

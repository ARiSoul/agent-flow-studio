"""Catálogo do construtor: o que um flow pode usar.

- Ferramentas internas: as dos servidores MCP em processo de `registry.py` (id "servidor.ferramenta").
- Contratos: modelos pydantic fornecidos pelas extensões (ver extensions.py).
Os MCPs externos não estão aqui: são declarados em cada flow (ver spec.ExternalMcp).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from pydantic import TypeAdapter

from ..extensions import LOADED
from ..registry import _GROUPS, MCP_SERVERS


@dataclass(frozen=True)
class ToolInfo:
    id: str  # "servidor.ferramenta"
    server: str
    name: str
    description: str
    writes_local_only: bool = False  # escreve, mas só em runs/<id>/ (artefactos)
    native: str | None = None  # ferramenta nativa do Claude Code (workspace.*)
    kind: str = "read"  # read · write_local (runs/) · write (efeito fora do projeto)

    @property
    def sdk_name(self) -> str:
        return self.native or f"mcp__{self.server}__{self.name}"


BUILTIN_TOOLS: dict[str, ToolInfo] = {
    f"{server}.{t.name}": ToolInfo(
        f"{server}.{t.name}", server, t.name, t.description,
        writes_local_only=(server == "artifacts" and t.name == "save_artifact"),
    )
    for server, tools in _GROUPS.items()
    for t in tools
}
for _t in BUILTIN_TOOLS.values():  # classificação de escrita
    object.__setattr__(_t, "kind", "write_local" if _t.writes_local_only
                       else "write" if _t.id == "connections.http_write" else "read")

# Ferramentas nativas, só dentro do workspace do flow (o hook confina os caminhos).
_WORKSPACE = {
    "read": ("Read", "Lê um ficheiro do workspace do flow.", "read"),
    "glob": ("Glob", "Procura ficheiros por padrão no workspace.", "read"),
    "grep": ("Grep", "Procura texto nos ficheiros do workspace.", "read"),
    "edit": ("Edit", "Edita um ficheiro do workspace (escrita).", "write"),
    "write": ("Write", "Cria ou substitui um ficheiro do workspace (escrita).", "write"),
    "bash": ("Bash", "Corre um comando na pasta do workspace: git, testes, gh, az (escrita).",
             "write"),
}
for _n, (_native, _desc, _kind) in _WORKSPACE.items():
    BUILTIN_TOOLS[f"workspace.{_n}"] = ToolInfo(f"workspace.{_n}", "workspace", _n, _desc,
                                                native=_native, kind=_kind)
NATIVE_TOOLS = frozenset(n for n, _, _ in _WORKSPACE.values())
WRITE_TOOLS = frozenset(t.sdk_name for t in BUILTIN_TOOLS.values() if t.kind == "write")
BUILTIN_SERVERS = MCP_SERVERS

CONTRACTS: dict[str, TypeAdapter[Any]] = dict(LOADED.contracts)


def catalog_dict() -> dict[str, Any]:
    """Forma serializável (para o editor)."""
    return {
        "tools": [t.__dict__ | {"sdk_name": t.sdk_name} for t in BUILTIN_TOOLS.values()],
        "contracts": {k: v.json_schema() for k, v in CONTRACTS.items()},
    }

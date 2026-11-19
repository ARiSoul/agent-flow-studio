"""Extensões locais: ferramentas, contratos e relatórios específicos de um domínio/empresa.

Cada extensão é um pacote Python em `extensions/<nome>/` (ou na pasta de `AFS_EXTENSIONS_DIR`) que
pode expor:

- `TOOL_GROUPS: dict[str, list[SdkMcpTool]]` — servidores MCP em processo ("servidor" → ferramentas);
- `CONTRACTS: dict[str, TypeAdapter]` — contratos que os artefactos JSON podem exigir;
- `REPORTS: dict[str, Callable[[str], Path]]` — flow → função que gera um relatório no fim da execução.

As extensões não vão para o repositório público (ver `.gitignore`): servem para código interno.
Os nomes não podem colidir com os do núcleo nem entre extensões.
"""
from __future__ import annotations

import importlib
import os
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

CORE_SERVERS = frozenset({"artifacts", "connections", "flow_control", "workspace"})


@dataclass
class Loaded:
    names: list[str] = field(default_factory=list)
    tool_groups: dict[str, list[Any]] = field(default_factory=dict)
    contracts: dict[str, Any] = field(default_factory=dict)
    reports: dict[str, Callable[[str], Path]] = field(default_factory=dict)


def extensions_dir() -> Path:
    return Path(os.getenv("AFS_EXTENSIONS_DIR", "extensions"))


def _load() -> Loaded:
    out = Loaded()
    base = extensions_dir().resolve()
    if os.getenv("AFS_NO_EXTENSIONS") == "1" or not base.is_dir():
        return out
    if str(base) not in sys.path:
        sys.path.insert(0, str(base))
    for d in sorted(p for p in base.iterdir() if (p / "__init__.py").is_file()):
        mod = importlib.import_module(d.name)
        out.names.append(d.name)
        for server, tools in (getattr(mod, "TOOL_GROUPS", None) or {}).items():
            if server in CORE_SERVERS or server in out.tool_groups:
                raise RuntimeError(f"extensão {d.name}: servidor {server!r} já existe")
            out.tool_groups[server] = list(tools)
        for name, adapter in (getattr(mod, "CONTRACTS", None) or {}).items():
            if name in out.contracts:
                raise RuntimeError(f"extensão {d.name}: contrato {name!r} repetido")
            out.contracts[name] = adapter
        out.reports.update(getattr(mod, "REPORTS", None) or {})
    return out


LOADED = _load()

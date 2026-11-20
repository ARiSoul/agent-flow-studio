"""Local extensions: tools, contracts and reports specific to a domain/company.

Each extension is a Python package in `extensions/<name>/` (or in the `AFS_EXTENSIONS_DIR` folder)
that can expose:

- `TOOL_GROUPS: dict[str, list[SdkMcpTool]]` — in-process MCP servers ("server" → tools);
- `CONTRACTS: dict[str, TypeAdapter]` — contracts that JSON artifacts can require;
- `REPORTS: dict[str, Callable[[str], Path]]` — flow → function that writes a run report.

A `templates/` folder inside the extension adds flow templates (see builder/templates.py) and an
`agents/` folder adds agents to the agent library (see builder/library.py).

Extensions do not go into the public repository (see `.gitignore`): they hold internal code.
Names cannot collide with the core names or between extensions.
"""
from __future__ import annotations

import importlib
import os
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

CORE_SERVERS = frozenset({"artifacts", "connections", "flow_control", "io", "workspace"})


@dataclass
class Loaded:
    names: list[str] = field(default_factory=list)
    tool_groups: dict[str, list[Any]] = field(default_factory=dict)
    contracts: dict[str, Any] = field(default_factory=dict)
    reports: dict[str, Callable[[str], Path]] = field(default_factory=dict)
    template_dirs: list[tuple[str, Path]] = field(default_factory=list)  # (extension, folder)
    agent_dirs: list[tuple[str, Path]] = field(default_factory=list)  # (extension, folder)


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
                raise RuntimeError(f"extension {d.name}: server {server!r} already exists")
            out.tool_groups[server] = list(tools)
        for name, adapter in (getattr(mod, "CONTRACTS", None) or {}).items():
            if name in out.contracts:
                raise RuntimeError(f"extension {d.name}: duplicate contract {name!r}")
            out.contracts[name] = adapter
        out.reports.update(getattr(mod, "REPORTS", None) or {})
        if (d / "templates").is_dir():
            out.template_dirs.append((d.name, d / "templates"))
        if (d / "agents").is_dir():
            out.agent_dirs.append((d.name, d / "agents"))
    return out


LOADED = _load()


def extension_root(name: str) -> Path:
    return extensions_dir().resolve() / name


def extension_flow_dirs() -> list[tuple[str, Path]]:
    """(extension, extensions/<name>/flows) for each loaded extension (the folder may not exist
    yet: it is created when the first flow is saved there)."""
    return [(n, extension_root(n) / "flows") for n in LOADED.names]


def extension_agent_dirs() -> list[tuple[str, Path]]:
    return [(n, extension_root(n) / "agents") for n in LOADED.names]


def extension_connection_files() -> list[tuple[str, Path]]:
    """extensions/<name>/connections.yaml: connector definitions shared through the extension
    (no secrets there: only ${VAR} or keyring references, as in the local file)."""
    return [(n, extension_root(n) / "connections.yaml") for n in LOADED.names]

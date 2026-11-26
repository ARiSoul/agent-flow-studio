"""Builder catalog: what a flow can use.

- Built-in tools: those of the in-process MCP servers in `registry.py` (id "server.tool").
- Contracts: pydantic models provided by the extensions (see extensions.py).
External MCPs are not here: each flow declares them (see spec.ExternalMcp).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from pydantic import TypeAdapter

from ..extensions import LOADED
from ..registry import _GROUPS, MCP_SERVERS


@dataclass(frozen=True)
class ToolInfo:
    id: str  # "server.tool"
    server: str
    name: str
    description: str
    writes_local_only: bool = False  # writes, but only in runs/<id>/ (artifacts)
    native: str | None = None  # native Claude Code tool (workspace.*)
    kind: str = "read"  # read · write_local (runs/) · write (effect outside the project)

    @property
    def sdk_name(self) -> str:
        return self.native or f"mcp__{self.server}__{self.name}"


BUILTIN_TOOLS: dict[str, ToolInfo] = {
    f"{server}.{t.name}": ToolInfo(
        f"{server}.{t.name}", server, t.name, t.description,
        writes_local_only=(f"{server}.{t.name}" in ("artifacts.save_artifact", "io.set_output",
                                                      "preview.screenshot")),
    )
    for server, tools in _GROUPS.items()
    for t in tools
}
for _t in BUILTIN_TOOLS.values():  # write classification
    object.__setattr__(_t, "kind", "write_local" if _t.writes_local_only
                       else "write" if _t.id == "connections.http_write" else "read")

# Native tools, only inside the flow workspace (the hook confines the paths).
_WORKSPACE = {
    "read": ("Read", "Reads a file from the flow workspace.", "read"),
    "glob": ("Glob", "Finds files by pattern in the workspace.", "read"),
    "grep": ("Grep", "Searches text in the workspace files.", "read"),
    "edit": ("Edit", "Edits a workspace file (write).", "write"),
    "write": ("Write", "Creates or replaces a workspace file (write).", "write"),
    "bash": ("Bash", "Runs a command in the workspace folder: git, tests, gh, az (write).",
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
    """Serializable form (for the editor)."""
    return {
        "tools": [t.__dict__ | {"sdk_name": t.sdk_name} for t in BUILTIN_TOOLS.values()],
        "contracts": {k: v.json_schema() for k, v in CONTRACTS.items()},
    }

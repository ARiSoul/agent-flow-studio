"""What the run log shows about a tool call: a short detail of the call and the error text.

Only names and short values the agent chose (an action, a path, an artifact name) and the error
messages of the tools, cut short; never file contents or what a tool returned.
"""
from __future__ import annotations

from typing import Any

MAX_DETAIL = 120
MAX_ERROR = 300

# tool → the input fields worth showing (in this order)
_DETAIL_KEYS: dict[str, tuple[str, ...]] = {
    "run_action": ("connection", "action"),
    "http_get": ("connection", "path"),
    "http_write": ("connection", "method", "path"),
    "save_artifact": ("name",),
    "read_artifact": ("name",),
    "set_output": ("name",),
    "get_inputs": (),
    "read_file": ("path",),
    "next_steps": ("node",),
    "Read": ("file_path",),
    "Grep": ("pattern", "path"),
    "Glob": ("pattern",),
    "Edit": ("file_path",),
    "Write": ("file_path",),
    "Bash": ("command",),
    "git": ("args",),
}


def short_tool(name: str) -> str:
    """mcp__server__tool → tool."""
    return name.split("__")[-1] if name.startswith("mcp__") else name


def _clip(s: str, n: int) -> str:
    s = " ".join(s.split())
    return s if len(s) <= n else s[: n - 1] + "…"


def tool_detail(name: str, data: Any) -> str:
    """One short line about the call, e.g. "tickets · list_users" (empty when nothing helps)."""
    if not isinstance(data, dict):
        return ""
    parts = []
    for k in _DETAIL_KEYS.get(short_tool(name), ()):
        v = data.get(k)
        if v not in (None, "", [], {}):
            parts.append(" ".join(map(str, v)) if isinstance(v, list) else str(v))
    if short_tool(name) == "run_action" and isinstance(data.get("params"), dict) and data["params"]:
        parts.append(", ".join(f"{k}={v}" for k, v in data["params"].items()))
    return _clip(" · ".join(parts), MAX_DETAIL)


def error_text(content: Any) -> str:
    """The text of a failed tool result, cut short."""
    if isinstance(content, list):
        content = " ".join(str(c.get("text", "")) if isinstance(c, dict) else str(c)
                           for c in content)
    return _clip(str(content or ""), MAX_ERROR)

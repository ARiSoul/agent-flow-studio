"""Safety net: only the flow tools (and delegation to subagents) are allowed, each subagent
uses only its own, and every call is logged in runs/audit.jsonl."""
from __future__ import annotations

import json
import time
from collections.abc import Awaitable, Callable, Iterable, Mapping
from typing import TYPE_CHECKING, Any

from claude_agent_sdk import HookContext, HookJSONOutput, HookMatcher

from .config import settings

if TYPE_CHECKING:
    from .policy import WritePolicy

IO_PREFIX = "mcp__io__"  # io tools: the `agent` argument must be the calling agent
DELEGATION_TOOL = "Agent"
# "Task" is the old name, still accepted as an alias by the CLI.
DELEGATION_TOOLS = frozenset({DELEGATION_TOOL, "Task"})
CONNECTION_TOOL = "mcp__connections__http_get"

GuardFn = Callable[[dict[str, Any], str | None, HookContext], Awaitable[HookJSONOutput]]


def _deny(reason: str) -> HookJSONOutput:
    return {"hookSpecificOutput": {
        "hookEventName": "PreToolUse",
        "permissionDecision": "deny",
        "permissionDecisionReason": reason,
    }}


def make_guard(allowed: Iterable[str], per_agent: Mapping[str, Iterable[str]] | None = None,
               per_agent_connections: Mapping[str, Iterable[str]] | None = None,
               policy: WritePolicy | None = None, fixed_agent: str | None = None) -> GuardFn:
    """allowed: closed list of the flow (SDK names); per_agent: subagent → its tools;
    per_agent_connections: subagent → connectors it can use with http_get.
    fixed_agent: the session IS this agent (graph engine): no delegation, its rules always."""
    allowed_set = frozenset(allowed) | (frozenset() if fixed_agent else DELEGATION_TOOLS)
    agent_tools = {k: frozenset(v) for k, v in (per_agent or {}).items()}
    agent_conns = {k: frozenset(v) for k, v in (per_agent_connections or {}).items()}

    async def guard_and_audit(
        input_data: dict[str, Any], tool_use_id: str | None, context: HookContext
    ) -> HookJSONOutput:
        name = input_data.get("tool_name", "")
        agent = fixed_agent or input_data.get("agent_type")
        settings.runs_dir.mkdir(parents=True, exist_ok=True)
        with (settings.runs_dir / "audit.jsonl").open("a", encoding="utf-8") as f:
            f.write(json.dumps({
                "ts": time.time(), "session_id": input_data.get("session_id"),
                "tool": name, "tool_use_id": tool_use_id,
                "agent": agent, "input": _audit_input(name, input_data.get("tool_input")),
            }, ensure_ascii=False) + "\n")
        # Closed list: an "mcp__" prefix is not enough (other MCP servers could be loaded).
        if name not in allowed_set:
            return _deny(f"Tool '{name}' is not allowed in this flow (closed list).")
        if agent in agent_tools and name not in agent_tools[agent]:
            return _deny(f"Tool '{name}' is not assigned to subagent '{agent}'.")
        if name.startswith(IO_PREFIX):
            who = (input_data.get("tool_input") or {}).get("agent")
            if agent is None or who != agent:
                return _deny(f"{name}: 'agent' must be your own name ({agent or 'orchestrator'}).")
        if name == CONNECTION_TOOL:
            conn = (input_data.get("tool_input") or {}).get("connection")
            if conn not in agent_conns.get(agent, frozenset()):
                return _deny(f"Connector '{conn}' is not assigned to subagent '{agent}'.")
        if policy is not None:
            why = await policy.check(name, agent, input_data.get("tool_input") or {})
            if why:
                return _deny(f"{name}: {why}")
        return {}

    return guard_and_audit


def _audit_input(name: str, data: Any) -> Any:
    """File contents do not go to the audit log (only the path and the size)."""
    if not isinstance(data, dict):
        return data
    if name in ("Write", "Edit"):
        out = {k: v for k, v in data.items() if k not in ("content", "old_string", "new_string")}
        out["chars"] = sum(len(str(data.get(k, ""))) for k in ("content", "new_string"))
        return out
    if name == "mcp__connections__http_write" and "body" in data:
        return {**data, "body": str(data["body"])[:500]}
    return data


def make_hooks(guard: GuardFn) -> dict:
    # on_failure="block": if the hook fails (exception, invalid return), the call is blocked.
    return {"PreToolUse": [HookMatcher(hooks=[guard], on_failure="block")]}

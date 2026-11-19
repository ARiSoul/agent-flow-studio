"""Rede de segurança: só as ferramentas do flow (e a delegação a subagentes) são permitidas, cada
subagente só usa as suas, e todas as chamadas ficam registadas em runs/audit.jsonl."""
from __future__ import annotations

import json
import time
from collections.abc import Awaitable, Callable, Iterable, Mapping
from typing import TYPE_CHECKING, Any

from claude_agent_sdk import HookContext, HookJSONOutput, HookMatcher

from .config import settings

if TYPE_CHECKING:
    from .policy import WritePolicy

DELEGATION_TOOL = "Agent"
# "Task" é o nome antigo, ainda aceite como alias pelo CLI.
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
               policy: WritePolicy | None = None) -> GuardFn:
    """allowed: lista fechada do flow (nomes SDK); per_agent: subagente → as suas ferramentas;
    per_agent_connections: subagente → conectores que pode usar com http_get."""
    allowed_set = frozenset(allowed) | DELEGATION_TOOLS
    agent_tools = {k: frozenset(v) for k, v in (per_agent or {}).items()}
    agent_conns = {k: frozenset(v) for k, v in (per_agent_connections or {}).items()}

    async def guard_and_audit(
        input_data: dict[str, Any], tool_use_id: str | None, context: HookContext
    ) -> HookJSONOutput:
        name = input_data.get("tool_name", "")
        agent = input_data.get("agent_type")
        settings.runs_dir.mkdir(parents=True, exist_ok=True)
        with (settings.runs_dir / "audit.jsonl").open("a", encoding="utf-8") as f:
            f.write(json.dumps({
                "ts": time.time(), "session_id": input_data.get("session_id"),
                "tool": name, "tool_use_id": tool_use_id,
                "agent": agent, "input": _audit_input(name, input_data.get("tool_input")),
            }, ensure_ascii=False) + "\n")
        # Lista fechada: um prefixo "mcp__" não chega (outros servidores MCP podiam ser carregados).
        if name not in allowed_set:
            return _deny(f"Ferramenta '{name}' não permitida neste flow (lista fechada).")
        if agent in agent_tools and name not in agent_tools[agent]:
            return _deny(f"Ferramenta '{name}' não atribuída ao subagente '{agent}'.")
        if name == CONNECTION_TOOL:
            conn = (input_data.get("tool_input") or {}).get("connection")
            if conn not in agent_conns.get(agent, frozenset()):
                return _deny(f"Conector '{conn}' não atribuído ao subagente '{agent}'.")
        if policy is not None:
            why = await policy.check(name, agent, input_data.get("tool_input") or {})
            if why:
                return _deny(f"{name}: {why}")
        return {}

    return guard_and_audit


def _audit_input(name: str, data: Any) -> Any:
    """Conteúdos de ficheiros não vão para o audit (só o caminho e o tamanho)."""
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
    # on_failure="block": se o hook falhar (exceção, retorno inválido), a chamada é bloqueada.
    return {"PreToolUse": [HookMatcher(hooks=[guard], on_failure="block")]}

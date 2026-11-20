"""Write policy applied by the guard hook (it does not depend on the model).

- File tools (Read/Edit/Write/Glob/Grep) work only inside the flow workspace; Bash runs with the
  workspace as its working folder and denies dangerous commands (force push, push to protected
  branches, deleting the root/home, downloading and running scripts…).
- Writes (Edit, Write, Bash, connections.http_write) follow the agent or flow autonomy:
  `approve` asks for approval on every write, `approve_prod` only when the target is production,
  `auto` never stops. With no approver (e.g. the CLI), a write that needs approval is denied.
- The orchestrator never uses file or write tools: only the subagents do.

Note: Bash is not a sandbox. A command can leave the folder; that is why Bash counts as a write
(it asks for approval in the approve/approve_prod modes) and the deny list is only an extra net.
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
CONN_READ_TOOL = "mcp__connections__http_get"
RUN_ACTION_TOOL = "mcp__connections__run_action"
_FILE_TOOLS = {"Read": "file_path", "Edit": "file_path", "Write": "file_path",
               "Glob": "path", "Grep": "path"}
_DENY_BASH = [
    (r"\bgit\s+push\b[^\n]*(\s--force\b|\s-f\b|\s--force-with-lease\b|\s\+\S)", "force push"),
    (r"\brm\s+-[a-zA-Z]*r[a-zA-Z]*f?\s+(/|~|\$HOME|[A-Za-z]:[\\/]?)(\s|$)", "deleting root/home"),
    (r"Remove-Item[^\n]*-Recurse[^\n]*\s([A-Za-z]:\\?|~|/)(\s|$)", "deleting root/home"),
    (r"\b(curl|wget|iwr|Invoke-WebRequest)\b[^\n|]*\|\s*(sh|bash|iex|pwsh|powershell)\b",
     "download and run"),
    (r"\b(shutdown|format|mkfs|diskpart)\b", "system command"),
    (r"\bgit\s+config\s+--global\b", "changing the global git config"),
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
    """Readable summary and (truncated) detail of what the write will do."""
    if tool == "Write":
        size = len(str(args.get("content", "")))
        return (f"Create/replace {args.get('file_path')} ({size} characters)",
                {"file_path": args.get("file_path"), "content": _clip(args.get("content", ""))})
    if tool == "Edit":
        return (f"Edit {args.get('file_path')}",
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
    if tool == RUN_ACTION_TOOL:
        return (f"Action {args.get('connection')}.{args.get('action')}",
                {"connection": args.get("connection"), "action": args.get("action"),
                 "params": _clip(args.get("params"))})
    return tool, {}


@dataclass
class WritePolicy:
    flow_autonomy: str = "approve"
    agent_autonomy: dict[str, str] = field(default_factory=dict)
    workspace: Path | None = None
    workspace_env: str = "dev"
    protected_branches: list[str] = field(default_factory=lambda: ["main", "master"])
    write_connections: dict[str, set[str]] = field(default_factory=dict)  # agent → connectors
    read_connections: dict[str, set[str]] = field(default_factory=dict)  # agent → connectors
    connection_env: dict[str, str] = field(default_factory=dict)
    # named actions: connector → {action → "read" | "write"}; connectors usable only by actions
    connection_actions: dict[str, dict[str, str]] = field(default_factory=dict)
    actions_only: set[str] = field(default_factory=set)
    approver: Approver | None = None
    # Step-by-step tests (the editor runner sets these fields before each segment):
    delegation_limit: int | None = None  # number of delegations allowed in this segment
    delegation_only: frozenset[str] | None = None  # allowed subagents (None = all)
    delegations: int = 0

    def limit_segment(self, limit: int | None, only: frozenset[str] | None = None) -> None:
        self.delegation_limit, self.delegation_only, self.delegations = limit, only, 0

    def _delegation(self, args: Mapping[str, Any]) -> str | None:
        sub = str(args.get("subagent_type", ""))
        if self.delegation_only is not None and sub not in self.delegation_only:
            only = ", ".join(sorted(self.delegation_only))
            return f"test mode: you can only delegate to {only}. Stop now and report the state."
        if self.delegation_limit is not None and self.delegations >= self.delegation_limit:
            return ("step mode: this step is done. Do not delegate again; stop now "
                    "with a short summary of the step and of what comes next.")
        self.delegations += 1
        return None

    def _inside(self, raw: str | None, required: bool) -> str | None:
        if not raw:
            return "the path is missing" if required else None
        assert self.workspace is not None
        p = Path(raw)
        p = (p if p.is_absolute() else self.workspace / p).resolve()
        ws = self.workspace.resolve()
        if p != ws and ws not in p.parents:
            return f"path outside the workspace ({ws})"
        return None

    def _bash_denied(self, cmd: str) -> str | None:
        for pattern, why in _DENY_BASH:
            if re.search(pattern, cmd, re.IGNORECASE):
                return why
        for b in self.protected_branches:
            if re.search(rf"\bgit\s+push\b[^\n]*\b(origin\s+)?(HEAD:)?{re.escape(b)}\b", cmd):
                return f"push to the protected branch {b!r} (open a PR)"
        return None

    def level(self, agent: str | None) -> str:
        return self.agent_autonomy.get(agent or "", self.flow_autonomy)

    async def check(self, tool: str, agent: str | None, args: Mapping[str, Any]) -> str | None:
        """Returns the reason for the denial, or None if the call can go ahead."""
        if tool in ("Agent", "Task") and agent is None:
            return self._delegation(args)
        if tool in (CONN_READ_TOOL, CONN_WRITE_TOOL) and str(args.get("connection")) in \
                self.actions_only:
            return (f"connector {args.get('connection')!r} can only be used through its named "
                    "actions (run_action)")
        if tool == RUN_ACTION_TOOL:
            return await self._action(agent, args)
        if tool not in NATIVE_TOOLS and tool not in WRITE_TOOLS:
            return None
        if agent is None:
            return "the orchestrator does not write or touch files; delegate to a subagent"
        env = self.workspace_env
        if tool in NATIVE_TOOLS:
            if self.workspace is None:
                return "the flow has no workspace"
            if tool in _FILE_TOOLS:
                why = self._inside(args.get(_FILE_TOOLS[tool]), tool in ("Read", "Edit", "Write"))
                if why:
                    return why
                if tool == "Glob" and ".." in str(args.get("pattern", "")):
                    return "pattern contains '..'"
            if tool == "Bash":
                why = self._bash_denied(str(args.get("command", "")))
                if why:
                    return f"command denied: {why}"
        if tool == CONN_WRITE_TOOL:
            conn = str(args.get("connection", ""))
            if conn not in self.write_connections.get(agent, set()):
                return f"connector {conn!r} has no write access assigned to subagent {agent!r}"
            env = self.connection_env.get(conn, "prod")  # unknown = the most restricted
        if tool not in WRITE_TOOLS:
            return None
        return await self._approve(tool, agent, args, env)

    async def _action(self, agent: str | None, args: Mapping[str, Any]) -> str | None:
        """run_action: the action must be declared on the connector; read actions need the
        connector assigned to the agent; write actions need write access and follow the autonomy."""
        if agent is None:
            return "the orchestrator does not use connectors; delegate to a subagent"
        conn, name = str(args.get("connection", "")), str(args.get("action", ""))
        access = self.connection_actions.get(conn, {}).get(name)
        if access is None:
            known = ", ".join(self.connection_actions.get(conn, {})) or "none"
            return f"connector {conn!r} has no action {name!r} (actions: {known})"
        writable = self.write_connections.get(agent, set())
        if access == "read":
            if conn not in self.read_connections.get(agent, set()) | writable:
                return f"connector {conn!r} is not assigned to subagent {agent!r}"
            return None
        if conn not in writable:
            return f"connector {conn!r} has no write access assigned to subagent {agent!r}"
        return await self._approve(RUN_ACTION_TOOL, agent, args,
                                   self.connection_env.get(conn, "prod"))

    async def _approve(self, tool: str, agent: str | None, args: Mapping[str, Any],
                       env: str) -> str | None:
        level = self.level(agent)
        if level == "auto" or (level == "approve_prod" and env != "prod"):
            return None
        if self.approver is None:
            return ("this write requires human approval; run the flow in the editor (Run) "
                    "or change the autonomy")
        summary, detail = describe(tool, args)
        ok, comment = await self.approver(ApprovalRequest(agent, tool, env, summary, detail))
        if not ok:
            return "denied by the human" + (f": {comment}" if comment else "") + \
                ". Do not repeat this write; continue without it or report."
        return None

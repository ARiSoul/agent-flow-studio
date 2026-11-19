import asyncio
from dataclasses import replace

import pytest

from agent_flow_studio import hooks
from agent_flow_studio.config import settings
from agent_flow_studio.orchestrator import build_options
from agent_flow_studio.registry import ALL_TOOLS

GUARD = hooks.make_guard(ALL_TOOLS, {"triage": ["mcp__artifacts__read_artifact"]},
                         {"reader": ["devops"]})
# tools with extra rules (connector, agent identity) have their own tests
NO_CONN = [t for t in ALL_TOOLS if t not in (hooks.CONNECTION_TOOL, "mcp__connections__http_write")
           and not t.startswith(hooks.IO_PREFIX)]


@pytest.fixture(autouse=True)
def _tmp_runs(tmp_path, monkeypatch):
    monkeypatch.setattr(hooks, "settings", replace(settings, runs_dir=tmp_path))


def _call(name: str, agent: str | None = None, tool_input: dict | None = None) -> dict:
    data = {"tool_name": name, "tool_input": tool_input or {}, "agent_type": agent,
            "session_id": "s"}
    return asyncio.run(GUARD(data, "id", {}))


@pytest.mark.parametrize("name", [*NO_CONN, "Agent", "Task"])
def test_allowed(name):
    assert _call(name) == {}


@pytest.mark.parametrize("name", ["Bash", "Write", "Read", "mcp__other__any", ""])
def test_denied(name):
    out = _call(name)
    assert out["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_per_agent_restriction():
    assert _call("mcp__artifacts__read_artifact", "triage") == {}
    out = _call("mcp__artifacts__save_artifact", "triage")
    assert out["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert _call("mcp__artifacts__save_artifact", "other_agent_without_map") == {}


def test_connection_restricted_per_agent():
    tool = hooks.CONNECTION_TOOL
    assert _call(tool, "reader", {"connection": "devops", "path": "/"}) == {}
    for agent, conn in (("reader", "github"), ("triage", "devops"), (None, "devops")):
        out = _call(tool, agent, {"connection": conn, "path": "/"})
        assert out["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_audit_written():
    _call("Bash")
    text = (hooks.settings.runs_dir / "audit.jsonl").read_text(encoding="utf-8")
    assert text.count("\n") == 1 and '"session_id": "s"' in text


def test_options():
    o = build_options("pipeline")
    assert o.tools == ["Agent"]
    assert "Agent" in o.allowed_tools
    assert set(o.allowed_tools) == {"mcp__artifacts__read_artifact", "mcp__artifacts__save_artifact",
                              "Agent", "mcp__flow_control__reach_gate"}
    assert "flow_control" in o.mcp_servers
    assert o.permission_mode == "dontAsk"
    assert o.setting_sources == []
    assert o.hooks["PreToolUse"][0].on_failure == "block"
    assert set(o.agents) == {"triage", "identity", "current_state", "log_investigator",
                             "cross_checker", "diagnostician", "responder"}

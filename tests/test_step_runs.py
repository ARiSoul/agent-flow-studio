"""Test while building: step by step, until an agent and isolated agent (fake SDK that passes
the delegations through the real hook, as the CLI does)."""
import asyncio
import shutil
from dataclasses import replace
from pathlib import Path

import pytest
from claude_agent_sdk import (
    AssistantMessage,
    ResultMessage,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
)

from agent_flow_studio import hooks
from agent_flow_studio.config import settings
from agent_flow_studio.editor import runner as runner_mod
from agent_flow_studio.editor.runner import RunError, Runner

PIPELINE = Path(__file__).parent / "fixtures" / "flows" / "pipeline"

ORDER = ["triage", "identity", "current_state", "log_investigator", "cross_checker",
         "diagnostician", "responder"]


@pytest.fixture
def env(tmp_path: Path, monkeypatch):
    shutil.copytree(PIPELINE, tmp_path / "flows/pipeline")
    s = replace(settings, runs_dir=tmp_path / "runs")
    monkeypatch.setattr(runner_mod, "settings", s)
    monkeypatch.setattr(hooks, "settings", s)
    return tmp_path


def make_query(state: dict):
    """Fake orchestrator: tries to delegate to the missing agents, in order, until it is blocked."""
    state.setdefault("calls", [])

    async def q(prompt, options):
        state["calls"].append({"prompt": prompt, "agents": sorted(options.agents),
                               "system": options.system_prompt})
        guard = options.hooks["PreToolUse"][0].hooks[0]
        for i, sub in enumerate(a for a in ORDER if a not in state.setdefault("ran", [])):
            if sub not in options.agents:
                sub = next(iter(options.agents))
            tid = f"d{len(state['calls'])}-{i}"
            yield AssistantMessage(content=[ToolUseBlock(tid, "Agent", {"subagent_type": sub})],
                                   model="m")
            out = await guard({"tool_name": "Agent", "agent_type": None, "session_id": "s",
                               "tool_input": {"subagent_type": sub}}, tid, {})
            if out:
                reason = out["hookSpecificOutput"]["permissionDecisionReason"]
                yield UserMessage(content=[ToolResultBlock(tid, reason, True)])
                break
            state["ran"].append(sub)
            yield UserMessage(content=[ToolResultBlock(tid, "ok", False)])
            if state.get("stop_after") == sub:
                break
        yield ResultMessage(subtype="success", duration_ms=1, duration_api_ms=1, is_error=False,
                            num_turns=1, session_id="sess", total_cost_usd=0.01)

    return q


async def wait(r):
    while r.task and not r.task.done():
        await asyncio.sleep(0.01)


def test_step_mode_pauses_after_each_agent(env):
    st: dict = {}
    rn = Runner(env / "flows", make_query(st))

    async def scenario():
        r = rn.start("pipeline", "INC1", mode="step")
        await wait(r)
        assert r.status == "paused" and r.done_agents == {"triage"}
        assert any(e["type"] == "agent_blocked" and e["agent"] == "identity" for e in r.events)
        assert "Step-by-step test mode" in st["calls"][0]["prompt"]
        rn.continue_(r.id, "step")
        await wait(r)
        assert r.status == "paused" and r.done_agents == {"triage", "identity"}
        rn.continue_(r.id, "full")
        await wait(r)
        return r

    r = asyncio.run(scenario())
    assert r.status == "done" and r.done_agents == set(ORDER)
    assert all(c["prompt"].startswith("Continue the flow") for c in st["calls"][1:])


def test_until_agent_runs_steps_automatically(env):
    st: dict = {}
    rn = Runner(env / "flows", make_query(st))

    async def scenario():
        r = rn.start("pipeline", "INC2", mode="until", target="current_state")
        await wait(r)
        return r

    r = asyncio.run(scenario())
    assert r.status == "paused" and r.done_agents == {"triage", "identity", "current_state"}
    assert len(st["calls"]) == 3  # one segment per agent
    with pytest.raises(RunError):
        rn.decide(r.id, "approve", "")  # a pause is not a gate


def test_agent_mode_runs_only_target(env):
    st: dict = {}
    rn = Runner(env / "flows", make_query(st))

    async def scenario():
        r = rn.start("pipeline", "INC3", mode="agent", target="diagnostician",
                     instruction="Diagnose with what there is.")
        await wait(r)
        return r

    r = asyncio.run(scenario())
    call = st["calls"][0]
    assert call["agents"] == ["diagnostician"] and "TEST orchestrator" in call["system"]
    assert call["prompt"] == "Diagnose with what there is."
    assert r.done_agents == {"diagnostician"} and r.status == "done"


def test_bad_target_and_continue_rules(env):
    rn = Runner(env / "flows", make_query({}))
    with pytest.raises(RunError):
        rn.start("pipeline", "INC4", mode="until", target="ghost")

    async def scenario():
        r = rn.start("pipeline", "INC5")  # full
        await wait(r)
        with pytest.raises(RunError):
            rn.continue_(r.id, "step")  # not paused
        return r

    assert asyncio.run(scenario()).status == "done"


def test_full_mode_has_no_delegation_limit(env):
    st: dict = {}
    rn = Runner(env / "flows", make_query(st))

    async def scenario():
        r = rn.start("pipeline", "INC6")
        await wait(r)
        return r

    r = asyncio.run(scenario())
    assert r.done_agents == set(ORDER) and len(st["calls"]) == 1


def test_stop_closes_paused_run(env):
    rn = Runner(env / "flows", make_query({}))

    async def scenario():
        r = rn.start("pipeline", "INC7", mode="step")
        await wait(r)
        assert r.status == "paused"
        rn.stop(r.id)
        return r

    r = asyncio.run(scenario())
    assert r.status == "stopped" and r.events[-1] == {**r.events[-1], "type": "end",
                                                       "status": "stopped"}

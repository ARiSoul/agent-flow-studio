"""Execuções a partir do editor: trace, gates, decisões e API (com um SDK falso)."""
import asyncio
import json
import shutil
from dataclasses import replace
from pathlib import Path

import pytest
from claude_agent_sdk import (
    AssistantMessage,
    ResultMessage,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
)
from fastapi.testclient import TestClient

from agent_flow_studio.config import settings
from agent_flow_studio.editor import runner as runner_mod
from agent_flow_studio.editor.runner import RunError, Runner
from agent_flow_studio.editor.server import create_app

PIPELINE = Path(__file__).parent / "fixtures" / "flows" / "pipeline"

H = {"X-Flow-Editor": "1"}
GATE = "mcp__flow_control__reach_gate"


def _am(*blocks, parent=None):
    return AssistantMessage(content=list(blocks), model="m", parent_tool_use_id=parent)


def _result(sid="sess-1"):
    return ResultMessage(subtype="success", duration_ms=1, duration_api_ms=1, is_error=False,
                         num_turns=3, session_id=sid, total_cost_usd=0.01)


def fake_query(script):
    calls = []

    async def q(prompt, options):
        calls.append({"prompt": prompt, "resume": options.resume})
        for m in script(len(calls)):
            yield m

    q.calls = calls
    return q


def first_run(_n):
    return [
        _am(TextBlock("A começar.")),
        _am(ToolUseBlock("d1", "Agent", {"subagent_type": "triage", "description": "triagem"})),
        _am(ToolUseBlock("t1", "mcp__artifacts__read_artifact", {"number": "INC1"}), parent="d1"),
        _am(ToolUseBlock("t2", "mcp__artifacts__save_artifact",
                         {"ticket": "INC1", "name": "01_triage.json", "content": "{}"}), parent="d1"),
        UserMessage(content=[ToolResultBlock("d1", "ok", False)]),
        _am(ToolUseBlock("g1", GATE, {"gate": "analyst_answers", "reason": "falta a data z"})),
        _result(),
    ]


@pytest.fixture
def env(tmp_path: Path, monkeypatch):
    shutil.copytree(PIPELINE, tmp_path / "flows/pipeline")
    monkeypatch.setattr(runner_mod, "settings", replace(settings, runs_dir=tmp_path / "runs"))
    return tmp_path


async def _wait(r):
    while r.task and not r.task.done():
        await asyncio.sleep(0.01)


def test_run_reaches_gate_then_resumes(env: Path):
    q = fake_query(lambda n: first_run(n) if n == 1 else [_am(TextBlock("Continuo.")), _result()])
    rn = Runner(env / "flows", q)

    async def scenario():
        r = rn.start("pipeline", "INC1")
        await _wait(r)
        assert r.status == "gate" and r.gate["gate"] == "analyst_answers"
        assert r.session_id == "sess-1"
        types = [e["type"] for e in r.events]
        assert types == ["start", "text", "delegate", "tool", "tool", "agent_done", "gate",
                         "result", "end"]
        tool_ev = [e for e in r.events if e["type"] == "tool"]
        assert tool_ev[0] == {**tool_ev[0], "agent": "triage", "tool": "read_artifact"}
        assert "number" not in json.dumps(tool_ev)  # argumentos não ficam no trace
        assert tool_ev[1]["artifact"] == "01_triage.json"
        rn.decide(r.id, "answer", "z = 2026-11-01")
        await _wait(r)
        assert r.status == "done" and q.calls[1]["resume"] == "sess-1"
        assert "RESPONDIDO" in q.calls[1]["prompt"] and "2026-11-01" in q.calls[1]["prompt"]
        return r

    r = asyncio.run(scenario())
    runs = env / "runs/INC1"
    decisions = [json.loads(x) for x in (runs / "decisions.jsonl").read_text("utf-8").splitlines()]
    assert decisions[0]["gate"] == "analyst_answers" and decisions[0]["decision"] == "answer"
    assert len((runs / "events.jsonl").read_text("utf-8").splitlines()) == len(r.events)


def test_terminal_gate_records_decision_without_resuming(env: Path):
    def script(n):
        return [_am(ToolUseBlock("g", GATE, {"gate": "approve_export", "reason": "x"})), _result()]

    q = fake_query(script)
    rn = Runner(env / "flows", q)

    async def scenario():
        r = rn.start("pipeline", "INC2")
        await _wait(r)
        rn.decide(r.id, "approve", "ok")
        return r

    r = asyncio.run(scenario())
    assert r.status == "done" and len(q.calls) == 1  # sem ligações de saída: não retoma


def test_stop_gate_ends_run_and_cannot_be_approved(env: Path):
    q = fake_query(lambda n: [_am(ToolUseBlock("g", GATE, {"gate": "user_not_found",
                                                           "reason": "x"})), _result()])
    rn = Runner(env / "flows", q)

    async def scenario():
        r = rn.start("pipeline", "INC3")
        await _wait(r)
        return r

    r = asyncio.run(scenario())
    assert r.status == "done" and r.gate["kind"] == "stop"
    with pytest.raises(RunError):
        rn.decide(r.id, "approve", "")


def test_one_run_at_a_time_and_stop(env: Path):
    async def slow(prompt, options):
        await asyncio.sleep(10)
        yield _result()

    rn = Runner(env / "flows", slow)

    async def scenario():
        r = rn.start("pipeline", "INC4")
        with pytest.raises(RunError) as e:
            rn.start("pipeline", "INC5")
        assert e.value.status == 409
        rn.stop(r.id)
        await _wait(r)
        return r

    assert asyncio.run(scenario()).status == "stopped"


def test_bad_input_rejected(env: Path):
    from agent_flow_studio.builder import FlowError
    with pytest.raises(FlowError):
        Runner(env / "flows", fake_query(first_run)).start("pipeline", "../etc")


def test_api_run_flow_and_artifacts(env: Path, monkeypatch):
    q = fake_query(first_run)
    app = create_app(env / "flows", allowed_hosts=["testserver"])
    app.state.runner._query = q
    (env / "runs/INC1").mkdir(parents=True)
    (env / "runs/INC1/01_triage.json").write_text('{"a": 1}', encoding="utf-8")
    with TestClient(app) as c:
        body = {"flow": "pipeline", "input": "INC1"}
        assert c.post("/api/runs", json=body).status_code == 403
        r = c.post("/api/runs", json=body, headers=H)
        assert r.status_code == 201
        rid = r.json()["id"]
        events = [json.loads(x) for x in c.get(f"/api/runs/{rid}/events").text.splitlines()]
        assert events[-1]["type"] == "state" and events[-1]["status"] == "gate"
        assert c.post("/api/runs", json={**body, "input": "../x"}, headers=H).status_code == 400
        names = [a["name"] for a in c.get("/api/artifacts/INC1").json()]
        assert "01_triage.json" in names and "events.jsonl" in names
        assert c.get("/api/artifacts/INC1/01_triage.json").json()["text"] == '{"a": 1}'
        assert c.get("/api/artifacts/INC1/..%2F..%2Fsecret").status_code in (400, 404)
        assert c.get("/api/catalog").json()["mock"] in (True, False)

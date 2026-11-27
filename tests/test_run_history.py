"""What the Run panel reads about runs: tool results (size and time, never the content), the
model and tokens of each session, History (summaries cached by file, paging, runs that never
ended, attempts), the trash (list, restore, empty) and the plan of "run from step". Fake SDK;
no real model."""
import asyncio
import json
import shutil
import sys
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

from agent_flow_studio.editor.runner import RunError, Runner
from agent_flow_studio.editor.server import create_app
from agent_flow_studio.tools import io as io_tool

sys.path.insert(0, str(Path(__file__).parent))
from test_engine import AGENT, FIX, PAR, RUN, agent_query, run, wait

H = {"X-Flow-Editor": "1"}


@pytest.fixture
def flows(tmp_path: Path) -> Path:
    shutil.copytree(FIX / "parallel", tmp_path / "flows/parallel")
    return tmp_path / "flows"


async def _start(rn, rid):
    r = rn.start("parallel", inputs={"topic": "agents"}, run_id=rid)
    await wait(r)
    return r


def test_tool_results_tokens_and_model(flows):
    async def q(prompt, options):
        agent, rid = AGENT.search(prompt).group(1), RUN.search(prompt).group(1)
        yield AssistantMessage(content=[TextBlock("reading"),
                                        ToolUseBlock("t1", "Read", {"file_path": "README.md"})],
                               model="claude-test-model")
        yield UserMessage(content=[ToolResultBlock("t1", content="secret file text " * 10)])
        yield UserMessage(content=[ToolResultBlock("t2", content="boom", is_error=True)])
        for k, v in PAR.get(agent, {}).items():
            io_tool.store_output(io_tool.spec_for(rid), rid, agent, k, v)
        yield ResultMessage(subtype="success", duration_ms=1, duration_api_ms=1, is_error=False,
                            num_turns=2, session_id=f"s-{agent}", total_cost_usd=0.01,
                            usage={"input_tokens": 1200, "output_tokens": 340,
                                   "cache_read_input_tokens": 5000, "cache_creation_input_tokens": 0})

    rn = Runner(flows, q)
    r = run(_start(rn, "t1"))
    done = [e for e in r.events if e["type"] == "tool_result" and e["agent"] == "planner"]
    assert done and done[0]["id"] == "t1" and done[0]["tool"] == "Read"
    assert done[0]["size"] == len("secret file text " * 10) and done[0]["ms"] >= 0
    assert "secret" not in str(done[0])  # the size, never the content
    tool = next(e for e in r.events if e["type"] == "tool" and e["agent"] == "planner")
    assert tool["id"] == "t1"
    err = next(e for e in r.events if e["type"] == "tool_error" and e["agent"] == "planner")
    assert err["id"] == "t2" and err["error"] == "boom"
    res = next(e for e in r.events if e["type"] == "result" and e["agent"] == "planner")
    assert res["model"] == "claude-test-model"
    assert res["tokens"] == {"in": 1200, "out": 340, "cache_read": 5000}


def test_history_pages_and_summaries(flows):
    rn = Runner(flows, agent_query())

    async def go():
        for i in range(3):
            await _start(rn, f"h{i}")
            await asyncio.sleep(0.01)

    run(go())
    page, total = rn.past_page("parallel", limit=2)
    assert total == 3 and [p["input"] for p in page] == ["h2", "h1"]
    assert [p["input"] for p in rn.past_page("parallel", limit=2, offset=2)[0]] == ["h0"]
    assert page[0]["attempts"] == 1 and page[0]["status"] == "gate"
    # a second look reads the cache, a change of the file reads it again
    assert rn._summaries and rn.past_page("parallel")[1] == 3


def test_a_run_that_never_ended_is_lost(flows, tmp_path):
    rn = Runner(flows, agent_query())
    run(_start(rn, "l1"))
    f = tmp_path / "runs" / "l1" / "events.jsonl"
    lines = [x for x in f.read_text(encoding="utf-8").splitlines() if '"type": "end"' not in x]
    f.write_text("\n".join(lines) + "\n", encoding="utf-8")  # as if the studio died meanwhile
    again = Runner(flows, agent_query())
    got = again.past_runs("parallel")[0]
    assert got["status"] == "lost" and got["ended"] is None


def test_trash_restore_and_empty(flows, tmp_path):
    app = create_app(flows, allowed_hosts=["testserver"])
    app.state.runner._query = agent_query()
    c = TestClient(app)
    run(_start(app.state.runner, "x1"))
    name = c.delete("/api/runs/x1", headers=H).json()["trash"]
    got = c.get("/api/runs-trash").json()["runs"]
    assert [t["name"] for t in got] == [name] and got[0]["input"] == "x1"
    assert c.post(f"/api/runs-trash/{name}/restore").status_code == 403
    assert c.post(f"/api/runs-trash/{name}/restore", headers=H).json()["input"] == "x1"
    assert c.get("/api/flows/parallel/runs").json()["total"] == 1
    assert c.post("/api/runs-trash/nope/restore", headers=H).status_code == 404
    assert c.post("/api/runs-trash/a..b/restore", headers=H).status_code == 400
    c.delete("/api/runs/x1", headers=H)
    assert c.delete("/api/runs-trash", headers=H).json()["deleted"] == 1
    assert c.get("/api/runs-trash").json()["runs"] == []


def test_the_plan_of_run_from_step(flows):
    rn = Runner(flows, agent_query({"researcher_a": lambda rid, n: {}}))  # it never saves its notes
    run(_start(rn, "p1"))
    plan = rn.from_plan("p1", "writer")
    assert plan["states"]["researcher_a"] == "failed" and plan["states"]["planner"] == "done"
    assert "writer" in plan["redo"] and "planner" in plan["reuse"]
    assert "researcher_a" not in plan["reuse"]
    assert set(rn.from_plan("p1")) == {"status", "states"}
    with pytest.raises(RunError) as e:
        rn.from_plan("p1", "nope")
    assert e.value.status == 400
    app = create_app(flows, allowed_hosts=["testserver"])
    app.state.runner._query = agent_query()
    c = TestClient(app)
    got = c.get("/api/runs/p1/from-plan", params={"node": "writer"})
    assert got.status_code == 200 and "writer" in got.json()["redo"]
    assert c.get("/api/runs/nope9/from-plan").status_code == 404


def test_history_filters_and_totals(flows):
    rn = Runner(flows, agent_query({"researcher_a": lambda rid, n: {} if rid == "f2" else PAR["researcher_a"]}))

    async def go():
        for rid in ("f1", "f2", "f3"):
            await _start(rn, rid)
            await asyncio.sleep(0.01)

    run(go())
    rn.rename("f3", "Release notes")
    assert [r["input"] for r in rn.past_page("parallel", status="error")[0]] == ["f2"]
    assert [r["input"] for r in rn.past_page("parallel", q="release")[0]] == ["f3"]
    assert [r["input"] for r in rn.past_page("parallel", q="researcher_a")[0]] == ["f2"]  # failed step
    stats = rn.past_stats("parallel")
    assert stats["runs"] == 3 and stats["failed"] == 1 and stats["cost"] > 0


def test_waiting_runs_of_the_workspace(flows):
    rn = Runner(flows, agent_query())
    r = run(_start(rn, "w1"))
    assert r.status == "gate"
    got = rn.waiting()
    assert got[0]["since"] and len(got) == 1
    assert {k: v for k, v in got[0].items() if k != "since"} == {
        "id": r.id, "input": "w1", "flow": "parallel", "title": "", "kind": "gate",
        "label": "Check the report", "dismissed": False, "stale": False}
    rn.stop(r.id)
    assert rn.waiting() == []


def test_the_plan_does_not_keep_the_run(flows):
    rn = Runner(flows, agent_query())
    run(_start(rn, "k1"))
    again = Runner(flows, agent_query())
    again.from_plan("k1", "writer")
    assert again.runs == {}  # a look only


def test_cost_of_every_attempt_and_trash_per_flow(flows, tmp_path):
    rn = Runner(flows, agent_query({"researcher_a": lambda rid, n: PAR["researcher_a"] if n >= 3 else {}}))

    async def go():
        r = await _start(rn, "c1")
        rn.resume(r.id)
        await wait(r)

    run(go())
    got = rn.past_runs("parallel")[0]
    assert got["attempts"] == 2
    events = [e for e in (tmp_path / "runs/c1/events.jsonl").read_text(encoding="utf-8").splitlines() if '"result"' in e]
    assert got["cost"] == round(0.01 * len(events), 4)
    name = rn.delete_run("c1")
    assert rn.trash("other") == [] and rn.trash("parallel")[0]["name"] == name
    assert rn.trash("parallel")[0]["deleted"] >= rn.trash("parallel")[0]["started"]
    assert rn.empty_trash("parallel") == (1, [])


def test_a_tool_call_is_read_from_the_local_session(flows, tmp_path):
    """The run keeps no arguments or results; the panel reads them on demand from the agent's
    Claude Code session file on this machine (secret-like text masked)."""
    import json as _json
    import uuid as _uuid

    sid = str(_uuid.uuid4())
    secret = "ghp_" + "a" * 36

    async def q(prompt, options):
        agent, rid = AGENT.search(prompt).group(1), RUN.search(prompt).group(1)
        if agent == "planner":
            yield AssistantMessage(content=[ToolUseBlock("call-1", "Read", {"file_path": "notes.md"})],
                                   model="m", session_id=sid, usage={"input_tokens": 50, "output_tokens": 5})
        for k, v in PAR.get(agent, {}).items():
            io_tool.store_output(io_tool.spec_for(rid), rid, agent, k, v)
        yield ResultMessage(subtype="success", duration_ms=1, duration_api_ms=1, is_error=False,
                            num_turns=1, session_id=sid if agent == "planner" else f"s-{agent}", total_cost_usd=0.01)

    d = tmp_path / "claude" / "projects" / "some-project"
    d.mkdir(parents=True)
    lines = [
        {"type": "user", "uuid": "u1", "parentUuid": None, "sessionId": sid, "message": {"role": "user", "content": "go"}},
        {"type": "assistant", "uuid": "u2", "parentUuid": "u1", "sessionId": sid, "message": {
            "role": "assistant", "content": [{"type": "tool_use", "id": "call-1", "name": "Read", "input": {"file_path": "notes.md"}}]}},
        {"type": "user", "uuid": "u3", "parentUuid": "u2", "sessionId": sid, "message": {
            "role": "user", "content": [{"type": "tool_result", "tool_use_id": "call-1", "content": f"line 1\ntoken: {secret}"}]}},
    ]
    (d / f"{sid}.jsonl").write_text("\n".join(_json.dumps(x, separators=(",", ":")) for x in lines) + "\n", encoding="utf-8")
    rn = Runner(flows, q)
    r = run(_start(rn, "tc1"))
    tool = next(e for e in r.events if e["type"] == "tool")
    assert tool["session"] == sid and "file_path" not in str(tool)  # the event keeps no arguments
    assert any(e["type"] == "usage" and e["tokens"] == {"in": 50, "out": 5} for e in r.events)
    got = rn.tool_call(r.id, "call-1")
    assert '"notes.md"' in got["input"] and "line 1" in got["result"]
    assert secret not in got["result"] and "[secret removed]" in got["result"]
    with pytest.raises(RunError):
        rn.tool_call(r.id, "nope")


def test_a_saved_gate_still_waits_after_a_restart(flows):
    rn = Runner(flows, agent_query())
    run(_start(rn, "g1"))
    again = Runner(flows, agent_query())  # the studio restarted: nothing in memory
    got = again.waiting()
    assert [(w["input"], w["kind"], w["label"], w["id"]) for w in got] == [("g1", "gate", "Check the report", None)]


def _busy_query(fail_times: int):
    """researcher_a's first `fail_times` sessions end with a temporary API error (529)."""
    calls: dict[str, int] = {}

    async def q(prompt, options):
        agent, rid = AGENT.search(prompt).group(1), RUN.search(prompt).group(1)
        calls[agent] = calls.get(agent, 0) + 1
        yield AssistantMessage(content=[TextBlock("working")], model="claude-sonnet-5-5",
                               message_id=f"m-{agent}-{calls[agent]}",
                               usage={"input_tokens": 1000, "output_tokens": 100})
        yield AssistantMessage(content=[TextBlock("still")], model="claude-sonnet-5-5",
                               message_id=f"m-{agent}-{calls[agent]}",  # the same API message
                               usage={"input_tokens": 1000, "output_tokens": 100})
        if agent == "researcher_a" and calls[agent] <= fail_times:
            yield ResultMessage(subtype="error_during_execution", duration_ms=1, duration_api_ms=1,
                                is_error=True, num_turns=1, session_id=f"s-{agent}-{calls[agent]}",
                                total_cost_usd=0.001, result="API Error: overloaded (529)")
            return
        for k, v in PAR.get(agent, {}).items():
            io_tool.store_output(io_tool.spec_for(rid), rid, agent, k, v)
        yield ResultMessage(subtype="success", duration_ms=1, duration_api_ms=1, is_error=False,
                            num_turns=1, session_id=f"s-{agent}-{calls[agent]}", total_cost_usd=0.01)

    q.calls = calls
    return q


async def _until(r, kind):
    for _ in range(500):
        if any(e["type"] == kind for e in r.events):
            return
        await asyncio.sleep(0.01)
    raise AssertionError(f"no {kind} event")


def test_a_temporary_error_runs_the_step_again(flows):
    q = _busy_query(1)
    rn = Runner(flows, q)

    async def go():
        r = rn.start("parallel", inputs={"topic": "agents"}, run_id="rt1")
        await _until(r, "retry")
        rn.retry(r.id, "researcher_a", "now")  # no need to wait the 15 s
        await wait(r)
        return r

    r = run(go())
    assert r.status == "gate" and q.calls["researcher_a"] == 2
    failed = next(e for e in r.events if e["type"] == "agent_done" and e.get("error"))
    assert failed["transient"] is True
    retry = next(e for e in r.events if e["type"] == "retry")
    assert (retry["agent"], retry["attempt"], retry["max"], retry["wait"]) == ("researcher_a", 1, 2, 15)
    again = [e for e in r.events if e["type"] == "delegate" and e["agent"] == "researcher_a"]
    assert [e.get("retry") for e in again] == [None, 1]
    # live tokens: once per API message, with an estimate from the model's price
    usage = [e for e in r.events if e["type"] == "usage" and e["agent"] == "planner"]
    assert len(usage) == 1 and usage[0]["est"] == pytest.approx((1000 * 2 + 100 * 10) / 1e6)
    with pytest.raises(RunError):  # nothing waits any more
        rn.retry(r.id, None, "now")


def test_cancel_a_retry_ends_the_step(flows):
    rn = Runner(flows, _busy_query(5))

    async def go():
        r = rn.start("parallel", inputs={"topic": "agents"}, run_id="rt2")
        await _until(r, "retry")
        rn.retry(r.id, None, "cancel")
        await wait(r)
        return r

    r = run(go())
    assert r.status == "error"
    assert any(e["type"] == "retry_cancelled" for e in r.events)


def test_a_retry_off_and_a_non_temporary_error_fail_at_once(flows):
    from agent_flow_studio.engine import is_transient
    assert is_transient("API Error: 529 overloaded_error") and is_transient("rate_limit_error")
    assert not is_transient("Bash timed out after 120 s") and not is_transient("file not found")
    f = flows / "parallel" / "flow.yaml"
    f.write_text(f.read_text(encoding="utf-8").replace("max_parallel: 4", "max_parallel: 4\nretry: {transient: 0}"),
                 encoding="utf-8")
    r = run(_start(Runner(flows, _busy_query(1)), "rt3"))
    assert r.status == "error" and not any(e["type"] == "retry" for e in r.events)


def test_prices_only_for_known_models():
    from agent_flow_studio import pricing
    assert pricing.price("claude-opus-5-5") == (4.0, 20.0, 0.2, 5.0)
    assert pricing.price("claude-sonnet-4-6-20260101")[0] == 3.0
    assert pricing.price("claude-sonnet-5-5[1m]")[0] == 2.0
    assert pricing.price("claude-opus-5-9") is None and pricing.price(None) is None
    assert pricing.estimate("unknown", {"in": 10}) is None


def test_a_queued_run_waits_for_runs_and_for_gates(flows):
    """A queued run starts only when no run goes and none waits for the person: starting it at a
    gate would block that gate's decision (one run at a time)."""
    rn = Runner(flows, agent_query({"planner": lambda rid, n: asyncio.sleep(0.2, PAR["planner"])}))

    async def settle():
        for _ in range(300):
            await asyncio.sleep(0.02)
            if not rn.active():
                return

    async def go():
        first = rn.start("parallel", inputs={"topic": "agents"}, run_id="q1")
        item = rn.enqueue("parallel", inputs={"topic": "more"}, run_id="q2")
        bad = rn.enqueue("parallel", inputs={}, run_id="q3")  # no topic: fails when it starts
        assert [(i["place"], i["name"]) for i in rn.queue()["queued"]] == [(1, "q2"), (2, "q3")]
        await wait(first)
        assert first.status == "gate" and len(rn.queued) == 2  # q1 waits for the person: q2 too
        rn.decide(first.id, "approve", "")  # not refused: nothing else runs
        await wait(first)
        await settle()
        second = next(r for r in rn.runs.values() if r.input == "q2")
        assert second.status == "gate" and [i["name"] for i in rn.queued] == ["q3"]
        rn.stop(second.id)  # a waiting run stopped: the next one tries to start
        return item, bad

    item, bad = run(go())
    log = rn.queue()["log"]
    assert [x["id"] for x in log] == [item["id"], bad["id"]]
    assert log[0]["input"] == "q2" and "error" in log[1] and not rn.queued
    with pytest.raises(RunError):
        rn.enqueue("parallel", inputs={"topic": "x"})  # nothing runs or waits: start it now
    with pytest.raises(RunError):
        rn.unqueue("nope")


def _writer_query(denied: bool):
    """researcher_a asks to write, the write runs (or is denied), then the API is busy."""
    async def q(prompt, options):
        agent, rid = AGENT.search(prompt).group(1), RUN.search(prompt).group(1)
        if agent == "researcher_a" and not q.done:
            q.done = True
            yield AssistantMessage(content=[ToolUseBlock("w1", "Write", {"file_path": "x.md", "content": "x"})], model="m")
            yield UserMessage(content=[ToolResultBlock("w1", content="denied by policy" if denied else "ok", is_error=denied)])
            yield ResultMessage(subtype="error_during_execution", duration_ms=1, duration_api_ms=1, is_error=True,
                                num_turns=1, session_id="s-a", total_cost_usd=0.001, result="API Error: overloaded (529)")
            return
        for k, v in PAR.get(agent, {}).items():
            io_tool.store_output(io_tool.spec_for(rid), rid, agent, k, v)
        yield ResultMessage(subtype="success", duration_ms=1, duration_api_ms=1, is_error=False,
                            num_turns=1, session_id=f"s-{agent}", total_cost_usd=0.01)
    q.done = False
    return q


def test_a_step_that_wrote_is_not_retried_by_itself(flows):
    r = run(_start(Runner(flows, _writer_query(denied=False)), "w2"))
    assert r.status == "error"
    assert any(e["type"] == "retry_skipped" and e["why"] == "wrote" for e in r.events)
    assert not any(e["type"] == "retry" for e in r.events)


def test_a_denied_write_does_not_stop_the_retry(flows):
    rn = Runner(flows, _writer_query(denied=True))

    async def go():
        r = rn.start("parallel", inputs={"topic": "agents"}, run_id="w3")
        await _until(r, "retry")
        rn.retry(r.id, None, "now")
        await wait(r)
        return r

    r = run(go())
    assert r.status == "gate" and not any(e["type"] == "retry_skipped" for e in r.events)


def test_only_model_api_errors_are_temporary():
    from agent_flow_studio.engine import is_transient
    for text in ("API Error: 529 overloaded", "API Error: overloaded (529)", "API Error (503) service unavailable",
                 "rate_limit_error: slow down", "overloaded_error", "API Error: Connection error."):
        assert is_transient(text), text
    for text in ("HTTP 500 from connector jira", "error at line 504", "connector: connection error",
                 "Bash timed out after 120 s", "required outputs not saved: report"):
        assert not is_transient(text), text


def test_old_and_dismissed_gates(flows, monkeypatch):
    rn = Runner(flows, agent_query())
    r = run(_start(rn, "d1"))
    w = rn.waiting()[0]
    assert w["since"] and w["stale"] is False and w["dismissed"] is False
    rn.dismiss("d1", True)
    assert rn.waiting()[0]["dismissed"] is True
    rn.dismiss("d1", False)
    import agent_flow_studio.editor.runner as runner_mod
    monkeypatch.setattr(runner_mod, "STALE_GATE", -1)
    assert rn.waiting()[0]["stale"] is True
    again = Runner(flows, agent_query())  # saved gate: the same fields
    assert again.waiting()[0]["stale"] is True and again.waiting()[0]["since"]
    # deciding records how long the gate waited

    async def decide():
        rn.decide(r.id, "approve", "ok")
        await wait(r)

    run(decide())
    d = next(e for e in r.events if e["type"] == "decision")
    assert d["waited"] >= 0


def test_totals_of_a_period_and_the_one_before(flows):
    import time as _time
    rn = Runner(flows, agent_query())
    run(_start(rn, "p1"))
    now = _time.time()
    rn._all_past = lambda flow: [  # saved runs of several days
        {"status": "done", "started": now - 3600, "ended": now - 3500, "cost": 0.1},
        {"status": "error", "started": now - 2 * 86400, "ended": None, "cost": 0.2},
        {"status": "done", "started": now - 9 * 86400, "ended": now - 9 * 86400 + 50, "cost": 0.3},
        {"status": "done", "started": now - 40 * 86400, "ended": now - 40 * 86400 + 5, "cost": 0.4}]
    week = rn.past_stats("parallel", 7)
    assert (week["runs"], week["failed"], week["cost"], week["days"]) == (2, 1, 0.3, 7)
    assert week["prev"]["runs"] == 1 and week["prev"]["cost"] == 0.3
    assert rn.past_stats("parallel")["runs"] == 4 and "prev" not in rn.past_stats("parallel")


def test_the_queue_follows_what_waits_for_you(flows):
    """One rule: a gate the person dismissed (or one older than a day) no longer holds the queue;
    the queue says what holds it; "Start anyway" starts a queued run while others wait."""
    rn = Runner(flows, agent_query())

    async def go():
        first = await _start(rn, "h1")
        rn.enqueue("parallel", inputs={"topic": "more"}, run_id="h2")
        held = rn.queue()["queued"][0]["held_by"]
        assert [(h["input"], h["kind"]) for h in held] == [("h1", "gate")]
        rn.dismiss("h1", True)  # out of the count: the queued run starts
        for _ in range(300):
            await asyncio.sleep(0.02)
            if not rn.queued and not rn.active():
                break
        second = next(r for r in rn.runs.values() if r.input == "h2")
        assert second.status == "gate" and first.status == "gate"
        item = rn.enqueue("parallel", inputs={"topic": "third"}, run_id="h3")  # h2's gate holds it
        third = rn.start_queued_now(item["id"])
        await wait(third)
        assert third.status == "gate" and not rn.queued
        with pytest.raises(RunError):
            rn.start_queued_now("nope")

    run(go())


def test_the_waiting_items_have_the_shape_the_editor_reads(flows):
    import json as _json
    fixture = _json.loads((Path(__file__).parents[1] / "editor/tests/fixtures/waiting.json").read_text(encoding="utf-8"))
    rn = Runner(flows, agent_query())
    run(_start(rn, "c1"))
    got = rn.waiting()
    assert got and set(got[0]) == set(fixture["runs"][0])
    assert {w["kind"] for w in fixture["runs"]} <= {"gate", "paused", "approval"}


def test_time_waiting_for_the_person():
    from agent_flow_studio.editor.runner import _waited
    ev = [{"type": "gate", "kind": "human", "gate": "g", "ts": 10.0}, {"type": "decision", "gate": "g", "ts": 40.0},
          {"type": "gate", "kind": "human", "gate": "h", "ts": 50.0}]
    assert _waited(ev, None) == 30.0 and _waited(ev, 60.0) == 40.0


def test_a_decision_is_never_refused_while_another_run_goes(flows):
    """Dismiss a gate, a queued run starts; deciding the dismissed gate is then kept (the run is
    "queued") and goes on as soon as that run ends, before anything else in the queue."""
    rn = Runner(flows, agent_query({"planner": lambda rid, n: asyncio.sleep(0.3, PAR["planner"])}))

    async def go():
        first = await _start(rn, "x1")
        rn.enqueue("parallel", inputs={"topic": "more"}, run_id="x2")
        rn.enqueue("parallel", inputs={"topic": "later"}, run_id="x3")
        rn.dismiss("x1", True)  # x2 starts
        second = next(r for r in rn.runs.values() if r.input == "x2")
        assert second.status == "running"
        rn.decide(first.id, "approve", "ok")  # not refused
        assert first.status == "decided" and any(e["type"] == "turn" and e["after"] == "x2" for e in first.events)
        assert all(w["input"] != "x1" for w in rn.waiting())  # decided: it no longer waits
        await wait(second)
        for _ in range(300):
            await asyncio.sleep(0.02)
            if first.status not in ("decided", "running"):
                break
        assert first.status == "done"  # it went on before x3
        assert [i["name"] for i in rn.queued] == ["x3"]  # x2's gate holds x3

    run(go())


def test_a_decided_run_that_cannot_go_on_ends_and_the_queue_moves(flows, monkeypatch):
    rn = Runner(flows, agent_query({"planner": lambda rid, n: asyncio.sleep(0.3, PAR["planner"])}))

    async def go():
        first = await _start(rn, "y1")
        rn.enqueue("parallel", inputs={"topic": "more"}, run_id="y2")
        rn.dismiss("y1", True)  # y2 starts
        second = next(r for r in rn.runs.values() if r.input == "y2")
        rn.decide(first.id, "approve", "")
        assert first.status == "decided"

        def broken(*a, **k):  # not only a FlowError: any failure ends the run
            raise KeyError("the flow was changed and is not valid now")
        monkeypatch.setattr(rn, "_continue_gate", broken)
        rn.enqueue("parallel", inputs={"topic": "last"}, run_id="y3")
        rn.dismiss("y2", True)  # y2's gate will not hold y3
        await wait(second)
        for _ in range(300):
            await asyncio.sleep(0.02)
            if not rn.queued and not rn.active():
                break
        assert first.status == "error"
        end = [e for e in first.events if e["type"] == "end"][-1]
        assert "could not go on after the decision" in end["message"]
        assert not rn.queued and any(r.input == "y3" for r in rn.runs.values())

    run(go())



def test_a_decision_and_the_queue_survive_a_restart(flows):
    """Decided while another run goes, then the studio restarts: the run comes back decided
    (never at its gate again), the queue comes back from disk, and both go on by the clock. A
    decided run can still change its decision before it goes on (a superseding record)."""
    rn = Runner(flows, agent_query({"planner": lambda rid, n: asyncio.sleep(0.3, PAR["planner"])}))

    async def go():
        first = await _start(rn, "z1")
        rn.enqueue("parallel", inputs={"topic": "more"}, run_id="z2")
        rn.dismiss("z1", True)  # z2 starts
        rn.decide(first.id, "reject", "")
        rn.decide(first.id, "approve", "changed my mind")  # still possible while decided
        assert first.status == "decided" and first.after[1] == "approve"
        rn.enqueue("parallel", inputs={"topic": "last"}, run_id="z3")
        second = next(r for r in rn.runs.values() if r.input == "z2")
        rn.stop(second.id)
        await wait(second)
        rn.queued.clear()  # what the restart loses from memory (z1 went on, z3 stays saved)

    run(go())
    lines = [json.loads(x) for x in (flows.parent / "runs/z1/decisions.jsonl").read_text(encoding="utf-8").splitlines()]
    assert [x["decision"] for x in lines] == ["reject", "approve"] and lines[1]["supersedes"] is True


def test_the_queue_and_a_decided_run_come_back_after_a_restart(flows):
    rn = Runner(flows, agent_query({"planner": lambda rid, n: asyncio.sleep(0.3, PAR["planner"])}))

    async def go():
        first = await _start(rn, "r1")
        rn.enqueue("parallel", inputs={"topic": "more"}, run_id="r2")
        rn.dismiss("r1", True)  # r2 starts
        rn.decide(first.id, "approve", "")
        rn.enqueue("parallel", inputs={"topic": "last"}, run_id="r3")
        rn._start_queued = lambda: None  # the studio dies while r2 runs: r1 decided, r3 queued

    run(go())
    again = Runner(flows, agent_query())  # a restart: nothing in memory

    async def later():
        assert all(w["input"] != "r1" for w in again.waiting())  # decided: it does not wait again
        again.tick()  # the clock: r1 comes back decided and goes on first
        r1 = next(r for r in again.runs.values() if r.input == "r1")
        await wait(r1)
        assert r1.status == "done"
        for _ in range(200):
            again.tick()
            await asyncio.sleep(0.02)
            if not again.queued and not again.active():
                break
        r3 = next(r for r in again.runs.values() if r.input == "r3")
        assert any(e["type"] == "from_queue" for e in r3.events)
        assert again.past_page("parallel", q="r3")[0][0]["from_queue"]

    run(later())


def test_the_run_states_are_the_ones_the_editor_knows():
    import typing

    from agent_flow_studio.editor.runner import Status
    table = json.loads((Path(__file__).parents[1] / "editor/tests/fixtures/run_status.json").read_text(encoding="utf-8"))
    assert set(typing.get_args(Status)) == set(table["states"])
    for name, s in table["states"].items():
        assert set(s["next"]) <= set(table["states"]), name
    from agent_flow_studio.editor.runner import HOLD_FOR
    assert table["flags"]["held"]["minutes"] * 60 == HOLD_FOR



def test_a_decision_being_changed_does_not_go_on_with_the_old_one(flows):
    rn = Runner(flows, agent_query({"planner": lambda rid, n: asyncio.sleep(0.3, PAR["planner"])}))

    async def go():
        first = await _start(rn, "c1")
        rn.enqueue("parallel", inputs={"topic": "more"}, run_id="c2")
        rn.dismiss("c1", True)  # c2 starts
        rn.decide(first.id, "reject", "")
        rn.hold(first.id, True)  # the person opens "Change decision"
        second = next(r for r in rn.runs.values() if r.input == "c2")
        rn.stop(second.id)
        await wait(second)
        rn.tick()  # the clock: nothing runs, but the decision is being changed
        assert first.status == "decided" and first.after[1] == "reject"
        rn.decide(first.id, "approve", "on second thought")  # the change: held no more
        await wait(first)
        assert first.status == "done"
        with pytest.raises(RunError, match="already went on"):
            rn.hold(first.id, True)

    run(go())



def test_a_held_decision_holds_the_queue_and_survives_a_restart(flows):
    rn = Runner(flows, agent_query({"planner": lambda rid, n: asyncio.sleep(0.3, PAR["planner"])}))

    async def go():
        first = await _start(rn, "k1")
        rn.enqueue("parallel", inputs={"topic": "more"}, run_id="k2")
        rn.dismiss("k1", True)  # k2 starts
        rn.decide(first.id, "reject", "")
        rn.hold(first.id, True)  # the person changes it
        assert first.summary()["held_until"] > first.holding
        rn.enqueue("parallel", inputs={"topic": "later"}, run_id="k3")
        second = next(r for r in rn.runs.values() if r.input == "k2")
        rn.stop(second.id)
        await wait(second)
        rn.tick()
        assert [i["name"] for i in rn.queued] == ["k3"]  # the change holds the queue too
        assert rn.queue()["queued"][0]["held_by"][0]["kind"] == "changing"
        rn._start_queued = lambda: None  # the studio dies during the change

    run(go())
    again = Runner(flows, agent_query())

    async def later():
        again.tick()  # back from disk: decided and still held; nothing goes on
        k1 = next(r for r in again.runs.values() if r.input == "k1")
        assert k1.status == "decided" and k1.holding
        assert not again.active() and [i["name"] for i in again.queued] == ["k3"]

    run(later())


def test_a_saved_autonomy_reaches_the_runs_in_memory(flows):
    """The person changes the flow's autonomy while a run waits (or runs): the run takes it now,
    and a write approval that waits and that the new autonomy no longer asks for is approved."""
    from agent_flow_studio.policy import ApprovalRequest
    rn = Runner(flows, agent_query())

    async def go():
        r = await _start(rn, "au1")
        policy = r.engine.compiled.policy
        assert policy.flow_autonomy == "approve"
        fut = asyncio.get_running_loop().create_future()
        r.pending["w1"] = (ApprovalRequest("writer", "Edit", "dev", "edit a.md", {}), fut)
        assert rn.refresh_autonomy("parallel") == []  # nothing changed yet
        f = flows / "parallel" / "flow.yaml"
        f.write_text(f.read_text(encoding="utf-8").replace("max_parallel: 4", "max_parallel: 4\nautonomy: approve_prod"),
                     encoding="utf-8")
        assert rn.refresh_autonomy("parallel") == [r.id]
        assert policy.flow_autonomy == "approve_prod"
        assert fut.done() and fut.result()[0] is True  # a dev write no longer needs approval
        ev = [e for e in r.events if e["type"] == "autonomy"]
        assert ev and ev[-1]["flow"] == "approve_prod"
        assert any(e["type"] == "approval_result" and "autonomy" in e["comment"] for e in r.events)

    run(go())

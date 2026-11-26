"""Resuming a run that ended with an error or was stopped: the finished nodes keep their work,
the failed ones (and what depends on them) run again, edited nodes are flagged, and it also works
on a run opened again after a restart. Fake SDK; no real model."""
import asyncio
import shutil
import sys
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient

from agent_flow_studio.editor.runner import RunError, Runner
from agent_flow_studio.editor.server import create_app

sys.path.insert(0, str(Path(__file__).parent))
from test_engine import FIX, PAR, agent_query, ran, run, wait

H = {"X-Flow-Editor": "1"}


@pytest.fixture
def flows(tmp_path: Path) -> Path:
    shutil.copytree(FIX / "parallel", tmp_path / "flows/parallel")
    return tmp_path / "flows"


def _failing_a(until_call: int):
    """researcher_a saves nothing (so it fails after its follow-up) until call `until_call`."""
    return {"researcher_a": lambda rid, n: PAR["researcher_a"] if n >= until_call else {}}


async def _start(rn, rid="r1"):
    r = rn.start("parallel", inputs={"topic": "agents"}, run_id=rid)
    await wait(r)
    return r


def test_resume_keeps_the_finished_work_and_repeats_the_failed_node(flows):
    q = agent_query(_failing_a(3))  # calls 1 and 2 (the follow-up) fail, the 3rd works
    rn = Runner(flows, q)

    async def go():
        r = await _start(rn)
        assert r.status == "error"
        plan = rn.resume_plan(r.id)
        assert plan["redo"] == ["researcher_a", "writer"] and plan["changed"] == []
        assert sorted(plan["reuse"]) == ["planner", "researcher_b"]
        rn.resume(r.id)
        await wait(r)
        return r

    r = run(go())
    assert r.status == "gate" and r.gate["gate"] == "approve"
    assert ran(q).count("planner") == 1 and ran(q).count("researcher_b") == 1  # not repeated
    assert ran(q).count("researcher_a") == 3 and ran(q).count("writer") == 1
    resume = next(e for e in r.events if e["type"] == "resume")
    assert sorted(resume["reuse"]) == ["planner", "researcher_b"]
    assert r.engine.state["planner"] == "done" and r.done_agents == set(PAR)


def test_a_failed_attempt_does_not_count_for_max_runs(flows):
    q = agent_query(_failing_a(3))
    rn = Runner(flows, q)

    async def go():
        r = await _start(rn)
        rn.resume(r.id)
        await wait(r)
        return r

    r = run(go())
    assert r.engine.runs["researcher_a"] == 1  # the failed attempt was taken back


def test_an_edited_node_is_flagged_and_repeated_with_what_depends_on_it(flows):
    q = agent_query(_failing_a(3))
    rn = Runner(flows, q)

    async def go():
        r = await _start(rn)
        path = flows / "parallel" / "flow.yaml"
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
        raw["agents"]["planner"]["prompt"] = "Split the topic in a different way."
        path.write_text(yaml.safe_dump(raw, sort_keys=False), encoding="utf-8")
        plan = rn.resume_plan(r.id)
        keep = rn.resume_plan(r.id, reuse_changed=True)
        return r, plan, keep

    _r, plan, keep = run(go())
    assert plan["changed"] == ["planner"]
    assert sorted(plan["redo"]) == ["planner", "researcher_a", "researcher_b", "writer"]
    assert plan["reuse"] == []
    assert keep["changed"] == ["planner"] and keep["reuse"] == ["planner", "researcher_b"]
    assert keep["redo"] == ["researcher_a", "writer"]  # kept on request


def test_reusing_the_edited_node_runs_the_rest_only(flows):
    q = agent_query(_failing_a(3))
    rn = Runner(flows, q)

    async def go():
        r = await _start(rn)
        path = flows / "parallel" / "flow.yaml"
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
        raw["agents"]["planner"]["prompt"] = "Split the topic in a different way."
        path.write_text(yaml.safe_dump(raw, sort_keys=False), encoding="utf-8")
        rn.resume(r.id, reuse_changed=True)
        await wait(r)
        return r

    r = run(go())
    assert r.status == "gate" and ran(q).count("planner") == 1


def test_resume_after_a_restart_from_the_saved_run(flows):
    q = agent_query(_failing_a(3))
    first = Runner(flows, q)

    async def go():
        r = await _start(first, "r2")
        assert r.status == "error"
        again = Runner(flows, q)  # the studio was restarted: nothing in memory
        opened = again.open_past("r2")
        assert opened.status == "error"
        again.resume(opened.id)
        await wait(opened)
        return opened

    r = run(go())
    assert r.status == "gate" and ran(q).count("planner") == 1
    assert ran(q).count("researcher_b") == 1 and ran(q).count("writer") == 1


def test_a_stopped_run_resumes_too(flows):
    async def slow(rid, n):
        await asyncio.sleep(0.4)
        return PAR["researcher_a"]

    q = agent_query({"researcher_a": lambda rid, n: slow(rid, n)})
    rn = Runner(flows, q)

    async def go():
        r = rn.start("parallel", inputs={"topic": "agents"}, run_id="r3")
        while "researcher_a" not in {e.get("agent") for e in r.events if e["type"] == "delegate"}:
            await asyncio.sleep(0.01)
        rn.stop(r.id)
        await wait(r)
        assert r.status == "stopped"
        rn.resume(r.id)
        await wait(r)
        return r

    r = run(go())
    assert r.status == "gate" and r.done_agents == set(PAR)
    assert ran(q).count("planner") == 1


def test_a_stop_between_the_end_of_an_agent_and_its_links(flows, monkeypatch):
    """The stop comes after researcher_b saved its work (`agent_done`) but before its links were
    decided: the resume decides them, so the writer still runs (it used to wait for ever and the
    run ended `done` without it), and the interrupted researcher_a is listed as redone."""
    from agent_flow_studio.engine import GraphEngine

    real = GraphEngine._evaluate
    hold = {"on": True}

    async def evaluate(self, node):
        if node == "researcher_b" and hold["on"]:
            await asyncio.Event().wait()  # until the stop cancels it
        return await real(self, node)

    monkeypatch.setattr(GraphEngine, "_evaluate", evaluate)

    async def slow(rid, n):
        await asyncio.sleep(5)
        return PAR["researcher_a"]

    q = agent_query({"researcher_a": lambda rid, n: slow(rid, n) if n == 1
                     else PAR["researcher_a"]})
    rn = Runner(flows, q)

    async def go():
        r = rn.start("parallel", inputs={"topic": "agents"}, run_id="r6")
        while not any(e["type"] == "agent_done" and e.get("agent") == "researcher_b"
                      for e in r.events):
            await asyncio.sleep(0.01)
        await asyncio.sleep(0.05)
        rn.stop(r.id)
        await wait(r)
        assert r.status == "stopped"
        hold["on"] = False
        plan = rn.resume_plan(r.id)
        assert plan["redo"] == ["researcher_a", "writer"]
        assert sorted(plan["reuse"]) == ["planner", "researcher_b"]
        rn.resume(r.id)
        await wait(r)
        return r

    r = run(go())
    assert r.status == "gate" and r.done_agents == set(PAR)
    assert ran(q).count("researcher_b") == 1 and ran(q).count("writer") == 1


def test_only_errored_or_stopped_graph_runs_resume(flows):
    q = agent_query()
    rn = Runner(flows, q)

    async def go():
        r = await _start(rn, "r4")
        assert r.status == "gate"
        for call in (lambda: rn.resume(r.id), lambda: rn.resume_plan(r.id)):
            with pytest.raises(RunError) as e:
                call()
            assert e.value.status == 409

    run(go())


def test_resume_api(flows, monkeypatch):
    q = agent_query(_failing_a(3))
    app = create_app(flows, allowed_hosts=["testserver"])
    app.state.runner._query = q
    c = TestClient(app)

    async def go():
        rn = app.state.runner
        r = await _start(rn, "r5")
        return rn, r

    _rn, r = run(go())
    assert c.post(f"/api/runs/{r.id}/resume", json={}).status_code == 403  # no header
    plan = c.get(f"/api/runs/{r.id}/resume-plan")
    assert plan.status_code == 200 and plan.json()["redo"] == ["researcher_a", "writer"]
    assert c.get("/api/runs/nope/resume-plan").status_code == 404


# --- run from a step (even a finished run), with its inputs from the saved outputs or by hand ----
def test_run_from_a_step_keeps_the_earlier_work_and_repeats_the_rest(flows):
    q = agent_query()
    rn = Runner(flows, q)

    async def go():
        r = await _start(rn, "f1")
        assert r.status == "gate"  # waiting at "approve"
        rn.decide(r.id, "approve", "")
        await wait(r)
        assert r.status == "done"  # even a done run can start from a step
        rn.run_from("f1", "researcher_a")
        await wait(r)
        return r

    r = run(go())
    assert ran(q).count("planner") == 1 and ran(q).count("researcher_b") == 1
    assert ran(q).count("researcher_a") == 2 and ran(q).count("writer") == 2
    ev = next(e for e in r.events if e["type"] == "resume")
    assert ev["from"] == "researcher_a" and sorted(ev["reuse"]) == ["planner", "researcher_b"]
    assert "writer" in ev["redo"] and r.status == "gate" and r.gate["gate"] == "approve"


def test_run_from_with_inputs_defined_by_hand(flows):
    q = agent_query()
    rn = Runner(flows, q)

    async def go():
        r = await _start(rn, "f2")
        rn.run_from("f2", "writer", {"a": "MY OWN ANSWER"})
        await wait(r)
        return r

    r = run(go())
    writes = [c for c in q.log if c["agent"] == "writer"]
    assert len(writes) == 2 and "MY OWN ANSWER" in writes[1]["prompt"]
    assert "MY OWN ANSWER" not in writes[0]["prompt"]
    assert ran(q).count("researcher_a") == 1  # the earlier steps were not repeated
    assert r.status == "gate"


def test_run_from_without_overrides_uses_the_saved_outputs(flows):
    q = agent_query()
    rn = Runner(flows, q)

    async def go():
        await _start(rn, "f3")
        rn.run_from("f3", "writer", {})
        await wait(rn.runs[next(iter(rn.runs))])

    run(go())
    second = [c for c in q.log if c["agent"] == "writer"][1]["prompt"]
    assert "`a`: A" in second and "`b`: B" in second  # the saved outputs of the researchers


def test_run_from_errors(flows):
    q = agent_query()
    rn = Runner(flows, q)

    async def go():
        r = await _start(rn, "f4")
        for rid, node, status in (("nope1", "writer", 404), ("f4", "ghost", 400),
                                  ("bad id!", "writer", 400)):
            with pytest.raises(RunError) as e:
                rn.run_from(rid, node)
            assert e.value.status == status
        return r

    run(go())


def test_rename_a_run_shows_in_past_runs_and_not_in_artifacts(flows):
    from agent_flow_studio.editor.runner import list_artifacts
    q = agent_query()
    rn = Runner(flows, q)

    async def go():
        await _start(rn, "f5")

    run(go())
    assert rn.past_runs("parallel")[0]["title"] == ""
    assert rn.rename("f5", "  First   try  ") == "First try"
    assert rn.past_runs("parallel")[0]["title"] == "First try"
    assert "run_meta.json" not in [a["name"] for a in list_artifacts("f5")]
    assert rn.rename("f5", "") == "" and rn.past_runs("parallel")[0]["title"] == ""
    with pytest.raises(RunError):
        rn.rename("nope2", "x")


def test_rename_api(flows):
    app = create_app(flows, allowed_hosts=["testserver"])
    app.state.runner._query = agent_query()
    c = TestClient(app)
    run(_start(app.state.runner, "f6"))
    assert c.put("/api/runs/f6/title", json={"title": "x"}).status_code == 403
    r = c.put("/api/runs/f6/title", json={"title": "Plan A"}, headers=H)
    assert r.status_code == 200 and r.json()["title"] == "Plan A"
    assert c.get("/api/flows/parallel/runs").json()["runs"][0]["title"] == "Plan A"
    assert c.put("/api/runs/nope3/title", json={"title": "x"}, headers=H).status_code == 404


def test_past_runs_say_what_failed_and_when_it_ended(flows):
    q = agent_query(_failing_a(99))  # researcher_a never saves its notes
    rn = Runner(flows, q)
    run(_start(rn, "p1"))
    got = rn.past_runs("parallel")[0]
    assert got["status"] == "error" and got["failed"] == "researcher_a"
    assert got["ended"] >= got["started"] and "researcher_a" in (got["message"] or "")


def test_delete_a_run_moves_it_to_the_trash(flows, tmp_path):
    app = create_app(flows, allowed_hosts=["testserver"])
    app.state.runner._query = agent_query()
    c = TestClient(app)
    run(_start(app.state.runner, "d1"))
    assert c.delete("/api/runs/d1").status_code == 403  # no header
    r = c.delete("/api/runs/d1", headers=H)
    assert r.status_code == 200 and r.json()["trash"].startswith("d1-")
    assert c.get("/api/flows/parallel/runs").json()["runs"] == []
    assert (tmp_path / "runs" / ".trash" / r.json()["trash"] / "events.jsonl").is_file()
    assert c.delete("/api/runs/d1", headers=H).status_code == 404
    assert c.delete("/api/runs/bad%20id!", headers=H).status_code == 400

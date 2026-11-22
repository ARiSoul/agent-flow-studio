"""Hand-off between flows of a workspace: the `source` hint on flow inputs, the finished runs of
another flow with compatible outputs ("Fill from a run"), the value itself, and the provenance
saved with the run (`_handoff.json` and the start event). Run folders are kept per workspace.
No real model is called."""
import asyncio
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from agent_flow_studio import workspaces
from agent_flow_studio.builder.spec import FlowSpec
from agent_flow_studio.editor.runner import Runner
from agent_flow_studio.editor.server import create_app
from agent_flow_studio.tools import io as io_tool

sys.path.insert(0, str(Path(__file__).parent))  # the fake SDK helpers of the engine tests
from test_engine import agent_query, write_flow

H = {"X-Flow-Editor": "1"}
ANALYSIS = {"version": 1, "name": "analysis", "title": "Analysis", "execution": "graph",
            "task": "Plan the feature.", "inputs": {"feature": {"type": "text"}},
            "start": "planner",
            "agents": {"planner": {"description": "Plans.", "prompt": "Plan it.",
                                   "outputs": {"plan": {"type": "markdown"},
                                               "size": {"type": "integer"}}}}}
IMPL = {"version": 1, "name": "impl", "title": "Implementation", "execution": "graph",
        "task": "Build it.", "start": "coder",
        "inputs": {"plan": {"type": "markdown",
                            "source": {"flow": "analysis", "output": "planner.plan"}}},
        "agents": {"coder": {"description": "Codes.", "prompt": "Code it.",
                             "inputs": {"plan": {"from": "input.plan"}},
                             "outputs": {"done": {"type": "boolean"}}}}}


@pytest.fixture
def shop(tmp_path) -> workspaces.Workspace:
    ws = workspaces.create("shop", "local")
    write_flow(ws.flows_dir, ANALYSIS)
    write_flow(ws.flows_dir, IMPL)
    return ws


def _run(rn: Runner, ws, flow: str, **kw):
    async def go():
        with workspaces.using(ws):
            r = rn.start(flow, **kw)
        while r.task and not r.task.done():
            await asyncio.sleep(0.01)
        return r
    return asyncio.run(go())


def _runner(tmp_path):
    q = agent_query({"planner": lambda rid, n: {"plan": f"# Plan {n}\nDo it.", "size": 3},
                     "coder": lambda rid, n: {"done": True}})
    return Runner(tmp_path / "flows", q), q


def test_source_is_only_for_flow_inputs():
    FlowSpec.model_validate(IMPL)
    bad = {**ANALYSIS, "agents": {"planner": {**ANALYSIS["agents"]["planner"], "outputs": {
        "plan": {"type": "markdown", "source": {"flow": "x", "output": "a.b"}}}}}}
    with pytest.raises(ValueError, match="source is only for flow inputs"):
        FlowSpec.model_validate(bad)
    with pytest.raises(ValueError):
        FlowSpec.model_validate({**IMPL, "inputs": {"plan": {
            "type": "markdown", "source": {"flow": "analysis", "output": "no-dot"}}}})


def test_fill_from_a_run_and_provenance(shop, tmp_path):
    rn, _ = _runner(tmp_path)
    r = _run(rn, shop, "analysis", run_id="a1", inputs={"feature": "login"})
    assert r.status == "done", r.events[-3:]
    with workspaces.using(shop):
        runs = rn.flow_outputs("analysis", "markdown")
        assert [x["input"] for x in runs] == ["a1"]
        # integer can feed markdown (any value can feed text/markdown/json)
        assert {(o["agent"], o["name"]) for o in runs[0]["outputs"]} == {("planner", "plan"),
                                                                       ("planner", "size")}
        assert [o["name"] for o in rn.flow_outputs("analysis", "integer")[0]["outputs"]] == \
            ["size"]
        assert rn.flow_outputs("analysis", "boolean") == []
        value = rn.output_value("analysis", "a1", "planner", "plan")
    assert value.startswith("# Plan 1")
    ref = {"flow": "analysis", "run": "a1", "agent": "planner", "output": "plan"}
    r = _run(rn, shop, "impl", run_id="i1", inputs={"plan": value},
             handoff={"plan": ref, "nope": ref})
    assert r.status == "done", r.events[-3:]
    assert r.events[0]["handoff"] == {"plan": ref}  # only real inputs are kept
    with workspaces.using(shop):
        assert io_tool.handoff("i1") == {"plan": ref}
        assert io_tool.flow_inputs("i1")["plan"].startswith("# Plan 1")
    # the run folders of a workspace live apart from the studio's
    assert (tmp_path / "runs/@shop/i1/io/_handoff.json").is_file()
    assert not (tmp_path / "runs/i1").exists()
    assert rn.flow_outputs("analysis") == []  # from the studio: not its flow, not its runs


def test_handoff_api(shop, tmp_path):
    rn, _ = _runner(tmp_path)
    (tmp_path / "flows").mkdir(exist_ok=True)
    app = create_app(tmp_path / "flows", allowed_hosts=["testserver"])
    c = TestClient(app)
    c.cookies.set("afs_ws", "shop")
    # one finished run of the analysis, made with the same runner as the app's
    r = _run(rn, shop, "analysis", run_id="a1", inputs={"feature": "login"})
    assert r.status == "done"
    got = c.get("/api/handoff/analysis", params={"type": "markdown"}).json()
    assert got["runs"][0]["input"] == "a1"
    v = c.get("/api/handoff/analysis/a1/planner/plan").json()
    assert v["value"].startswith("# Plan") and v["from"]["run"] == "a1"
    assert c.get("/api/handoff/analysis/zz/planner/plan").status_code == 404
    assert c.get("/api/handoff/analysis", params={"type": "weird"}).status_code == 400


def test_board_shows_flows_sources_and_handoffs(shop, tmp_path):
    rn, _ = _runner(tmp_path)
    (tmp_path / "flows").mkdir(exist_ok=True)
    c = TestClient(create_app(tmp_path / "flows", allowed_hosts=["testserver"]))
    c.cookies.set("afs_ws", "shop")
    _run(rn, shop, "analysis", run_id="a1", inputs={"feature": "login"})
    ref = {"flow": "analysis", "run": "a1", "agent": "planner", "output": "plan"}
    _run(rn, shop, "impl", run_id="i1", inputs={"plan": "# Plan"}, handoff={"plan": ref})
    b = c.get("/api/board").json()
    assert b["workspace"]["name"] == "shop"
    flows = {f["name"]: f for f in b["flows"]}
    assert set(flows) == {"analysis", "impl"}
    assert flows["impl"]["inputs"]["plan"]["source"] == {"flow": "analysis",
                                                         "output": "planner.plan"}
    assert flows["impl"]["handoffs"] == [{"flow": "analysis", "output": "plan", "input": "plan"}]
    assert flows["impl"]["last_run"]["input"] == "i1"
    assert {"agent": "planner", "name": "plan", "type": "markdown"} in flows["analysis"]["outputs"]
    assert c.get("/api/workspaces/shop/flows").json()[0]["name"] == "analysis"

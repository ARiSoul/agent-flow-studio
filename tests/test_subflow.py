"""Subflow nodes: a whole flow runs as one step of another (graph mode). The child runs in its own
run folder with its events tagged `sub`, its mapped outputs (or its stop message) become the
node's outputs, its cost counts in the parent, and the rules hold (no human gates inside, no
cycles, at most 3 levels, known inputs and outputs). No real model is called."""
import asyncio
import copy
import sys
from pathlib import Path

import pytest

from agent_flow_studio.builder import FlowError
from agent_flow_studio.builder.compile import load_graph
from agent_flow_studio.builder.spec import FlowSpec
from agent_flow_studio.editor.runner import Runner
from agent_flow_studio.tools import io as io_tool

sys.path.insert(0, str(Path(__file__).parent))  # the fake SDK helpers of the engine tests
from test_engine import agent_query, ran, types, write_flow

CHILD = {"version": 1, "name": "analysis", "title": "Analysis", "execution": "graph",
         "task": "Plan the feature.", "start": "planner",
         "inputs": {"feature": {"type": "text"}, "depth": {"type": "integer", "required": False}},
         "agents": {"planner": {"description": "Plans.", "prompt": "Plan it.",
                                "inputs": {"feature": {"from": "input.feature"}},
                                "outputs": {"plan": {"type": "markdown"},
                                            "size": {"type": "integer"}}}},
         "gates": {"ready": {"kind": "stop", "label": "Ready",
                             "message": "Plan ready: {planner.size} steps."}},
         "edges": [{"from": "planner", "to": "ready"}]}
PARENT = {"version": 1, "name": "feature", "title": "Feature", "execution": "graph",
          "task": "Build the feature.", "start": "analyse",
          "inputs": {"feature": {"type": "text"}},
          "agents": {
              "analyse": {"description": "Analysis and plan.",
                          "subflow": {"flow": "analysis",
                                      "inputs": {"feature": "{input.feature}", "depth": "{2 + 1}"},
                                      "outputs": {"plan": {"from": "planner.plan"},
                                                  "summary": {"from": "message"}}},
                          "outputs": {"plan": {"type": "markdown"}, "summary": {"type": "text"}}},
              "coder": {"description": "Codes.", "prompt": "Code it.",
                        "inputs": {"plan": {"from": "analyse.plan"}},
                        "outputs": {"done": {"type": "boolean"}}}},
          "edges": [{"from": "analyse", "to": "coder"}]}


@pytest.fixture
def flows(tmp_path) -> Path:
    base = tmp_path / "flows"
    write_flow(base, copy.deepcopy(CHILD))
    write_flow(base, copy.deepcopy(PARENT))
    return base


def test_a_subflow_runs_as_one_step(flows, tmp_path):
    seen = {}

    def planner(rid, n):
        values, _ = io_tool.resolve_inputs(io_tool.spec_for(rid), rid, "planner")
        seen["planner"] = (rid, values, io_tool.flow_inputs(rid))
        return {"plan": "# Plan\nStep 1.", "size": 4}

    def coder(rid, n):
        seen["coder"] = io_tool.resolve_inputs(io_tool.spec_for(rid), rid, "coder")[0]
        return {"done": True}

    q = agent_query({"planner": planner, "coder": coder})
    rn = Runner(flows, q)

    async def go():
        r = rn.start("feature", run_id="f1", inputs={"feature": "login"})
        while r.task and not r.task.done():
            await asyncio.sleep(0.01)
        return r
    r = asyncio.run(go())
    assert r.status == "done", r.events[-4:]
    assert ran(q) == ["planner", "coder"]
    # the child ran in its own folder with the mapped inputs (typed: "{2 + 1}" is the integer 3)
    rid, values, inputs = seen["planner"]
    assert rid == "f1--analyse" and values == {"feature": "login"}
    assert inputs == {"feature": "login", "depth": 3}
    assert (tmp_path / "runs/f1--analyse/io/planner.json").is_file()
    # the node's outputs: a child output and the child's stop message
    assert io_tool.outputs("f1", "analyse") == {"plan": "# Plan\nStep 1.",
                                                "summary": "Plan ready: 4 steps."}
    assert seen["coder"] == {"plan": "# Plan\nStep 1."}
    # events: the child's are tagged with the node; the node itself is one step
    sub = [e for e in r.events if e.get("sub") == "analyse"]
    assert {e["type"] for e in sub} >= {"delegate", "agent_done", "gate"}
    assert types(r, "subflow")[0] == {**types(r, "subflow")[0], "agent": "analyse",
                                      "flow": "analysis", "run": "f1--analyse"}
    done = [e["agent"] for e in types(r, "agent_done") if not e.get("sub")]
    assert done == ["analyse", "coder"]
    assert r.cost == pytest.approx(0.02)  # the child's session counts in the parent
    assert "f1--analyse" not in io_tool._by_run  # released


def _codes(e: FlowError) -> set[str]:
    return {getattr(m, "code", None) for m in e.msgs}


@pytest.mark.parametrize("patch, code", [
    (lambda c, p: c["gates"].__setitem__("ok", {"kind": "human", "label": "OK?"}) or
     c["edges"].append({"from": "planner", "to": "ok"}), "compile.subflow_human"),
    (lambda c, p: c.__setitem__("execution", "orchestrator"), "compile.subflow_graph"),
    (lambda c, p: p["agents"]["analyse"]["subflow"].__setitem__("flow", "nope"),
     "compile.subflow_missing"),
    (lambda c, p: p["agents"]["analyse"]["subflow"]["inputs"].__setitem__("x", "1"),
     "compile.subflow_inputs"),
    (lambda c, p: p["agents"]["analyse"]["subflow"]["inputs"].pop("feature"),
     "compile.subflow_inputs"),
    (lambda c, p: p["agents"]["analyse"]["subflow"]["outputs"].__setitem__(
        "plan", {"from": "planner.nope"}), "compile.subflow_output"),
])
def test_compile_rules(tmp_path, patch, code):
    c, p = copy.deepcopy(CHILD), copy.deepcopy(PARENT)
    patch(c, p)
    base = tmp_path / "flows"
    write_flow(base, c)
    write_flow(base, p)
    with pytest.raises(FlowError) as e:
        load_graph("feature", base=base)
    assert code in _codes(e.value), e.value.msgs


def test_cycles_and_depth(tmp_path):
    base = tmp_path / "flows"
    c = copy.deepcopy(CHILD)
    c["agents"]["back"] = {"description": "Back.", "subflow": {
        "flow": "feature", "inputs": {"feature": "x"}}}
    c["edges"].append({"from": "planner", "to": "back"})
    write_flow(base, c)
    write_flow(base, copy.deepcopy(PARENT))
    with pytest.raises(FlowError) as e:
        load_graph("feature", base=base)
    assert "compile.subflow_cycle" in _codes(e.value)

    # four levels: a → b → c → d
    base = tmp_path / "deep"
    leaf = {"version": 1, "name": "d", "title": "D", "execution": "graph", "task": "Do it.",
            "start": "w", "inputs": {"x": {"type": "text", "required": False}},
            "agents": {"w": {"description": "W.", "prompt": "Work."}}}
    write_flow(base, leaf)
    for name, child in (("c", "d"), ("b", "c"), ("a", "b")):
        write_flow(base, {**leaf, "name": name, "agents": {"w": {
            "description": "W.", "subflow": {"flow": child}}}})
    load_graph("b", base=base)  # three levels: fine
    with pytest.raises(FlowError) as e:
        load_graph("a", base=base)
    assert "compile.subflow_depth" in _codes(e.value)


@pytest.mark.parametrize("patch, code", [
    (lambda p: p.__setitem__("execution", "orchestrator"), "spec.subflow_needs_graph"),
    (lambda p: p["agents"]["analyse"]["subflow"]["outputs"].pop("summary"),
     "spec.subflow_output_unmapped"),
    (lambda p: p["agents"]["analyse"]["subflow"]["outputs"].__setitem__(
        "other", {"from": "planner.plan"}), "spec.subflow_output_unknown"),
    (lambda p: p["agents"]["analyse"]["subflow"].__setitem__("flow", "feature"),
     "spec.subflow_self"),
    (lambda p: p["agents"]["analyse"]["subflow"]["inputs"].__setitem__("feature", "{nobody.x}"),
     "spec.subflow_input_reason"),
    (lambda p: p["agents"]["analyse"].__setitem__("prompt", "Hi."), "spec.subflow_no_prompt"),
])
def test_spec_rules(patch, code):
    from agent_flow_studio.editor.validation import _check

    p = copy.deepcopy(PARENT)
    patch(p)
    codes = {e.get("code") for e in _check(p, {})["errors"]}
    assert code in codes, codes
    if code != "spec.subflow_no_prompt":
        with pytest.raises(ValueError):
            FlowSpec.model_validate(p)

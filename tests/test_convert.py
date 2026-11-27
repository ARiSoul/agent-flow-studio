"""From the orchestrator to the graph engine (builder/convert.py): the notes before converting,
the conversion of the draft, the warning on long orchestrator flows and the API."""
from pathlib import Path

import yaml
from fastapi.testclient import TestClient

from agent_flow_studio.builder import convert
from agent_flow_studio.builder.spec import FlowSpec
from agent_flow_studio.editor.server import create_app
from agent_flow_studio.editor.validation import _validate

H = {"X-Flow-Editor": "1"}


def orch_flow(n: int = 3) -> dict:
    """An orchestrator flow a1 → a2 → … → review (human gate) whose agents have no typed IO."""
    agents = {f"a{i}": {"description": f"Step {i}.", "prompt": f"Do step {i}."} for i in range(1, n + 1)}
    edges = [{"from": f"a{i}", "to": f"a{i + 1}", "when": "finished"} for i in range(1, n)]
    edges.append({"from": f"a{n}", "to": "review", "when": "if the plan looks risky"})
    return {"version": 1, "name": "legacy", "title": "Legacy", "task": "Work on {input}.",
            "start": "a1", "agents": agents, "gates": {"review": {"kind": "human", "label": "Check"}},
            "edges": edges, "orchestrator": {"prompt": "Delegate carefully."}}


def test_the_notes_say_what_works_differently_in_the_graph_engine():
    notes = convert.graph_notes(FlowSpec.model_validate(orch_flow()))
    codes = [(level, m.code, nodes) for level, m, nodes in notes]
    # a2 and a3 relied on the orchestrator's words: they get only the task now
    assert ("warn", "convert.no_inputs", ["a2"]) in codes and ("warn", "convert.no_inputs", ["a3"]) in codes
    assert not any(c == "convert.no_inputs" and n == ["a1"] for _, c, n in codes)  # the start
    # a1 and a2 save nothing for the agents after them
    assert ("warn", "convert.no_outputs", ["a1"]) in codes and ("warn", "convert.no_outputs", ["a2"]) in codes
    assert not any(c == "convert.no_outputs" and n == ["a3"] for _, c, n in codes)  # only a gate after
    assert ("info", "convert.when_cleared", ["a1", "a2"]) in codes
    assert ("info", "convert.when", ["a3", "review"]) in codes
    assert ("info", "convert.orch_prompt", []) in codes


def test_typed_inputs_and_outputs_leave_no_warnings():
    d = orch_flow(2)
    d["agents"]["a1"]["outputs"] = {"plan": {"type": "text"}}
    d["agents"]["a2"]["inputs"] = {"plan": {"from": "a1.plan"}}
    warns = [m.code for level, m, _ in convert.graph_notes(FlowSpec.model_validate(d)) if level == "warn"]
    assert warns == []


def test_to_graph_only_sets_the_mode_and_clears_go_on_conditions():
    d = orch_flow()
    g = convert.to_graph(d)
    assert g["execution"] == "graph" and d.get("execution") is None  # the input is not changed
    assert [e.get("when") for e in g["edges"]] == [None, None, "if the plan looks risky"]
    assert g["agents"] == d["agents"] and g["orchestrator"] == d["orchestrator"]
    assert convert.trivial_when(" Finished. ") and not convert.trivial_when("when tests fail")


def test_a_long_orchestrator_flow_gets_a_warning_and_a_graph_flow_does_not():
    short = _validate(orch_flow(3), {})
    assert not any(w.get("code") == "validate.orch_large" for w in short["warnings"])
    long = _validate(orch_flow(4), {})
    w = next(w for w in long["warnings"] if w.get("code") == "validate.orch_large")
    assert w["params"] == {"max": "3", "n": "4"} and w["loc"] == "execution"
    graph = _validate(convert.to_graph(orch_flow(4)), {})
    assert not any(w.get("code") == "validate.orch_large" for w in graph["warnings"])


def test_the_api_plans_the_conversion_without_saving(tmp_path: Path):
    d = tmp_path / "legacy"
    d.mkdir()
    (d / "flow.yaml").write_text(yaml.safe_dump(orch_flow()), encoding="utf-8")
    before = (d / "flow.yaml").read_text(encoding="utf-8")
    c = TestClient(create_app(tmp_path, allowed_hosts=["testserver"]))
    r = c.post("/api/flows/legacy/to-graph", headers=H, json={"spec": orch_flow(), "prompts": {}})
    assert r.status_code == 200
    plan = r.json()
    assert plan["spec"]["execution"] == "graph" and plan["errors"] == []
    assert {n["code"] for n in plan["notes"]} >= {"convert.no_inputs", "convert.when_cleared"}
    assert all(n["level"] in ("warn", "info") and n["msg"] for n in plan["notes"])
    assert (d / "flow.yaml").read_text(encoding="utf-8") == before  # nothing saved
    # a new blank flow is a graph flow with no condition to ask a model about
    assert c.post("/api/flows", headers=H, json={"name": "fresh", "title": "Fresh"}).status_code == 201
    fresh = yaml.safe_load((tmp_path / "fresh" / "flow.yaml").read_text(encoding="utf-8"))
    assert fresh["execution"] == "graph" and all("when" not in e for e in fresh["edges"])


def test_an_agents_own_loop_is_not_an_agent_after_it():
    d = orch_flow(2)
    d["gates"]["review"] = {"kind": "human", "label": "Check"}
    d["edges"] = [{"from": "a1", "to": "review"}, {"from": "review", "to": "a1", "when": "redo"},
                  {"from": "review", "to": "a2"}]
    after = {(m.code, tuple(nodes)): m.params for _, m, nodes in convert.graph_notes(FlowSpec.model_validate(d))}
    assert after[("convert.no_outputs", ("a1",))]["next"] == "a2"  # not "a1, a2"

"""Decision nodes (first branch whose `if` holds, or "otherwise"), stop messages with {expression}
parts, and `previous.<agent>.<output>` (the outputs of the last finished run). No real model."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))  # the fake SDK helpers of the engine tests
from test_engine import agent_query, run, types, wait, write_flow

from agent_flow_studio.builder import expr, load_spec
from agent_flow_studio.builder.compile import flow_section, next_steps_result
from agent_flow_studio.editor.runner import Runner
from agent_flow_studio.tools import io as io_tool

FIRST = "not exists(previous.counter.count)"
MORE = "counter.count > previous.counter.count"
FEWER = "counter.count < previous.counter.count"


def counter_flow(execution: str = "graph") -> dict:
    return {
        "version": 1, "name": "count-check", "title": "Count check", "execution": execution,
        "start": "counter",
        "agents": {"counter": {"description": "Counts the users.", "prompt": "Count them.",
                               "inputs": {"last": {"from": "previous.counter.count"}},
                               "outputs": {"count": {"type": "integer"}}}},
        "gates": {
            "compare": {"kind": "decision", "label": "More, fewer or the same?"},
            "first": {"kind": "stop", "label": "First check",
                      "message": "First check: {counter.count} users."},
            "more": {"kind": "stop", "label": "More",
                     "message": "You have {counter.count - previous.counter.count} new users."},
            "fewer": {"kind": "stop", "label": "Fewer",
                      "message": "You have {previous.counter.count - counter.count} users less."},
            "same": {"kind": "stop", "label": "Same", "message": "No new users ({counter.count})."},
        },
        "edges": [
            {"from": "counter", "to": "compare"},
            {"from": "compare", "to": "first", "if": FIRST, "label": "first"},
            {"from": "compare", "to": "more", "if": MORE, "label": "yes"},
            {"from": "compare", "to": "fewer", "if": FEWER, "label": "fewer"},
            {"from": "compare", "to": "same", "label": "no"},
        ],
    }


def test_three_runs_compare_with_the_previous_one(tmp_path):
    counts = iter([5, 7, 7, 4])
    seen_last = []

    def counter(rid, n):
        values, _ = io_tool.resolve_inputs(io_tool._spec, rid, "counter")
        seen_last.append(values.get("last"))
        return {"count": next(counts)}

    flows = tmp_path / "flows"
    write_flow(flows, counter_flow())
    rn = Runner(flows, agent_query({"counter": counter}))
    results = []
    for rid in ("c1", "c2", "c3", "c4"):
        async def go(rid=rid):
            r = rn.start("count-check", run_id=rid)
            await wait(r)
            return r
        r = run(go())
        assert r.status == "done", r.events[-1]
        results.append(r)
    msgs = [r.gate["message"] for r in results]
    assert msgs == ["First check: 5 users.", "You have 2 new users.", "No new users (7).",
                    "You have 3 users less."]
    assert seen_last == [None, 5, 7, 7]  # the agent's input from the previous run
    decided = types(results[1], "decided")[0]
    assert decided == {**decided, "node": "compare", "to": "more"}
    edges = {e["to"]: (e["fired"], e["by"]) for e in types(results[1], "edge")
             if e["from"] == "compare"}
    assert edges == {"first": (False, "if"), "more": (True, "if"), "fewer": (False, "if"),
                     "same": (False, "else")}
    assert types(results[2], "decided")[0]["to"] == "same"
    assert results[3].events[-1]["gate"]["message"] == "You have 3 users less."


def test_an_agent_test_run_does_not_become_the_previous_run(tmp_path):
    flows = tmp_path / "flows"
    write_flow(flows, counter_flow())
    rn = Runner(flows, agent_query({"counter": lambda rid, n: {"count": 9}}))

    async def go():
        r = rn.start("count-check", mode="agent", target="counter", run_id="t1")
        await wait(r)
        return r
    run(go())
    assert io_tool.previous_outputs("count-check") == {}


def test_orchestrator_mode_uses_next_steps_for_decisions(tmp_path):
    flows = tmp_path / "flows"
    write_flow(flows, counter_flow("orchestrator"))
    spec = load_spec("count-check", base=flows)[0]
    text = flow_section(spec)
    assert "Decisions (no agent runs there)" in text and "otherwise: STOP and report: **Same**" in text
    assert "go to the decision **More, fewer or the same?** (`compare`)" in text
    io_tool.store_output(spec, "o1", "counter", "count", 3)
    r = next_steps_result(spec, "o1", "compare")
    data = r["content"][0]["text"]
    assert '"go_to": "first"' in data and '"chosen": true' in data


@pytest.mark.parametrize("patch, code", [
    (lambda f: f["edges"].append({"from": "compare", "to": "first"}), "spec.decision_else"),
    (lambda f: f["edges"].__setitem__(1, {**f["edges"][1], "when": "it is the first"}),
     "spec.decision_when"),
    (lambda f: f["gates"]["compare"].__setitem__("message", "x"), "spec.message_only_stop"),
    (lambda f: f["gates"]["more"].__setitem__("message", "a { b"), "spec.message_reason"),
    (lambda f: f["gates"]["more"].__setitem__("message", "{counter.nope}"), "spec.message_reason"),
    (lambda f: f["edges"].__setitem__(2, {**f["edges"][2], "if": "previous.counter > 1"}),
     "spec.edge_reason"),
    (lambda f: f["agents"]["counter"]["inputs"].__setitem__("x", {"from": "previous.nobody.y"}),
     "spec.from_no_previous"),
    (lambda f: f["gates"].__setitem__("previous", {"kind": "stop", "label": "P"}),
     "spec.reserved_name"),
    (lambda f: f["gates"].__setitem__("lonely", {"kind": "decision", "label": "L"}),
     "spec.decision_no_branch"),
])
def test_rules(patch, code):
    from agent_flow_studio.editor.validation import _check

    f = counter_flow()
    patch(f)
    codes = {e.get("code") for e in _check(f, {})["errors"]}
    assert code in codes, codes


def test_message_templates():
    scope = {"a": {"n": 7, "x": 2.0, "tags": ["p", "q"]}, "previous": {}}
    assert expr.render("{a.n - 2} · {{literal}} · {a.x} · {a.tags} · {previous.a.n}", scope) == \
        "5 · {literal} · 2 · p, q · —"
    assert expr.render("{a.(}", scope) == "{a.(}"  # a part that fails stays as written
    assert expr.template_parts("{a.n} and {{x}}") == ["a.n"]
    with pytest.raises(expr.ExprError):
        expr.template_parts("a } b")


def test_asking_an_agent_for_more_after_the_run(tmp_path):
    """After the run, the person asks the agent for more: its own session is resumed (same
    tools and rules), it saves a new output, and the run goes back to where it was."""
    from agent_flow_studio.editor.runner import RunError

    flows = tmp_path / "flows"
    write_flow(flows, counter_flow())
    q = agent_query({"counter": lambda rid, n: {"count": 5 if n == 1 else 9}})
    rn = Runner(flows, q)

    async def go():
        r = rn.start("count-check", run_id="a1")
        await wait(r)
        return r
    r = run(go())
    assert r.status == "done" and rn.askable(r.id) == ["counter"]

    async def ask():
        rn.ask(r.id, "Count again, please.")
        await wait(r)
    run(ask())
    assert r.status == "done" and r.gate["gate"] == "first"  # back where it was
    second = q.log[-1]
    assert second["resume"] == "s-counter-1" and "Count again, please." in second["prompt"]
    assert io_tool.outputs("a1", "counter")["count"] == 9
    ask_ev = types(r, "ask")[-1]
    assert ask_ev["agent"] == "counter" and ask_ev["text"] == "Count again, please."
    assert types(r, "outputs")[-1]["agent"] == "counter"
    with pytest.raises(RunError) as e:
        rn.ask(r.id, "  ")
    assert e.value.status == 422
    with pytest.raises(RunError) as e:
        rn.ask(r.id, "x", agent="nobody")
    assert e.value.status == 422


def test_a_past_run_can_be_opened_again_and_asked_for_more(tmp_path):
    """After the editor restarts (a new Runner), a saved run opens again from its log: the list,
    its state and outputs, and "ask for more" resumes the agent's saved session."""
    flows = tmp_path / "flows"
    write_flow(flows, counter_flow())
    q = agent_query({"counter": lambda rid, n: {"count": 5 if n == 1 else 7}})
    rn = Runner(flows, q)

    async def go():
        r = rn.start("count-check", run_id="p1")
        await wait(r)
        return r
    first = run(go())
    assert first.status == "done"
    rn2 = Runner(flows, q)  # "restart": nothing in memory
    past = rn2.past_runs("count-check")
    assert [p["input"] for p in past] == ["p1"] and past[0]["status"] == "done"
    assert past[0]["open"] is False and rn2.past_runs("other-flow") == []
    r = rn2.open_past("p1")
    assert r.id == first.id and r.status == "done" and r.gate["message"] == "First check: 5 users."
    assert [e["type"] for e in r.events][:2] == ["start", "delegate"] and r.events[-1]["type"] == "end"
    assert rn2.askable(r.id) == ["counter"] and rn2.open_past("p1") is r

    async def ask():
        rn2.ask(r.id, "Count again.")
        await wait(r)
    run(ask())
    assert q.log[-1]["resume"] == "s-counter-1" and io_tool.outputs("p1", "counter")["count"] == 7
    assert r.status == "done"
    with pytest.raises(Exception):  # noqa: B017 — a folder without a saved run
        rn2.open_past("nothing-here")


def test_a_past_run_at_a_gate_can_still_be_decided(tmp_path):
    import shutil

    from test_engine import FIX

    flows = tmp_path / "flows"
    shutil.copytree(FIX / "parallel", flows / "parallel")
    q = agent_query()
    rn = Runner(flows, q)

    async def go():
        r = rn.start("parallel", inputs={"topic": "agents"}, run_id="g9")
        await wait(r)
        return r
    r1 = run(go())
    assert r1.status == "gate" and r1.gate["gate"] == "approve"
    rn2 = Runner(flows, q)
    r = rn2.open_past("g9")
    assert r.status == "gate" and r.engine is not None
    assert r.engine.state["writer"] == "done" and r.engine.pending_gates[0]["gate"] == "approve"
    assert set(rn2.askable(r.id)) == {"planner", "researcher_a", "researcher_b", "writer"}

    async def decide():
        rn2.decide(r.id, "approve", "fine")
        await wait(r)
    run(decide())
    assert r.status == "done" and len(q.log) == 4  # no agent ran again

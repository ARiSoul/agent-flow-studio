"""A human gate decided with "answer" when no link out of it handles that decision asks again
(instead of ending the run with everything after it skipped), and an agent can read the gate's
decision and comment (`from: <gate>.comment`), e.g. to revise a plan. Fake SDK; no real model."""
import sys
from pathlib import Path

import pytest

from agent_flow_studio.builder.spec import FlowSpec
from agent_flow_studio.editor.runner import Runner

sys.path.insert(0, str(Path(__file__).parent))
from test_engine import agent, agent_query, ran, run, wait, write_flow


def _flow(rework: bool) -> dict:
    edges = [{"from": "planner", "to": "check"},
             {"from": "check", "to": "doer", "if": 'check.decision == "approve"'}]
    if rework:  # rejected or answered: back to the planner
        edges.append({"from": "check", "to": "planner", "if": 'check.decision != "approve"'})
    return {"version": 1, "name": "plan", "title": "Plan", "execution": "graph", "start": "planner",
            "inputs": {"topic": {"type": "text"}},
            "agents": {
                "planner": agent(inputs={"topic": {"from": "input.topic"},
                                         "feedback": {"from": "check.comment", "required": False}},
                                 outputs={"plan": {"type": "text"}}),
                "doer": agent(inputs={"plan": {"from": "planner.plan"}},
                              outputs={"done": {"type": "text"}})},
            "gates": {"check": {"kind": "human", "label": "Approve the plan?"}},
            "edges": edges}


@pytest.fixture
def flows(tmp_path: Path) -> Path:
    return tmp_path / "flows"


def _go(flows, rework, decisions):
    write_flow(flows, _flow(rework))
    seen = []

    def plan(rid, n):
        from agent_flow_studio.tools import io as io_tool
        seen.append(io_tool.resolve_inputs(io_tool.spec_for(rid), rid, "planner")[0])
        return {"plan": f"plan {n}"}

    q = agent_query({"planner": plan}, defaults={"doer": {"done": "ok"}})
    rn = Runner(flows, q)

    async def go():
        r = rn.start("plan", inputs={"topic": "t"}, run_id="p1")
        await wait(r)
        out = []
        for d, comment in decisions:
            out.append((r.status, (r.gate or {}).get("gate"), (r.gate or {}).get("reason")))
            rn.decide(r.id, d, comment)
            await wait(r)
        return r, out

    r, out = run(go())
    return r, out, q, seen


def test_answer_without_a_matching_link_asks_again_instead_of_ending(flows):
    r, _, q, _ = _go(flows, rework=False, decisions=[("answer", "1. no 2. yes")])
    assert r.status == "gate" and r.gate["gate"] == "check"  # still waiting at the gate
    assert "answer" in r.gate["reason"] and "none of the links" in r.gate["reason"]
    assert "doer" not in ran(q) and r.engine.state["doer"] == "waiting"  # not skipped
    r2, _, q2, _ = _go(flows, rework=False, decisions=[("answer", "x"), ("approve", "")])
    assert r2.status == "done" and "doer" in ran(q2)  # then approving goes on


def test_reject_without_a_link_still_ends_the_run(flows):
    r, _, q, _ = _go(flows, rework=False, decisions=[("reject", "no")])
    assert r.status == "done" and "doer" not in ran(q)  # a rejection ends it, as before
    assert r.engine.state["doer"] == "skipped"


def test_the_comment_reaches_the_agent_when_the_gate_sends_it_back(flows):
    r, _, q, seen = _go(flows, rework=True, decisions=[("answer", "use the new mapper"),
                                                       ("approve", "")])
    assert r.status == "done" and ran(q) == ["planner", "planner", "doer"]
    assert "feedback" not in seen[0]  # first pass: no comment yet (and not "missing")
    assert seen[1]["feedback"] == "use the new mapper"


def test_spec_accepts_only_gate_decision_or_comment():
    base = _flow(True)
    FlowSpec.model_validate(base)
    bad = _flow(True)
    bad["agents"]["planner"]["inputs"]["feedback"] = {"from": "check.nope"}
    with pytest.raises(ValueError, match="human gate"):
        FlowSpec.model_validate(bad)

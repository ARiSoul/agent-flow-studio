"""Graph engine (execution: graph) with a fake SDK: scheduling, parallelism, joins, skips,
conditions (`if` and a fake router for `when`), loops, follow-ups, gates, test modes, stop,
costs and event shapes. Also compile_agent, the orchestrator-mode prompt/next_steps and the CLI.
No real model is called."""
import asyncio
import inspect
import json
import re
import shutil
import time
from pathlib import Path

import pytest
import yaml
from claude_agent_sdk import AssistantMessage, ResultMessage, TextBlock, ToolUseBlock

from agent_flow_studio import main as cli
from agent_flow_studio.builder import load_spec
from agent_flow_studio.builder.compile import (
    AGENT_RULES,
    NEXT_TOOL,
    compile_agent,
    compile_flow,
    flow_section,
    next_steps_result,
)
from agent_flow_studio.builder.graph import back_edges, parallel_groups
from agent_flow_studio.editor.runner import Runner
from agent_flow_studio.engine import ROUTER_PROMPT, make_llm_router, parse_route
from agent_flow_studio.tools import io as io_tool

FIX = Path(__file__).parent / "fixtures" / "flows"
AGENT = re.compile(r"agent `([a-z][a-z0-9_]*)`")
RUN = re.compile(r"[Rr]un[ _]id:? `([^`]+)`")
PAR = {
    "planner": {"question_a": "qa", "question_b": "qb", "size": 2},
    "researcher_a": {"answer": "A", "confidence": 0.9},
    "researcher_b": {"answer": "B", "confidence": 0.8},
    "writer": {"report": "R"},
}


# --- helpers -----------------------------------------------------------------------------------
def _am(*blocks):
    return AssistantMessage(content=list(blocks), model="m")


def agent_query(behave=None, defaults=None):
    """Fake SDK: one session per agent (found in the prompt). `behave[agent](run_id, n)` returns
    the outputs to save (it can be async, to take time); the fake saves them as `set_output`
    would. Every call is logged with its time window."""
    behave = behave or {}
    defaults = PAR if defaults is None else defaults
    log: list[dict] = []

    async def q(prompt, options):
        agent = AGENT.search(prompt).group(1)
        rid = RUN.search(prompt).group(1)
        n = sum(1 for c in log if c["agent"] == agent) + 1
        entry = {"agent": agent, "n": n, "prompt": prompt, "options": options,
                 "resume": options.resume, "start": time.perf_counter()}
        log.append(entry)
        yield _am(TextBlock(f"{agent} working"))
        fn = behave.get(agent)
        values = fn(rid, n) if fn else defaults.get(agent, {})
        if inspect.isawaitable(values):
            values = await values
        for k, v in (values or {}).items():
            yield _am(ToolUseBlock(f"t-{agent}-{n}-{k}", "mcp__io__set_output",
                                   {"run_id": rid, "agent": agent, "name": k, "value": v}))
            io_tool.store_output(io_tool._spec, rid, agent, k, v)
        entry["end"] = time.perf_counter()
        yield ResultMessage(subtype="success", duration_ms=1, duration_api_ms=1, is_error=False,
                            num_turns=2, session_id=f"s-{agent}-{n}", total_cost_usd=0.01)

    q.log = log
    return q


def ran(q):
    return [c["agent"] for c in q.log]


def write_flow(base: Path, data: dict) -> str:
    d = base / data["name"]
    d.mkdir(parents=True, exist_ok=True)
    (d / "flow.yaml").write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    return data["name"]


def agent(desc="Does a step.", **kw):
    return {"description": desc, "prompt": "Do the step.", **kw}


@pytest.fixture
def flows(tmp_path: Path) -> Path:
    shutil.copytree(FIX / "parallel", tmp_path / "flows/parallel")
    return tmp_path / "flows"


async def wait(r):
    while r.task and not r.task.done():
        await asyncio.sleep(0.01)


def run(coro):
    return asyncio.run(coro)


def types(r, t):
    return [e for e in r.events if e["type"] == t]


# --- sequence, events, costs -------------------------------------------------------------------
def test_full_run_sequence_events_and_costs(flows):
    q = agent_query()
    rn = Runner(flows, q)

    async def go():
        r = rn.start("parallel", inputs={"topic": "agents"}, run_id="g1")
        await wait(r)
        return r

    r = run(go())
    assert r.status == "gate" and r.gate["gate"] == "approve" and r.gate["kind"] == "human"
    assert ran(q)[0] == "planner" and ran(q)[-1] == "writer"
    assert sorted(ran(q)) == ["planner", "researcher_a", "researcher_b", "writer"]
    assert r.cost == pytest.approx(0.04) and r.done_agents == set(PAR)
    start = r.events[0]
    assert start["type"] == "start" and start["execution"] == "graph"
    assert types(r, "delegate")[0] == {**types(r, "delegate")[0], "agent": "planner",
                                       "task": "Splits the topic into two questions."}
    assert {"agent": "planner", "names": ["question_a", "question_b", "size"]}.items() <= \
        types(r, "outputs")[0].items()
    tool = types(r, "tool")[0]
    assert tool["agent"] == "planner" and tool["tool"] == "set_output" and "value" not in tool
    assert types(r, "text")[0]["agent"] == "planner"
    res = types(r, "result")
    assert {e["agent"] for e in res} == set(PAR) and res[0]["session_id"] == "s-planner-1"
    edges = {(e["from"], e["to"]): (e["fired"], e["by"]) for e in types(r, "edge")}
    assert edges[("planner", "researcher_a")] == (True, "if")
    assert edges[("planner", "too_small")] == (False, "if")
    assert edges[("researcher_a", "writer")] == (True, "always")
    assert [e["node"] for e in types(r, "skipped")] == ["too_small"]
    gate = types(r, "gate")[0]
    assert gate == {**gate, "gate": "approve", "kind": "human", "label": "Check the report"}
    assert "writer" in gate["reason"]
    assert all(not e["error"] for e in types(r, "agent_done"))
    end = r.events[-1]
    assert end["type"] == "end" and end["status"] == "gate" and end["gate"]["gate"] == "approve"
    # the agent got its resolved inputs and the run id in the message
    writer = next(c for c in q.log if c["agent"] == "writer")
    assert "Run id: `g1`" in writer["prompt"] and "- `a`: A" in writer["prompt"]
    assert "`report`" in writer["prompt"] and "`sources` (optional)" in writer["prompt"]
    assert r.summary()["nodes"]["writer"] == "done"
    # the decision ends the run (the gate has no next steps)
    async def after():
        rn.decide(r.id, "approve", "fine")
        await wait(r)

    run(after())
    assert r.status == "done" and r.events[-1]["status"] == "done"
    lines = (io_tool.run_dir("g1") / "events.jsonl").read_text("utf-8").splitlines()
    assert len(lines) == len(r.events)


def test_parallel_branches_overlap_and_join_all_waits(flows):
    async def slow(values):
        await asyncio.sleep(0.15)
        return values

    q = agent_query({"researcher_a": lambda rid, n: slow(PAR["researcher_a"]),
                     "researcher_b": lambda rid, n: slow(PAR["researcher_b"])})
    rn = Runner(flows, q)

    async def go():
        r = rn.start("parallel", inputs={"topic": "agents"}, run_id="g2")
        await wait(r)
        return r

    run(go())
    w = {c["agent"]: c for c in q.log}
    a, b = w["researcher_a"], w["researcher_b"]
    assert a["start"] < b["end"] and b["start"] < a["end"]  # real overlap
    assert w["writer"]["start"] >= max(a["end"], b["end"])  # join all


def test_max_parallel_one_runs_one_at_a_time(flows):
    p = flows / "parallel/flow.yaml"
    data = yaml.safe_load(p.read_text("utf-8"))
    data["max_parallel"] = 1
    p.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")

    async def slow(values):
        await asyncio.sleep(0.05)
        return values

    q = agent_query({"researcher_a": lambda rid, n: slow(PAR["researcher_a"]),
                     "researcher_b": lambda rid, n: slow(PAR["researcher_b"])})
    rn = Runner(flows, q)

    async def go():
        r = rn.start("parallel", inputs={"topic": "agents"}, run_id="g3")
        await wait(r)

    run(go())
    w = {c["agent"]: c for c in q.log}
    a, b = w["researcher_a"], w["researcher_b"]
    assert a["end"] <= b["start"] or b["end"] <= a["start"]


def test_join_any_runs_at_the_first_branch(tmp_path):
    base = tmp_path / "flows"
    write_flow(base, {
        "name": "anyjoin", "title": "t", "execution": "graph", "inputs": {"x": {"type": "text"}},
        "start": "s",
        "agents": {"s": agent(), "fast": agent(), "slow": agent(),
                   "j": agent(join="any")},
        "edges": [{"from": "s", "to": "fast"}, {"from": "s", "to": "slow"},
                  {"from": "fast", "to": "j"}, {"from": "slow", "to": "j"}]})

    async def later():
        await asyncio.sleep(0.2)
        return {}

    q = agent_query({"slow": lambda rid, n: later()}, defaults={})
    rn = Runner(base, q)

    async def go():
        r = rn.start("anyjoin", inputs={"x": "1"}, run_id="j1")
        await wait(r)
        return r

    r = run(go())
    w = {c["agent"]: c for c in q.log}
    assert w["j"]["start"] < w["slow"]["end"]
    assert ran(q).count("j") == 1 and r.status == "done"


# --- skips, stop gate, conditions --------------------------------------------------------------
def test_skip_propagates_and_stop_gate_ends_the_run(flows):
    q = agent_query({"planner": lambda rid, n: {**PAR["planner"], "size": 1}})
    rn = Runner(flows, q)

    async def go():
        r = rn.start("parallel", inputs={"topic": "agents"}, run_id="s1")
        await wait(r)
        return r

    r = run(go())
    assert ran(q) == ["planner"]
    assert r.status == "done" and r.gate["gate"] == "too_small" and r.gate["kind"] == "stop"
    assert {e["node"] for e in types(r, "skipped")} == {"researcher_a", "researcher_b", "writer",
                                                         "approve"}
    assert r.events[-1] == {**r.events[-1], "type": "end", "status": "done"}


def _when_flow(base):
    return write_flow(base, {
        "name": "routed", "title": "t", "execution": "graph", "inputs": {"x": {"type": "text"}},
        "start": "a",
        "agents": {"a": agent(outputs={"t": {"type": "text"}}), "b": agent(), "c": agent(),
                   "d": agent()},
        "edges": [{"from": "a", "to": "b", "when": "the answer is long"},
                  {"from": "a", "to": "c", "when": "the answer is short"},
                  {"from": "a", "to": "d", "if": 'a.t == "zzz"', "when": "anything"}]})


def test_when_conditions_use_the_router(tmp_path):
    base = tmp_path / "flows"
    _when_flow(base)
    calls = []

    async def route(node, data, conditions):
        calls.append((node, data, conditions))
        return [True, False], 0.005

    q = agent_query(defaults={"a": {"t": "hello"}})
    rn = Runner(base, q, route)

    async def go():
        r = rn.start("routed", inputs={"x": "1"}, run_id="w1")
        await wait(r)
        return r

    r = run(go())
    assert calls == [("a", {"outputs": {"t": "hello"}, "summary": "a working"},
                      ["the answer is long", "the answer is short"])]  # d: `if` is false
    edges = {e["to"]: (e["fired"], e["by"]) for e in types(r, "edge")}
    assert edges == {"b": (True, "when"), "c": (False, "when"), "d": (False, "both")}
    assert sorted(ran(q)) == ["a", "b"]
    assert {e["node"] for e in types(r, "skipped")} == {"c", "d"}
    assert r.cost == pytest.approx(0.025)


def test_router_failure_means_not_fired(tmp_path):
    base = tmp_path / "flows"
    _when_flow(base)

    async def route(node, data, conditions):
        raise ValueError("bad answer")

    q = agent_query(defaults={"a": {"t": "zzz"}})
    rn = Runner(base, q, route)

    async def go():
        r = rn.start("routed", inputs={"x": "1"}, run_id="w2")
        await wait(r)
        return r

    r = run(go())
    assert types(r, "route_error")[0]["node"] == "a"
    assert ran(q) == ["a"] and r.status == "done"
    assert all(not e["fired"] for e in types(r, "edge"))


def test_default_router_parses_the_answer():
    assert parse_route('Sure: {"holds": [true, false]}', 2) == [True, False]
    with pytest.raises(ValueError):
        parse_route('{"holds": [true]}', 2)
    with pytest.raises(ValueError):
        parse_route("no json", 1)
    seen = []

    async def fake(prompt, options):
        seen.append(options)
        yield ResultMessage(subtype="success", duration_ms=1, duration_api_ms=1, is_error=False,
                            num_turns=1, session_id="r", total_cost_usd=0.001,
                            result='{"holds": [false, true]}')

    holds, cost = run(make_llm_router(fake)("a", {"outputs": {}}, ["x", "y"]))
    assert holds == [False, True] and cost == pytest.approx(0.001)
    o = seen[0]
    assert o.system_prompt == ROUTER_PROMPT and o.tools == [] and o.allowed_tools == []
    assert o.model == "haiku" and o.permission_mode == "dontAsk" and o.setting_sources == []
    assert "Agent" in o.disallowed_tools and o.max_turns == 1


# --- loops -------------------------------------------------------------------------------------
def _loop_flow(base):
    return write_flow(base, {
        "name": "looped", "title": "t", "execution": "graph", "inputs": {"x": {"type": "text"}},
        "start": "prep",
        "agents": {
            "prep": agent(outputs={"topic": {"type": "text"}}),
            "writer": agent(max_runs=2, inputs={"topic": {"from": "prep.topic"},
                                                "notes": {"from": "reviewer.notes",
                                                          "required": False}},
                            outputs={"draft": {"type": "text"}}),
            "reviewer": agent(inputs={"draft": {"from": "writer.draft"}},
                              outputs={"ok": {"type": "boolean"},
                                       "notes": {"type": "text", "required": False}}),
        },
        "gates": {"publish": {"kind": "human", "label": "Publish"}},
        "edges": [{"from": "prep", "to": "writer"}, {"from": "writer", "to": "reviewer"},
                  {"from": "reviewer", "to": "writer", "if": "reviewer.ok == false"},
                  {"from": "reviewer", "to": "publish", "if": "reviewer.ok"}]})


def test_loop_reruns_until_max_runs(tmp_path):
    base = tmp_path / "flows"
    _loop_flow(base)
    spec, _ = load_spec(base / "looped/flow.yaml")
    assert back_edges(spec) == {2}
    q = agent_query(defaults={"prep": {"topic": "t"}, "writer": {"draft": "d"},
                              "reviewer": {"ok": False, "notes": "fix the intro"}})
    rn = Runner(base, q)

    async def go():
        r = rn.start("looped", inputs={"x": "1"}, run_id="l1")
        await wait(r)
        return r

    r = run(go())
    assert ran(q) == ["prep", "writer", "reviewer", "writer", "reviewer"]
    assert len(types(r, "loop")) == 1 and types(r, "loop")[0]["to"] == "writer"
    limit = types(r, "loop_limit")[0]
    assert limit["agents"] == ["writer"]
    last_back = [e for e in types(r, "edge") if e["to"] == "writer" and e["from"] == "reviewer"]
    assert last_back[-1]["fired"] is False and last_back[-1]["limit"] is True
    # the second writer run reads the reviewer's notes (feedback through the loop)
    assert "fix the intro" in [c for c in q.log if c["agent"] == "writer"][1]["prompt"]
    assert r.status == "done"


def test_loop_then_gate(tmp_path):
    base = tmp_path / "flows"
    _loop_flow(base)
    q = agent_query({"reviewer": lambda rid, n: {"ok": n > 1}},
                    defaults={"prep": {"topic": "t"}, "writer": {"draft": "d"}})
    rn = Runner(base, q)

    async def go():
        r = rn.start("looped", inputs={"x": "1"}, run_id="l2")
        await wait(r)
        return r

    r = run(go())
    assert ran(q) == ["prep", "writer", "reviewer", "writer", "reviewer"]
    assert r.status == "gate" and r.gate["gate"] == "publish"


# --- missing outputs ---------------------------------------------------------------------------
def test_missing_outputs_follow_up_then_fail(flows):
    q = agent_query({"researcher_a": lambda rid, n: {}})
    rn = Runner(flows, q)

    async def go():
        r = rn.start("parallel", inputs={"topic": "agents"}, run_id="m1")
        await wait(r)
        return r

    r = run(go())
    calls = [c for c in q.log if c["agent"] == "researcher_a"]
    assert len(calls) == 2 and calls[1]["resume"] == "s-researcher_a-1"
    assert "`answer`" in calls[1]["prompt"]
    assert types(r, "follow_up")[0] == {**types(r, "follow_up")[0], "agent": "researcher_a",
                                        "missing": ["answer", "confidence"]}
    failed = [e for e in types(r, "agent_done") if e["error"]]
    assert failed[0]["agent"] == "researcher_a" and "answer" in failed[0]["message"]
    assert "writer" not in ran(q) and "researcher_b" in ran(q)
    assert r.status == "error" and "researcher_a" in r.events[-1]["message"]


def test_missing_outputs_saved_after_the_follow_up(flows):
    q = agent_query({"researcher_a": lambda rid, n: PAR["researcher_a"] if n == 2 else {}})
    rn = Runner(flows, q)

    async def go():
        r = rn.start("parallel", inputs={"topic": "agents"}, run_id="m2")
        await wait(r)
        return r

    r = run(go())
    assert r.status == "gate" and "writer" in ran(q)


def test_missing_required_input_fails_the_node(flows):
    q = agent_query({"planner": lambda rid, n: {"question_a": "qa", "size": 2}})
    # question_b becomes optional for the planner, but researcher_b needs it
    p = flows / "parallel/flow.yaml"
    data = yaml.safe_load(p.read_text("utf-8"))
    data["agents"]["planner"]["outputs"]["question_b"]["required"] = False
    p.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    rn = Runner(flows, q)

    async def go():
        r = rn.start("parallel", inputs={"topic": "agents"}, run_id="m3")
        await wait(r)
        return r

    r = run(go())
    assert "researcher_b" not in ran(q)
    failed = [e for e in types(r, "agent_done") if e["error"]]
    assert failed[0]["agent"] == "researcher_b" and "question" in failed[0]["message"]
    assert r.status == "error"


# --- human gate with edges ---------------------------------------------------------------------
def test_human_gate_pauses_and_the_decision_picks_the_edge(tmp_path):
    base = tmp_path / "flows"
    write_flow(base, {
        "name": "gated", "title": "t", "execution": "graph", "inputs": {"x": {"type": "text"}},
        "start": "a", "agents": {"a": agent(), "b": agent(), "c": agent()},
        "gates": {"review": {"kind": "human", "label": "Review"}},
        "edges": [{"from": "a", "to": "review"},
                  {"from": "review", "to": "b", "if": 'review.decision == "approve"'},
                  {"from": "review", "to": "c", "if": 'review.decision == "reject"'}]})
    q = agent_query(defaults={})
    rn = Runner(base, q)

    async def go():
        r = rn.start("gated", inputs={"x": "1"}, run_id="h1")
        await wait(r)
        assert r.status == "gate" and r.gate["gate"] == "review" and ran(q) == ["a"]
        rn.decide(r.id, "approve", "ok")
        await wait(r)
        return r

    r = run(go())
    assert ran(q) == ["a", "b"] and r.status == "done"
    assert {e["to"]: e["fired"] for e in types(r, "edge") if e["from"] == "review"} == \
        {"b": True, "c": False}
    assert [e["node"] for e in types(r, "skipped")] == ["c"]


# --- test modes --------------------------------------------------------------------------------
def test_step_mode_runs_one_node_at_a_time(flows):
    q = agent_query()
    rn = Runner(flows, q)

    async def go():
        r = rn.start("parallel", inputs={"topic": "agents"}, run_id="st1", mode="step")
        await wait(r)
        assert r.status == "paused" and ran(q) == ["planner"]
        rn.continue_(r.id, "step")
        await wait(r)
        assert r.status == "paused" and ran(q) == ["planner", "researcher_a"]
        rn.continue_(r.id, "full")
        await wait(r)
        return r

    r = run(go())
    assert r.status == "gate" and ran(q)[-1] == "writer" and len(ran(q)) == 4


def test_until_mode_runs_what_leads_to_the_target(flows):
    q = agent_query()
    rn = Runner(flows, q)

    async def go():
        r = rn.start("parallel", inputs={"topic": "agents"}, run_id="u1", mode="until",
                     target="researcher_a")
        await wait(r)
        assert r.status == "paused" and ran(q) == ["planner", "researcher_a"]
        rn.continue_(r.id, "full")
        await wait(r)
        return r

    r = run(go())
    assert r.status == "gate" and sorted(ran(q)) == sorted(PAR)


def test_agent_mode_runs_only_the_target(flows):
    q = agent_query()
    rn = Runner(flows, q)

    async def go():
        r = rn.start("parallel", inputs={"topic": "agents"}, run_id="ag1", mode="agent",
                     target="writer", given={"a": "first", "b": "second"},
                     instruction="Keep it short.")
        await wait(r)
        return r

    r = run(go())
    assert ran(q) == ["writer"] and r.status == "done"
    assert not types(r, "edge") and not types(r, "gate")
    prompt = q.log[0]["prompt"]
    assert "- `a`: first" in prompt and "Keep it short." in prompt


def test_stop_cancels_running_agents(flows):
    cancelled = []

    async def forever(name):
        try:
            await asyncio.sleep(10)
        except asyncio.CancelledError:
            cancelled.append(name)
            raise

    q = agent_query({"researcher_a": lambda rid, n: forever("a"),
                     "researcher_b": lambda rid, n: forever("b")})
    rn = Runner(flows, q)

    async def go():
        r = rn.start("parallel", inputs={"topic": "agents"}, run_id="x1")
        while ran(q).count("researcher_a") + ran(q).count("researcher_b") < 2:
            await asyncio.sleep(0.01)
        rn.stop(r.id)
        await wait(r)
        return r

    r = run(go())
    assert r.status == "stopped" and sorted(cancelled) == ["a", "b"]
    assert r.events[-1] == {**r.events[-1], "type": "end", "status": "stopped"}
    assert "writer" not in ran(q)


# --- compile_agent -----------------------------------------------------------------------------
def test_compile_agent_is_one_closed_session():
    spec, d = load_spec(FIX / "parallel/flow.yaml")
    o = compile_agent(spec, d, "planner", resolve_secrets=False)
    assert o.tools == [] and o.agents is None
    assert o.allowed_tools == ["mcp__io__get_inputs", "mcp__io__set_output", "mcp__io__read_file"]
    assert set(o.mcp_servers) == {"io"}
    assert "Agent" in o.disallowed_tools and "Task" in o.disallowed_tools
    assert o.permission_mode == "dontAsk" and o.setting_sources == []
    assert o.system_prompt.endswith(AGENT_RULES) and "## Inputs and outputs" in o.system_prompt
    assert o.max_turns == 40 and o.cwd is None
    guard = o.hooks["PreToolUse"][0].hooks[0]
    assert o.hooks["PreToolUse"][0].on_failure == "block"

    def call(tool, tool_input):
        data = {"tool_name": tool, "agent_type": None, "session_id": "s", "tool_input": tool_input}
        return run(guard(data, "id", {}))

    assert call("mcp__io__set_output", {"agent": "planner", "run_id": "r"}) == {}
    deny = "hookSpecificOutput"
    assert deny in call("mcp__io__set_output", {"agent": "writer", "run_id": "r"})
    assert deny in call("Agent", {"subagent_type": "writer"})
    assert deny in call("mcp__artifacts__save_artifact", {})


def test_compile_agent_model_and_turns(tmp_path):
    spec, d = load_spec(FIX / "parallel/flow.yaml")
    data = spec.model_dump(by_alias=True, exclude_defaults=False)
    data["model"] = "sonnet"
    data["agents"]["writer"]["model"] = "haiku"
    data["agents"]["writer"]["max_turns"] = 7
    data["agents"]["planner"]["model"] = "inherit"
    s2 = type(spec).model_validate(data)
    assert compile_agent(s2, d, "writer").model == "haiku"
    assert compile_agent(s2, d, "writer").max_turns == 7
    assert compile_agent(s2, d, "planner").model == "sonnet"


# --- orchestrator mode -------------------------------------------------------------------------
def test_orchestrator_prompt_lists_io_groups_and_if():
    spec, d = load_spec(FIX / "parallel/flow.yaml")
    text = flow_section(spec)
    assert "Inputs: `question` (from planner.question_a)" in text
    assert "Outputs: `answer` (markdown), `confidence` (number 0..1)" in text
    assert "- `researcher_a`, `researcher_b`" in text
    assert "if `planner.size > 1` (check with `next_steps`): delegate to `researcher_a`" in text
    assert parallel_groups(spec) == [["researcher_a", "researcher_b"]]
    cf = compile_flow(spec, d, resolve_secrets=False, configure_artifacts=False)
    assert NEXT_TOOL in cf.options.allowed_tools
    legacy, ld = load_spec(FIX / "pipeline/flow.yaml")
    lf = compile_flow(legacy, ld, resolve_secrets=False, configure_artifacts=False)
    assert NEXT_TOOL not in lf.options.allowed_tools
    assert "next_steps" not in flow_section(legacy) and "same message" not in flow_section(legacy)


def test_next_steps_evaluates_the_if_edges():
    spec, _ = load_spec(FIX / "parallel/flow.yaml")
    io_tool.configure(spec, None)
    io_tool.store_output(spec, "n1", "planner", "size", 3)
    res = next_steps_result(spec, "n1", "planner")
    data = json.loads(res["content"][0]["text"])
    holds = {s["to"]: s["if_holds"] for s in data["next"]}
    assert holds == {"researcher_a": True, "researcher_b": True, "too_small": False}
    assert data["next"][0]["if"] == "planner.size > 1" and data["next"][0]["when"] is None
    assert next_steps_result(spec, "n1", "nobody").get("is_error")


# --- CLI ---------------------------------------------------------------------------------------
def test_cli_parse_sets_and_inputs(tmp_path):
    f = tmp_path / "notes.md"
    f.write_text("# hi", encoding="utf-8")
    sets = cli.parse_sets(["topic=a b", "tag=x", "tag=y", f"docs=@{f}", "raw=@@at"])
    assert sets["topic"] == "a b" and sets["tag"] == ["x", "y"] and sets["raw"] == "@at"
    assert sets["docs"]["name"] == "notes.md" and sets["docs"]["content_b64"] == "IyBoaQ=="
    with pytest.raises(ValueError):
        cli.parse_sets(["novalue"])
    with pytest.raises(ValueError):
        cli.parse_sets([f"docs=@{tmp_path / 'missing.md'}"])
    legacy, _ = load_spec(FIX / "pipeline/flow.yaml")
    assert cli.collect_inputs(legacy, "INC1", {}, None) == ("INC1", {"input": "INC1"})
    with pytest.raises(ValueError):
        cli.collect_inputs(legacy, None, {}, None)
    typed, _ = load_spec(FIX / "parallel/flow.yaml")
    rid, raw = cli.collect_inputs(typed, None, {"topic": "x"}, "r-1")
    assert rid == "r-1" and raw == {"topic": "x"}
    assert cli.collect_inputs(typed, None, {}, None)[0].startswith("parallel-")
    with pytest.raises(ValueError):
        cli.collect_inputs(typed, "INC1", {}, None)
    with pytest.raises(ValueError):
        cli.collect_inputs(typed, None, {}, "bad id!")
    args = cli.build_parser().parse_args(["--flow", "parallel", "--set", "topic=x",
                                          "--run-id", "r2"])
    assert args.flow == "parallel" and args.sets == ["topic=x"] and args.input is None


def test_cli_runs_a_graph_flow_with_a_file(tmp_path):
    f = tmp_path / "notes.md"
    f.write_text("# notes", encoding="utf-8")
    q = agent_query()
    lines: list[str] = []
    cli.main(["--flow", "parallel", "--set", "topic=agents", "--set", f"docs=@{f}",
              "--run-id", "cli-1"], query_fn=q, out=lines.append)
    text = "\n".join(lines)
    assert "Run id: cli-1" in text and "> planner:" in text and "= writer done" in text
    assert "planner -> too_small: not fired (if)" in text and "skipped: too_small" in text
    assert "# gate approve (human)" in text and "[gate] run cli-1: cost~0.0400 USD" in text
    assert io_tool.flow_inputs("cli-1")["docs"][0]["name"] == "notes.md"
    assert "docs" in q.log[0]["prompt"]


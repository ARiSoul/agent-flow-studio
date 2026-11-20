"""Typed inputs/outputs: types, expressions, flow validation, io tools, hook identity, compile,
runner and API (foundation of both execution modes)."""
import asyncio
import base64
import copy
import json
import shutil
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient

from agent_flow_studio import hooks
from agent_flow_studio.builder import expr, load_spec
from agent_flow_studio.builder.compile import compile_flow
from agent_flow_studio.builder.spec import FlowSpec
from agent_flow_studio.builder.types import IOType, check_value, compatible
from agent_flow_studio.editor.runner import RunError, Runner, prepare_inputs
from agent_flow_studio.editor.server import create_app
from agent_flow_studio.tools import io as io_tool

FIX = Path(__file__).parent / "fixtures" / "flows"
PARALLEL = FIX / "parallel"
H = {"X-Flow-Editor": "1"}


def raw_parallel() -> dict:
    return yaml.safe_load((PARALLEL / "flow.yaml").read_text(encoding="utf-8"))


def spec_parallel() -> FlowSpec:
    return load_spec(PARALLEL / "flow.yaml")[0]


# --- types -----------------------------------------------------------------------------------
@pytest.mark.parametrize("t,value,expected", [
    ({"type": "text", "max": 5}, "abc", "abc"),
    ({"type": "number"}, "3,5", 3.5),
    ({"type": "integer", "min": 1}, "4", 4),
    ({"type": "boolean"}, "yes", True),
    ({"type": "date"}, "2026-11-19T10:00", "2026-11-19"),
    ({"type": "datetime"}, "2026-11-19T10:00:00Z", "2026-11-19T10:00:00+00:00"),
    ({"type": "url"}, " https://example.com/x ", "https://example.com/x"),
    ({"type": "choice", "options": ["a", "b"]}, "b", "b"),
    ({"type": "json"}, '{"a": [1]}', {"a": [1]}),
    ({"type": "text", "multiple": True}, "one", ["one"]),
    ({"type": "integer", "required": False}, None, None),
    ({"type": "integer", "default": 7}, None, 7),
])
def test_check_value_ok(t, value, expected):
    assert check_value(IOType(**t), value) == expected


@pytest.mark.parametrize("t,value,needle", [
    ({"type": "text"}, "", "required"),
    ({"type": "text", "max": 2}, "abc", "longer"),
    ({"type": "text", "pattern": "[a-z]+"}, "A1", "pattern"),
    ({"type": "integer"}, 2.5, "whole"),
    ({"type": "number", "max": 1}, 3, "greater"),
    ({"type": "boolean"}, "maybe", "true or false"),
    ({"type": "date"}, "19/11/2026", "YYYY-MM-DD"),
    ({"type": "url"}, "ftp://x", "http"),
    ({"type": "choice", "options": ["a"]}, "z", "one of"),
    ({"type": "json"}, "{bad", "valid JSON"),
    ({"type": "text"}, ["a", "b"], "not a list"),
    ({"type": "integer", "multiple": True}, [1, "x"], "item 2"),
])
def test_check_value_errors(t, value, needle):
    with pytest.raises(ValueError, match=needle):
        check_value(IOType(**t), value)


def test_paths_and_files_stay_in_their_folders(tmp_path):
    ws, rd = tmp_path / "ws", tmp_path / "run"
    (ws / "src").mkdir(parents=True)
    (rd / "inputs").mkdir(parents=True)
    (rd / "inputs" / "a.md").write_text("x", encoding="utf-8")
    p = IOType(type="path")
    assert check_value(p, "src", roots=[ws, rd]) == str((ws / "src").resolve())
    with pytest.raises(ValueError, match="outside"):
        check_value(p, str(tmp_path), roots=[ws, rd])
    f = IOType(type="file", accept=[".md"])
    assert check_value(f, {"path": "inputs/a.md"}, run_dir=rd)["size"] == 1
    with pytest.raises(ValueError, match="not found"):
        check_value(f, {"path": "../ws/x.md"}, run_dir=rd)
    with pytest.raises(ValueError, match="not accepted"):
        check_value(f, {"path": "inputs/a.pdf"}, run_dir=rd)


@pytest.mark.parametrize("t,needle", [
    ({"type": "choice"}, "options"),
    ({"type": "text", "options": ["a"]}, "only for choice"),
    ({"type": "text", "accept": [".md"]}, "only for file"),
    ({"type": "file", "accept": ["md"]}, "look like"),
    ({"type": "text", "draft": True}, "only for markdown"),
    ({"type": "number", "min": 5, "max": 1}, "greater"),
    ({"type": "integer", "default": "x"}, "invalid default"),
])
def test_type_rules(t, needle):
    with pytest.raises(ValueError, match=needle):
        IOType(**t)


def test_compatible():
    assert compatible("integer", "number") and compatible("date", "text")
    assert not compatible("text", "number") and not compatible("number", "integer")


# --- expressions -------------------------------------------------------------------------------
SCOPE = {"input": {"n": 3}, "triage": {"severity": "high", "items": [{"a": 1}]},
         "approve": {"decision": "approve"}}


@pytest.mark.parametrize("text,expected", [
    ('triage.severity == "high"', True),
    ("input.n > 2 and input.n < 4", True),
    ("len(triage.items) == 1 and triage.items[0].a == 1", True),
    ('approve.decision in ["approve", "answer"]', True),
    ("exists(triage.missing)", False),
    ("triage.missing > 1", False),
    ('not contains(lower(triage.severity), "low")', True),
    ("input.n - 1 == 2", True),
    ("true and not false", True),
])
def test_expressions(text, expected):
    assert expr.evaluate(text, SCOPE) is expected


@pytest.mark.parametrize("text", [
    '__import__("os").system("x")', "triage.__class__", "open('f')", "triage.items.pop()",
    "[x for x in input]", "lambda: 1", "input.n * 2", "f'{input.n}'", "{'a': 1}", "",
    "triage['__dict__']",
])
def test_expressions_refused(text):
    with pytest.raises(expr.ExprError):
        expr.parse(text)


# --- flow validation ---------------------------------------------------------------------------
def test_parallel_fixture_is_valid():
    s = spec_parallel()
    assert s.execution == "graph" and set(s.inputs) == {"topic", "depth", "since", "docs"}
    assert s.input_type("researcher_a", "question") == "text"
    assert s.ancestors("writer") == {"planner", "researcher_a", "researcher_b"}


@pytest.mark.parametrize("patch,needle", [
    (lambda d: d["agents"]["writer"]["inputs"].update(x={"from": "input.nope"}),
     "flow input 'nope' does not exist"),
    (lambda d: d["agents"]["writer"]["inputs"].update(x={"from": "ghost.a"}),
     "agent 'ghost' does not exist"),
    (lambda d: d["agents"]["writer"]["inputs"].update(x={"from": "planner.nope"}),
     "has no output 'nope'"),
    (lambda d: d["agents"]["planner"]["inputs"].update(x={"from": "writer.report"}),
     "does not run before"),
    (lambda d: d["agents"]["writer"]["inputs"].update(x={"from": "planner.size", "type": "date"}),
     "cannot feed"),
    (lambda d: d["agents"]["writer"]["inputs"].update(x={"from": "writer.report"}),
     "its own output"),
    (lambda d: d["edges"][0].update({"if": "planner.nope > 1"}), "has no output 'nope'"),
    (lambda d: d["edges"][0].update({"if": "open('x')"}), "only these functions"),
    (lambda d: d["edges"][0].update({"if": "approve.who == 1"}), "only .decision"),
    (lambda d: d.update(task="Do {nothing}"), "not a flow input"),
    (lambda d: d["inputs"].update(Bad={"type": "text"}), "invalid name"),
    (lambda d: d["agents"]["planner"]["outputs"].update(x={"type": "choice"}), "options"),
])
def test_io_validation(patch, needle):
    d = raw_parallel()
    patch(d)
    with pytest.raises(ValueError, match=needle):
        FlowSpec.model_validate(d)


def test_order_rule_only_in_graph_mode():
    d = raw_parallel()
    d["agents"]["planner"]["inputs"]["x"] = {"from": "writer.report", "required": False}
    with pytest.raises(ValueError, match="does not run before"):
        FlowSpec.model_validate(d)
    d["execution"] = "orchestrator"
    assert FlowSpec.model_validate(d)


def test_legacy_flow_has_one_implicit_input():
    s, _ = load_spec(FIX / "pipeline" / "flow.yaml")
    assert list(s.flow_inputs()) == ["input"] and s.execution == "orchestrator"


# --- io tools ----------------------------------------------------------------------------------
def test_outputs_flow_to_the_next_agent(tmp_path):
    s = spec_parallel()
    io_tool.save_flow_inputs("r1", {"topic": "llm", "depth": "quick"})
    assert io_tool.resolve_inputs(s, "r1", "planner") == ({"topic": "llm", "depth": "quick"}, [])
    assert io_tool.store_output(s, "r1", "planner", "size", "3") == 3
    with pytest.raises(ValueError, match="greater"):
        io_tool.store_output(s, "r1", "planner", "size", 50)
    with pytest.raises(ValueError, match="no output"):
        io_tool.store_output(s, "r1", "planner", "nope", 1)
    io_tool.store_output(s, "r1", "planner", "question_a", "What?")
    assert io_tool.resolve_inputs(s, "r1", "researcher_a") == ({"question": "What?"}, [])
    assert io_tool.resolve_inputs(s, "r1", "researcher_b") == ({}, ["question"])
    report = io_tool.store_output(s, "r1", "writer", "report", "# R")
    assert report.startswith("> **DRAFT")  # drafts are always marked
    sc = io_tool.scope(s, "r1")
    assert expr.evaluate("planner.size > 1 and input.topic == 'llm'", sc)


def test_file_outputs_and_read_file(tmp_path):
    d = raw_parallel()
    d["agents"]["writer"]["outputs"]["attachment"] = {"type": "file", "accept": [".md"]}
    s = FlowSpec.model_validate(d)
    ref = io_tool.store_output(s, "r2", "writer", "attachment",
                               {"filename": "notes.md", "content": "hello"})
    assert ref == {"name": "notes.md", "path": "files/writer/notes.md", "size": 5}
    assert io_tool.read_run_file("r2", "files/writer/notes.md") == "hello"
    with pytest.raises(ValueError, match="filename"):
        io_tool.store_output(s, "r2", "writer", "attachment", {"filename": "../x.md", "content": "x"})
    with pytest.raises(ValueError, match="not found"):
        io_tool.read_run_file("r2", "../../etc/passwd")


def test_io_tool_calls():
    s = spec_parallel()
    io_tool.configure(s, None)
    try:
        io_tool.save_flow_inputs("r3", {"topic": "t", "depth": "deep"})
        out = asyncio.run(io_tool.get_inputs.handler({"run_id": "r3", "agent": "planner"}))
        data = json.loads(out["content"][0]["text"])
        assert data["inputs"] == {"topic": "t", "depth": "deep"}
        assert data["outputs_to_save"]["size"] == "integer"
        bad = asyncio.run(io_tool.set_output.handler(
            {"run_id": "r3", "agent": "planner", "name": "size", "value": "x"}))
        assert bad["is_error"] and "must be a number" in bad["content"][0]["text"]
    finally:
        io_tool.configure(None, None)


def test_hook_checks_the_agent_identity():
    guard = hooks.make_guard(["mcp__io__set_output"], {"planner": ["mcp__io__set_output"]})

    def call(agent, who):
        data = {"tool_name": "mcp__io__set_output", "agent_type": agent, "session_id": "s",
                "tool_input": {"agent": who, "run_id": "r", "name": "x", "value": 1}}
        return asyncio.run(guard(data, "id", {}))

    assert call("planner", "planner") == {}
    assert call("planner", "writer")["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert call(None, "planner")["hookSpecificOutput"]["permissionDecision"] == "deny"
    fixed = hooks.make_guard(["mcp__io__set_output"], fixed_agent="planner")
    data = {"tool_name": "Agent", "agent_type": None, "session_id": "s", "tool_input": {}}
    assert asyncio.run(fixed(data, "id", {}))["hookSpecificOutput"]["permissionDecision"] == "deny"
    data = {"tool_name": "mcp__io__set_output", "agent_type": None, "session_id": "s",
            "tool_input": {"agent": "planner"}}
    assert asyncio.run(fixed(data, "id", {})) == {}


# --- compile, runner, API ----------------------------------------------------------------------
def test_compile_gives_io_tools_and_prompt_section():
    s, d = load_spec(PARALLEL / "flow.yaml")
    cf = compile_flow(s, d, resolve_secrets=False, configure_artifacts=False)
    assert cf.agent_tools["planner"] == ["mcp__io__get_inputs", "mcp__io__set_output",
                                         "mcp__io__read_file"]
    assert "mcp__io__read_file" not in cf.agent_tools["writer"]
    assert "io" in cf.options.mcp_servers
    prompt = cf.options.agents["planner"].prompt
    assert "`docs` (file (.md, .txt)[], optional, from input.docs)" in prompt
    assert "`size` (integer 1..10, required)" in prompt
    task = cf.task_for("r1", {"topic": "llm", "depth": "deep"})
    assert task.startswith("Research llm (deep).") and "Run id: `r1`" in task


def test_prepare_inputs_with_files(tmp_path):
    s = spec_parallel()
    up = {"name": "a.md", "content_b64": base64.b64encode(b"# A").decode()}
    values = prepare_inputs(s, "r4", {"topic": "t", "docs": [up], "since": "2026-01-02"})
    assert values["depth"] == "quick" and values["since"] == "2026-01-02"
    assert values["docs"] == [{"name": "a.md", "path": "inputs/docs/a.md", "size": 3}]
    with pytest.raises(RunError) as e:
        prepare_inputs(s, "r5", {"depth": "huge", "docs": [{"name": "x.pdf", "content_b64": ""}],
                                 "extra": 1})
    assert set(e.value.detail["inputs"]) == {"topic", "depth", "docs", "extra"}
    assert not io_tool.run_dir("r5").exists()  # nothing left behind


def test_api_starts_a_typed_run(tmp_path):
    shutil.copytree(PARALLEL, tmp_path / "flows/parallel")
    shutil.copytree(FIX / "pipeline", tmp_path / "flows/pipeline")
    seen = []

    async def fake(prompt, options):
        seen.append(prompt)
        if False:
            yield None

    c = TestClient(create_app(tmp_path / "flows", allowed_hosts=["testserver"]))
    c.app.state  # noqa: B018 — the app is built
    r = c.post("/api/runs", json={"flow": "parallel", "inputs": {"depth": "deep"}}, headers=H)
    assert r.status_code == 422 and r.json()["detail"]["inputs"] == {"topic": "required"}
    r = c.post("/api/runs", json={"flow": "parallel", "run_id": "bad id!",
                                  "inputs": {"topic": "x"}}, headers=H)
    assert r.status_code == 422 and "run_id" in r.json()["detail"]
    legacy = c.post("/api/runs", json={"flow": "pipeline", "input": "../x"}, headers=H)
    assert legacy.status_code == 400


def test_runner_typed_start_saves_inputs_and_given(tmp_path):
    shutil.copytree(PARALLEL, tmp_path / "flows/parallel")
    prompts = []

    async def fake(prompt, options):
        prompts.append(prompt)
        if False:
            yield None

    rn = Runner(tmp_path / "flows", fake)

    async def go():
        r = rn.start("parallel", inputs={"topic": "agents"}, run_id="t-1", mode="agent",
                     target="researcher_a", given={"question": "Why?"})
        while r.task and not r.task.done():
            await asyncio.sleep(0.01)
        return r

    r = asyncio.run(go())
    assert r.input == "t-1"
    assert io_tool.flow_inputs("t-1") == {"topic": "agents", "depth": "quick"}
    s = spec_parallel()
    assert io_tool.resolve_inputs(s, "t-1", "researcher_a") == ({"question": "Why?"}, [])
    assert prompts and "t-1" in prompts[0]
    rid = Runner(tmp_path / "flows", fake)
    async def go2():
        r = rid.start("parallel", inputs={"topic": "x"})
        while r.task and not r.task.done():
            await asyncio.sleep(0.01)
        return r
    assert asyncio.run(go2()).input.startswith("parallel-")


def test_given_inputs_do_not_need_from():
    d = copy.deepcopy(raw_parallel())
    d["agents"]["writer"]["inputs"]["note"] = {"type": "text", "required": False}
    assert FlowSpec.model_validate(d).input_type("writer", "note") == "text"


def _start(rn, **kw):
    async def go():
        r = rn.start("parallel", **kw)
        while r.task and not r.task.done():
            await asyncio.sleep(0.01)
        return r
    return asyncio.run(go())


def test_agent_test_needs_only_the_agent_inputs(tmp_path):
    shutil.copytree(PARALLEL, tmp_path / "flows/parallel")

    async def fake(prompt, options):
        if False:
            yield None

    rn = Runner(tmp_path / "flows", fake)
    _start(rn, inputs={"topic": "agents", "depth": "deep"}, run_id="a-1", mode="agent",
           target="planner")
    # a later agent test: no flow inputs sent (topic is required in a full run) → saved ones stay
    _start(rn, run_id="a-1", mode="agent", target="researcher_a", given={"question": "Why?"})
    assert io_tool.flow_inputs("a-1") == {"topic": "agents", "depth": "deep"}
    with pytest.raises(RunError) as e:
        rn.start("parallel", run_id="a-1", mode="agent", target="planner",
                 given={"depth": "huge", "nope": 1})
    assert set(e.value.detail["given"]) == {"depth", "nope"}
    assert "one of" in e.value.detail["given"]["depth"]  # the type of the source (a choice)


def test_given_values_do_not_leak_into_a_full_run(tmp_path):
    shutil.copytree(PARALLEL, tmp_path / "flows/parallel")

    async def fake(prompt, options):
        if False:
            yield None

    rn = Runner(tmp_path / "flows", fake)
    s = spec_parallel()
    _start(rn, inputs={"topic": "t"}, run_id="a-2", mode="agent", target="researcher_a",
           given={"question": "Test only"})
    assert io_tool.resolve_inputs(s, "a-2", "researcher_a")[0] == {"question": "Test only"}
    io_tool.store_output(s, "a-2", "planner", "question_a", "Real question")
    _start(rn, run_id="a-2", mode="agent", target="researcher_a")  # nothing given this time
    assert io_tool.resolve_inputs(s, "a-2", "researcher_a")[0] == {"question": "Real question"}
    _start(rn, run_id="a-2", mode="agent", target="researcher_a", given={"question": "Again"})
    given = io_tool.run_dir("a-2") / "io" / "researcher_a.given.json"
    assert given.exists()
    _start(rn, inputs={"topic": "t"}, run_id="a-2")  # a full run clears every test value
    assert not given.exists()


AUTH_MSG = ("Claude Code returned an error result: Failed to authenticate: OAuth session expired "
            "and could not be refreshed (exit code: 1)")


def test_login_errors_get_a_hint_in_both_modes(tmp_path):
    from claude_agent_sdk import ResultMessage

    from agent_flow_studio.editor.runner import error_hint

    assert error_hint(AUTH_MSG) == "auth" and error_hint("Invalid API key · Please run /login")
    assert error_hint("HTTP 401 from connector devops") is None
    shutil.copytree(PARALLEL, tmp_path / "flows/parallel")
    shutil.copytree(FIX / "pipeline", tmp_path / "flows/pipeline")

    async def raising(prompt, options):
        raise RuntimeError(AUTH_MSG)
        yield None

    r = _start(Runner(tmp_path / "flows", raising), inputs={"topic": "t"}, run_id="auth-1")
    end = [e for e in r.events if e["type"] == "end"][-1]
    assert r.status == "error" and end.get("hint") == "auth"

    async def failed_result(prompt, options):
        yield ResultMessage(subtype="error_during_execution", duration_ms=1, duration_api_ms=1,
                            is_error=True, num_turns=0, session_id="s", result=AUTH_MSG)

    rn = Runner(tmp_path / "flows", failed_result)

    async def go():
        run = rn.start("pipeline", "INC9")
        while run.task and not run.task.done():
            await asyncio.sleep(0.01)
        return run

    run = asyncio.run(go())
    res = [e for e in run.events if e["type"] == "result"][-1]
    assert res["hint"] == "auth" and "OAuth session expired" in res["message"]


def test_agent_icon_is_a_short_label():
    d = raw_parallel()
    d["agents"]["planner"]["icon"] = "brain"
    d["agents"]["writer"]["icon"] = "📝"
    s = FlowSpec.model_validate(d)
    assert s.agents["planner"].icon == "brain" and s.agents["writer"].icon == "📝"
    for bad in ("x" * 17, "a\nb", "  "):
        d["agents"]["planner"]["icon"] = bad
        with pytest.raises(ValueError):
            FlowSpec.model_validate(d)

"""Action nodes (graph mode): the studio runs a connector action itself — no model, no session —
and saves the values of its result as typed outputs (`from`, `match`). No real site or model."""
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))  # the fake SDK helpers of the engine tests
from test_engine import agent_query, run, types, wait, write_flow

from agent_flow_studio import engine
from agent_flow_studio.connections import actions, store
from agent_flow_studio.connections.model import Connection
from agent_flow_studio.editor.runner import Runner

SITE = {"type": "website", "base_url": "https://tickets.example.com",
        "auth": {"method": "browser_session"},
        "actions": {"count_users": {"description": "Counts.", "params": {"page": {"required": False}},
                                    "steps": [{"goto": "/users?p={page}"},
                                              {"extract": {"users_range": "span.total"}}]},
                    "add_note": {"description": "Writes.", "access": "write",
                                 "steps": [{"goto": "/x"}]}},
        "access": "write"}


def flow(**node) -> dict:
    counter = {"description": "Counts the users.",
               "action": {"connector": "site", "name": "count_users",
                          "params": {"page": "{input.page}"},
                          "outputs": {"count": {"from": "users_range", "match": r"(\d+)\D*$"}}},
               "outputs": {"count": {"type": "integer"}}}
    counter.update(node)
    return {
        "version": 1, "name": "users-check", "title": "Users check", "execution": "graph",
        "inputs": {"page": {"type": "text", "required": False, "default": "1"}},
        "start": "counter", "agents": {"counter": counter},
        "gates": {"compare": {"kind": "decision", "label": "New users?"},
                  "first": {"kind": "stop", "label": "First", "message": "{counter.count} users."},
                  "more": {"kind": "stop", "label": "More",
                           "message": "{counter.count - previous.counter.count} new users."},
                  "same": {"kind": "stop", "label": "Same", "message": "No new users."}},
        "edges": [{"from": "counter", "to": "compare"},
                  {"from": "compare", "to": "first", "if": "not exists(previous.counter.count)"},
                  {"from": "compare", "to": "more",
                   "if": "counter.count > previous.counter.count"},
                  {"from": "compare", "to": "same"}],
    }


@pytest.fixture
def connectors(tmp_path, monkeypatch):
    monkeypatch.setenv("CONNECTIONS_FILE", str(tmp_path / "connections.yaml"))
    monkeypatch.setenv("AFS_PUBLIC_CONNECTIONS_FILE", str(tmp_path / "connections.public.yaml"))
    store.save({"site": Connection.model_validate(SITE)})


def fake_actions(monkeypatch, results):
    calls = []

    async def fake_run(name, action, params=None, conns=None, *, visible=False):
        calls.append((name, action, params))
        r = results.pop(0)
        if isinstance(r, Exception):
            raise r
        return {"action": action, "values": r}

    monkeypatch.setattr(actions, "run", fake_run)
    return calls


def start(rn, rid, **kw):
    async def go():
        r = rn.start("users-check", run_id=rid, **kw)
        await wait(r)
        return r
    return run(go())


def test_an_action_node_runs_without_a_model(tmp_path, connectors, monkeypatch):
    calls = fake_actions(monkeypatch, [{"users_range": "1–14 de 14"},
                                       {"users_range": "1–10 of 16"},
                                       {"users_range": "1–16 de 16"}])
    q = agent_query()
    flows = tmp_path / "flows"
    write_flow(flows, flow())
    rn = Runner(flows, q)
    r1 = start(rn, "u1", inputs={"page": "2"})
    assert r1.status == "done", r1.events[-1]
    assert r1.gate["message"] == "14 users."
    assert calls[0] == ("site", "count_users", {"page": "2"})  # the {input.page} part filled in
    assert q.log == []  # no model session at all
    tool = types(r1, "tool")[0]
    assert tool["tool"] == "run_action" and "site · count_users · page=2" in tool["detail"]
    assert types(r1, "outputs")[0]["names"] == ["count"]
    assert start(rn, "u2").gate["message"] == "2 new users."  # "1–10 of 16": the last number
    assert start(rn, "u3").gate["message"] == "No new users."
    audit = engine.settings.runs_dir / "audit.jsonl"  # the tests' runs folder (conftest)
    lines = [json.loads(x) for x in audit.read_text("utf-8").splitlines()]
    node = [x for x in lines if x.get("tool") == "action_node"]
    assert node[0]["input"] == {"connection": "site", "action": "count_users", "params": ["page"]}
    assert "2" not in json.dumps(node[0]["input"]["params"])  # names only, never the values


def test_action_node_failures(tmp_path, connectors, monkeypatch):
    fake_actions(monkeypatch, [actions.ActionError("site.count_users: step 2 (extract …) failed"),
                               {"users_range": "no number here"},
                               {"other": "1"}])
    flows = tmp_path / "flows"
    write_flow(flows, flow())
    rn = Runner(flows, agent_query())
    r = start(rn, "f1")
    assert r.status == "error" and "step 2" in r.events[-1]["message"]
    err = types(r, "tool_error")[0]
    assert err["tool"] == "run_action" and "step 2" in err["error"]
    r = start(rn, "f2")  # the match finds nothing → the required output has no value
    assert r.status == "error" and "no value for: count" in r.events[-1]["message"]
    r = start(rn, "f3")
    assert r.status == "error" and "no value for: count" in r.events[-1]["message"]


def test_a_value_of_the_wrong_type_fails_the_node(tmp_path, connectors, monkeypatch):
    fake_actions(monkeypatch, [{"users_range": "fourteen"}])
    f = flow(action={"connector": "site", "name": "count_users",
                     "outputs": {"count": {"from": "users_range"}}})
    flows = tmp_path / "flows"
    write_flow(flows, f)
    r = start(Runner(flows, agent_query()), "w1")
    assert r.status == "error" and "output count" in r.events[-1]["message"]


@pytest.mark.parametrize("patch, code", [
    (lambda f: f.update(execution="orchestrator"), "spec.action_needs_graph"),
    (lambda f: f["agents"]["counter"].update(prompt="Do it."), "spec.action_no_prompt"),
    (lambda f: f["agents"]["counter"]["action"]["outputs"].update(nope={}),
     "spec.action_output_unknown"),
    (lambda f: f["agents"]["counter"]["action"]["params"].update(page="{nobody.x}"),
     "spec.action_param_reason"),
    (lambda f: f["agents"]["counter"]["action"]["outputs"]["count"].update(match="("),
     "spec.action_match"),
    (lambda f: f["agents"]["counter"]["action"].update(name="nope"), "compile.action_unknown"),
    (lambda f: f["agents"]["counter"]["action"].update(name="add_note", params={}),
     "compile.action_write"),
    (lambda f: f["agents"]["counter"]["action"]["params"].update(size="3"),
     "compile.action_params"),
    (lambda f: f["agents"]["counter"]["action"].update(connector="elsewhere"),
     "compile.connector_missing"),
])
def test_rules(connectors, patch, code):
    from agent_flow_studio.editor.validation import _check

    f = flow()
    patch(f)
    errors = _check(f, {})["errors"]
    codes = {e.get("code") for e in errors}
    assert code in codes or any(code.split(".")[-1] in str(e) for e in errors), errors

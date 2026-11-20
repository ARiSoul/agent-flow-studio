"""Flow inputs with show_if, and the ticket-triage template (text, URL or number → one text)."""
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from agent_flow_studio.builder import compile_flow
from agent_flow_studio.builder.spec import FlowSpec
from agent_flow_studio.builder.templates import render
from agent_flow_studio.connections import store
from agent_flow_studio.connections.model import Connection
from agent_flow_studio.editor.runner import RunError, prepare_inputs
from agent_flow_studio.editor.server import create_app

BASE = {"name": "f", "title": "F", "start": "a", "task": "Do it.",
        "agents": {"a": {"description": "d", "prompt": "p"}}}
INPUTS = {"source": {"type": "choice", "options": ["text", "number"]},
          "text": {"type": "markdown", "show_if": 'input.source == "text"'},
          "number": {"type": "text", "show_if": 'input.source == "number"', "pattern": "INC[0-9]+"}}


@pytest.mark.parametrize("show_if,needle", [
    ("input.later == 1", "declared before it"),
    ("a.x == 1", "declared before it"),
    ("input.source ==", "show_if"),
])
def test_show_if_rules(show_if, needle):
    inputs = {"source": {"type": "text"}, "x": {"type": "text", "show_if": show_if},
              "later": {"type": "text"}}
    with pytest.raises(ValidationError, match=needle):
        FlowSpec.model_validate(BASE | {"inputs": inputs})


def test_show_if_only_on_flow_inputs():
    flow = BASE | {"agents": {"a": {"description": "d", "prompt": "p",
                                    "outputs": {"o": {"type": "text", "show_if": "input.x == 1"}}}}}
    with pytest.raises(ValidationError, match="only for flow inputs"):
        FlowSpec.model_validate(flow)


def test_hidden_inputs_are_not_required_nor_kept(tmp_path):
    spec = FlowSpec.model_validate(BASE | {"inputs": INPUTS})
    v = prepare_inputs(spec, "r1", {"source": "number", "number": "INC12", "text": "ignored"})
    assert v == {"source": "number", "number": "INC12"}
    with pytest.raises(RunError) as e:  # the shown one is required and checked
        prepare_inputs(spec, "r2", {"source": "number", "number": "bad"})
    assert set(e.value.detail["inputs"]) == {"number"}  # the hidden text is not required
    assert prepare_inputs(spec, "r3", {"source": "text", "text": "Printer down"}) == \
        {"source": "text", "text": "Printer down"}


def test_visible_endpoint(tmp_path):
    c = TestClient(create_app(tmp_path / "flows", allowed_hosts=["testserver"]))
    r = c.post("/api/inputs/visible", json={"inputs": INPUTS, "values": {"source": "text"}})
    assert r.json() == {"hidden": ["number"]}
    r = c.post("/api/inputs/visible", json={"inputs": INPUTS, "values": {}})
    assert r.json() == {"hidden": ["text", "number"]}


def test_ticket_triage_template(tmp_path, monkeypatch):
    spec, prompts = render("ticket-triage", "triage", "Triage",
                           {"connector": "tickets", "ticket_action": "open_ticket"})
    assert spec.execution == "graph" and spec.inputs["url"].show_if == 'input.source == "url"'
    assert spec.agents["fetcher"].connections == ["tickets"]
    assert "`open_ticket`" in prompts["prompts/fetcher.md"]
    flow_dir = tmp_path / "triage"
    for k, v in prompts.items():
        (flow_dir / k).parent.mkdir(parents=True, exist_ok=True)
        (flow_dir / k).write_text(v, encoding="utf-8")
    monkeypatch.setenv("CONNECTIONS_FILE", str(tmp_path / "connections.yaml"))
    store.save({"tickets": Connection.model_validate({
        "type": "http_api", "base_url": "https://tickets.example.com", "auth": {"method": "none"},
        "actions": {"open_ticket": {"description": "Reads a ticket.", "path": "/t/{number}",
                                    "params": {"number": {}}}}})})
    cf = compile_flow(spec, flow_dir)
    assert "mcp__connections__run_action" in cf.agent_tools["fetcher"]
    assert "mcp__connections__run_action" not in cf.agent_tools["triage"]
    assert "`open_ticket`(number: text)" in cf.options.agents["fetcher"].prompt


def test_template_is_listed():
    from agent_flow_studio.builder.templates import templates_dict
    t = next(x for x in templates_dict() if x["id"] == "ticket-triage")
    assert t["params"]["connector"]["kind"] == "connector"
    assert Path  # (keeps the import used when the test above is skipped)

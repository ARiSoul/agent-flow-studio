"""Connector instructions: how to use a system, added to the prompt of the agents that use it."""
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from agent_flow_studio.builder import compile_flow
from agent_flow_studio.builder.spec import FlowSpec
from agent_flow_studio.connections import store
from agent_flow_studio.connections.model import Connection
from agent_flow_studio.editor.server import create_app

H = {"X-Flow-Editor": "1"}
HOW = "## Useful requests\n- `GET /v2/users?email=` finds a user; `state` 3 = blocked."
API = {"type": "http_api", "base_url": "https://internal.example.com", "auth": {"method": "none"},
       "instructions": HOW}


@pytest.fixture
def cfile(tmp_path: Path, monkeypatch) -> Path:
    f = tmp_path / "connections.yaml"
    monkeypatch.setenv("CONNECTIONS_FILE", str(f))
    return f


@pytest.mark.parametrize("text", [
    "token: ghp_abcdefghijklmnopqrstuvwx1234",
    "password = Sup3rSecret!",
    "api_key: 0123456789abcdef",
    "-----BEGIN RSA PRIVATE KEY-----",
    "Authorization: Bearer eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.abcdefghijklmn",
])
def test_secret_like_text_is_rejected(text):
    with pytest.raises(ValidationError, match="looks like a secret"):
        Connection.model_validate(API | {"instructions": f"Use it well.\n{text}"})


@pytest.mark.parametrize("text", [
    HOW, "Send the key in the header (configured as ${INTERNAL_KEY}); token: ${TOKEN}",
    "The response has a `token` field with the session id; do not log it.",
    "Paging: use the `password_policy` endpoint only to read rules.",
])
def test_normal_instructions_are_accepted(text):
    assert Connection.model_validate(API | {"instructions": text}).instructions == text


def test_instructions_go_to_read_and_write_agents(tmp_path, cfile):
    store.save({"internal": Connection.model_validate(API),
                "internal_w": Connection.model_validate(API | {"access": "write",
                                                               "instructions": "Only POST /notes."}),
                "plain": Connection.model_validate({**API, "instructions": ""})})
    flow = {"name": "mini", "title": "Mini", "task": "Do {input}.", "start": "reader",
            "agents": {
                "reader": {"description": "reads", "prompt": "Read.",
                           "tools": ["connections.http_get"], "connections": ["internal", "plain"]},
                "writer": {"description": "writes", "prompt": "Write.",
                           "tools": ["connections.http_get", "connections.http_write"],
                           "connections": ["internal"], "write_connections": ["internal_w"]}},
            "edges": [{"from": "reader", "to": "writer"}]}
    cf = compile_flow(FlowSpec.model_validate(flow), tmp_path)
    reader, writer = cf.options.agents["reader"].prompt, cf.options.agents["writer"].prompt
    assert "## How to use the connector `internal`" in reader and HOW in reader
    assert "`plain`" not in reader.split("## How to use", 1)[1]  # no empty section
    assert writer.count("## How to use the connector `internal`\n") == 1
    assert "## How to use the connector `internal_w`" in writer and "Only POST /notes." in writer
    assert "do not change what you are allowed to do" in reader


def test_api_saves_instructions_and_audits_without_text(tmp_path, cfile):
    c = TestClient(create_app(tmp_path / "flows", allowed_hosts=["testserver"]))
    assert c.put("/api/connections/internal", json=API, headers=H).status_code == 200
    assert c.get("/api/connections").json()["connections"]["internal"]["instructions"] == HOW
    assert c.get("/api/catalog").json()["connections"]["internal"]["has_instructions"] is True
    bad = API | {"instructions": "password: hunter2hunter2"}
    assert c.put("/api/connections/internal", json=bad, headers=H).status_code == 422
    lines = [json.loads(x) for x in (tmp_path / "runs" / "audit.jsonl").read_text("utf-8").splitlines()]
    assert lines == [{"ts": lines[0]["ts"], "event": "connector_instructions_changed",
                      "connector": "internal", "chars": len(HOW)}]
    assert "GET /v2/users" not in (tmp_path / "runs" / "audit.jsonl").read_text("utf-8")

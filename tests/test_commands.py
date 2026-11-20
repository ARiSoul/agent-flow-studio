"""Saved assistant commands: store, API and the instructions sent with a message."""
import shutil
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from agent_flow_studio.editor import commands
from agent_flow_studio.editor.assistant import with_command
from agent_flow_studio.editor.server import create_app

PIPELINE = Path(__file__).parent / "fixtures" / "flows" / "pipeline"
H = {"X-Flow-Editor": "1"}


@pytest.fixture
def client(tmp_path: Path) -> TestClient:
    shutil.copytree(PIPELINE, tmp_path / "flows" / "pipeline")
    return TestClient(create_app(tmp_path / "flows", allowed_hosts=["testserver"]))


def test_store_create_edit_delete(tmp_path):
    assert commands.load() == []
    c = commands.save({"name": " New agent ", "text": "Create an agent in the library",
                       "instructions": "Generic only."})
    assert c.name == "New agent" and commands.COMMAND_ID.fullmatch(c.id)
    assert (tmp_path / "assistant_commands.yaml").is_file()
    c2 = commands.save(c.model_dump() | {"favorite": True, "text": "Changed"})
    assert c2.id == c.id and [x.text for x in commands.load()] == ["Changed"]
    assert commands.get(c.id).favorite is True
    commands.delete(c.id)
    assert commands.load() == []
    with pytest.raises(commands.CommandError):
        commands.delete(c.id)
    with pytest.raises(commands.CommandError):
        commands.save({"name": "x", "text": ""})
    with pytest.raises(commands.CommandError):
        commands.save({"name": "two\nlines", "text": "t"})


def test_instructions_go_with_the_message():
    c = commands.Command(id="abc123", name="New agent", text="t", instructions="  Rule A.  ")
    p = with_command("[Flow open in the editor: x]", "Do it", c)
    assert p.index("Rule A.") < p.index("Do it") and "Saved command 'New agent'" in p
    assert with_command("[h]", "Do it", None) == "[h]\n\nDo it"
    assert with_command("[h]", "Do it", c.model_copy(update={"instructions": " "})) == "[h]\n\nDo it"


def test_api(client: TestClient, monkeypatch):
    body = {"name": "Triage", "text": "Create a triage agent", "instructions": "Use markdown."}
    assert client.put("/api/commands/new", json=body).status_code == 403  # no header
    c = client.put("/api/commands/new", json=body, headers=H).json()
    assert client.get("/api/commands").json() == [c]
    r = client.put(f"/api/commands/{c['id']}", json=c | {"favorite": True}, headers=H)
    assert r.status_code == 200 and r.json()["favorite"] is True
    assert client.put("/api/commands/BAD!", json=body, headers=H).status_code == 400
    assert client.put("/api/commands/new", json={"name": "x"}, headers=H).status_code == 422

    seen = {}

    async def fake_chat(message, flow, spec, prompts, session_id=None, lang=None, command=None):
        seen["command"] = command
        yield {"type": "done"}

    monkeypatch.setattr("agent_flow_studio.editor.assistant.chat", fake_chat)
    spec = {"name": "pipeline"}
    r = client.post("/api/assist", headers=H, json={"flow": "pipeline", "message": "hi",
                                                    "spec": spec, "command": c["id"]})
    assert r.status_code == 200 and seen["command"].instructions == "Use markdown."
    r = client.post("/api/assist", headers=H, json={"flow": "pipeline", "message": "hi",
                                                    "spec": spec, "command": "aaaaaa"})
    assert r.status_code == 404

    assert client.delete(f"/api/commands/{c['id']}", headers=H).status_code == 200
    assert client.get("/api/commands").json() == []

"""Agent library: core agents, save to the local library, origin status of copies in a flow."""
import shutil
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient

from agent_flow_studio.builder import library
from agent_flow_studio.builder.spec import AgentSpec, FlowSpec
from agent_flow_studio.editor.server import create_app
from agent_flow_studio.editor.yaml_io import dump_flow

PIPELINE = Path(__file__).parent / "fixtures" / "flows" / "pipeline"
H = {"X-Flow-Editor": "1"}


@pytest.fixture
def client(tmp_path: Path) -> TestClient:
    shutil.copytree(PIPELINE, tmp_path / "flows" / "pipeline")
    return TestClient(create_app(tmp_path / "flows", allowed_hosts=["testserver"]))


def _flow_with(agent_name: str, agent: dict) -> dict:
    raw = yaml.safe_load((PIPELINE / "flow.yaml").read_text(encoding="utf-8"))
    raw["agents"][agent_name] = agent
    raw["edges"] = [*raw.get("edges", []), {"from": raw["start"], "to": agent_name}]
    return raw


def test_core_agents_are_valid_and_generic():
    agents = library.list_agents()
    assert {"repo_explorer", "code_reviewer", "summarizer"} <= set(agents)
    for a in agents.values():
        assert a.source == "core"
        assert a.spec.autonomy != "auto"
        assert all(i.from_ is None for i in a.spec.inputs.values())
        assert a.prompt.strip()


def test_fingerprint_ignores_flow_specific_fields():
    a = library.list_agents()["summarizer"]
    data = library.agent_data(a)
    copy = AgentSpec.model_validate(data | {
        "icon": "chat", "join": "any", "max_runs": 5,
        "origin": {"agent": "summarizer", "hash": a.hash},
        "inputs": {**data["inputs"], "text": {**data["inputs"]["text"], "from": "input.x"}}})
    assert library.fingerprint(copy, a.prompt + "\n\n") == a.hash
    tuned = AgentSpec.model_validate(data | {"model": "haiku"})
    assert library.fingerprint(tuned, a.prompt) != a.hash
    assert library.fingerprint(copy, "Another prompt.") != a.hash


def test_save_local_cleans_flow_fields(tmp_path):
    raw = {"description": "Does X.", "prompt_file": "prompts/x.md", "tools": [],
           "autonomy": "auto", "join": "any", "origin": {"agent": "summarizer", "hash": "ab" * 8},
           "inputs": {"text": {"from": "input.topic", "type": "text"}}}
    a, notes = library.save_local("my_agent", "My agent", raw, "Do X.\r\n")
    assert a.source == "local" and a.folder == tmp_path / "agents" / "my_agent"
    assert notes and "auto" in notes[0]
    saved = yaml.safe_load((a.folder / "agent.yaml").read_text(encoding="utf-8"))
    assert saved["title"] == "My agent"
    assert not {"autonomy", "join", "origin", "prompt", "prompt_file"} & set(saved)
    assert saved["inputs"] == {"text": {"type": "text"}}
    assert (a.folder / "prompt.md").read_text(encoding="utf-8") == "Do X.\n"


def test_save_local_refuses_core_ids_and_overwrite():
    with pytest.raises(library.LibraryError, match="core"):
        library.save_local("summarizer", "", {"description": "x"}, "p")
    library.save_local("mine", "", {"description": "x"}, "p")
    with pytest.raises(library.LibraryError, match="already exists"):
        library.save_local("mine", "", {"description": "x"}, "p")
    a, _ = library.save_local("mine", "", {"description": "y"}, "p", overwrite=True)
    assert a.spec.description == "y"
    with pytest.raises(library.LibraryError, match="invalid"):
        library.save_local("Bad-Id", "", {"description": "x"}, "p")


def test_library_rejects_auto_and_from(tmp_path):
    d = tmp_path / "agents" / "bad"
    d.mkdir(parents=True)
    (d / "prompt.md").write_text("p", encoding="utf-8")
    (d / "agent.yaml").write_text("description: x\nautonomy: auto\n", encoding="utf-8")
    with pytest.raises(library.LibraryError, match="auto"):
        library.list_agents()
    (d / "agent.yaml").write_text("description: x\ninputs: {a: {from: input.a}}\n", encoding="utf-8")
    with pytest.raises(library.LibraryError, match="from"):
        library.list_agents()


def test_origin_in_flow_roundtrip_and_status():
    a = library.list_agents()["summarizer"]
    agent = library.agent_data(a) | {"origin": {"agent": "summarizer", "hash": a.hash}}
    raw = _flow_with("summary", agent)
    spec = FlowSpec.model_validate(raw)
    assert spec.agents["summary"].origin.agent == "summarizer"
    assert "origin: {agent: summarizer" in dump_flow(spec)

    st = library.origin_status(raw, {})["summary"]
    assert st == {"agent": "summarizer", "title": "Summarizer", "source": "core", "missing": False,
                  "changed": False, "update": False}
    raw["agents"]["summary"]["model"] = "haiku"
    assert library.origin_status(raw, {})["summary"]["changed"] is True
    raw["agents"]["summary"]["origin"]["hash"] = "0" * 16
    assert library.origin_status(raw, {})["summary"]["update"] is True
    raw["agents"]["summary"]["origin"]["agent"] = "gone"
    assert library.origin_status(raw, {})["summary"]["missing"] is True


def test_origin_status_reads_prompt_files():
    a = library.list_agents()["summarizer"]
    agent = {k: v for k, v in library.agent_data(a).items() if k != "prompt"} | {
        "prompt_file": "prompts/summary.md", "origin": {"agent": "summarizer", "hash": a.hash}}
    raw = _flow_with("summary", agent)
    assert library.origin_status(raw, {"prompts/summary.md": a.prompt})["summary"]["changed"] is False
    assert library.origin_status(raw, {"prompts/summary.md": "x"})["summary"]["changed"] is True


def test_api_list_save_and_validate(client: TestClient):
    lib = client.get("/api/agents").json()
    s = next(x for x in lib if x["id"] == "summarizer")
    assert s["agent"]["prompt"] and s["hash"]

    body = {"id": "notes", "title": "Notes", "agent": {"description": "Takes notes."},
            "prompt": "Write notes."}
    assert client.post("/api/agents", json=body).status_code == 403  # no X-Flow-Editor header
    r = client.post("/api/agents", json=body, headers=H)
    assert r.status_code == 201 and r.json()["source"] == "local"
    assert client.post("/api/agents", json=body, headers=H).status_code == 409
    assert client.post("/api/agents", json=body | {"overwrite": True}, headers=H).status_code == 201
    assert any(x["id"] == "notes" for x in client.get("/api/agents").json())

    raw = _flow_with("summary", s["agent"] | {"origin": {"agent": "summarizer", "hash": s["hash"]}})
    v = client.post("/api/flows/pipeline/validate", json={"spec": raw, "prompts": {}}).json()
    assert v["origins"]["summary"]["changed"] is False

"""Where things live: local (this machine), an extension (its own place, e.g. a private repo) or
public (git). Flows, library agents and connectors can live in an extension."""
import shutil
import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from agent_flow_studio import extensions, publish
from agent_flow_studio.builder import compile as compile_mod
from agent_flow_studio.builder import library
from agent_flow_studio.connections import store
from agent_flow_studio.connections.model import Connection
from agent_flow_studio.editor.server import create_app

PIPELINE = Path(__file__).parent / "fixtures" / "flows" / "pipeline"
H = {"X-Flow-Editor": "1"}
API = {"type": "http_api", "base_url": "https://api.example.com", "auth": {"method": "none"}}


@pytest.fixture
def ext(tmp_path, monkeypatch) -> Path:
    """A loaded extension "acme" in tmp/extensions/acme (no code needed for these folders)."""
    root = tmp_path / "extensions"
    (root / "acme").mkdir(parents=True)
    monkeypatch.setenv("AFS_EXTENSIONS_DIR", str(root))
    monkeypatch.setattr(extensions.LOADED, "names", ["acme"])
    monkeypatch.setenv("CONNECTIONS_FILE", str(tmp_path / "connections.yaml"))
    return root / "acme"


@pytest.fixture
def client(tmp_path) -> TestClient:
    shutil.copytree(PIPELINE, tmp_path / "flows" / "pipeline")
    return TestClient(create_app(tmp_path / "flows", allowed_hosts=["testserver"]))


def test_flows_in_an_extension(client, ext, tmp_path):
    r = client.post("/api/flows", json={"name": "acme-flow", "title": "Acme", "location": "acme"},
                    headers=H)
    assert r.status_code == 201 and (ext / "flows" / "acme-flow" / "flow.yaml").is_file()
    assert not (tmp_path / "flows" / "acme-flow").exists()
    flows = {f["name"]: f for f in client.get("/api/flows").json()}
    assert flows["acme-flow"]["location"] == "acme" and flows["acme-flow"]["public"] is False
    assert flows["pipeline"]["location"] == "local"
    assert client.get("/api/flows/acme-flow").json()["location"] == "acme"
    assert compile_mod.find_flow("acme-flow", tmp_path / "flows")[0] == "acme"
    # names are unique across locations; unknown locations are refused
    assert client.post("/api/flows", json={"name": "acme-flow", "title": "x"}, headers=H).status_code == 409
    assert client.post("/api/flows", json={"name": "x2", "title": "x", "location": "nope"},
                       headers=H).status_code == 400
    r = client.delete("/api/flows/acme-flow", headers=H)
    assert r.status_code == 200 and (ext / "flows" / ".trash").is_dir()


def test_library_agents_in_an_extension(ext):
    a, _ = library.save_local("acme_agent", "Acme", {"description": "d"}, "p", location="acme")
    assert a.source == "acme" and (ext / "agents" / "acme_agent" / "agent.yaml").is_file()
    assert library.list_agents()["acme_agent"].source == "acme"
    with pytest.raises(library.LibraryError, match="acme library agent"):  # another place's id
        library.save_local("acme_agent", "", {"description": "d"}, "p")
    with pytest.raises(library.LibraryError, match="unknown location"):
        library.save_local("x", "", {"description": "d"}, "p", location="nope")


def test_connectors_in_an_extension_and_moving_them(ext, tmp_path):
    store.save({"mine": Connection.model_validate(API)})
    assert store.load() and store.location("mine") == "local"
    conns = store.load()
    conns["team_api"] = Connection.model_validate(API | {"description": "for the team"})
    store.save(conns, moves={"team_api": "acme"})
    assert "team_api" in (ext / "connections.yaml").read_text(encoding="utf-8")
    assert "team_api" not in (tmp_path / "connections.yaml").read_text(encoding="utf-8")
    assert set(store.load()) == {"mine", "team_api"} and store.location("team_api") == "acme"
    store.save(store.load(), moves={"team_api": "local"})  # back to this machine only
    assert "team_api" not in (ext / "connections.yaml").read_text(encoding="utf-8")
    (ext / "connections.yaml").write_text("connections:\n  mine:\n    type: http_api\n"
                                          "    base_url: https://x.example.com\n", encoding="utf-8")
    with pytest.raises(store.ConnectorConfigError, match="exists twice"):
        store.load()


def test_connector_location_through_the_api(client, ext):
    assert client.get("/api/connections").json()["locations"] == ["local", "acme", "public"]
    r = client.put("/api/connections/team_api", json=API | {"location": "acme"}, headers=H)
    assert r.status_code == 200
    assert client.get("/api/connections").json()["connections"]["team_api"]["location"] == "acme"
    assert client.put("/api/connections/x", json=API | {"location": "nope"}, headers=H).status_code == 400


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


def test_public_flows_and_private_terms(tmp_path, monkeypatch):
    if shutil.which("git") is None:
        pytest.skip("git is not installed")
    repo = tmp_path / "repo"
    shutil.copytree(PIPELINE, repo / "flows" / "pipeline")
    shutil.copytree(PIPELINE, repo / "flows" / "secretish")
    (repo / ".gitignore").write_text("flows/*\n!flows/pipeline/\n", encoding="utf-8")
    _git(repo, "init", "-q")
    monkeypatch.chdir(repo)
    (repo / "private_terms.txt").write_text("# company words\nAcmeCorp\n", encoding="utf-8")
    c = TestClient(create_app(repo / "flows", allowed_hosts=["testserver"]))
    flows = {f["name"]: f for f in c.get("/api/flows").json()}
    assert flows["pipeline"]["public"] is True and flows["secretish"]["public"] is False
    assert publish.private_terms() == ["AcmeCorp"]

    doc = c.get("/api/flows/pipeline").json()
    spec = {**doc["spec"], "description": "Reads the AcmeCorp tickets"}
    r = c.put("/api/flows/pipeline", json={"spec": spec, "prompts": {}}, headers=H)
    assert r.status_code == 409 and r.json()["private_terms"] == ["AcmeCorp"]
    assert "AcmeCorp" not in (repo / "flows" / "pipeline" / "flow.yaml").read_text(encoding="utf-8")
    # a local (ignored) flow can name them freely
    s2 = {**c.get("/api/flows/secretish").json()["spec"], "name": "secretish",
          "description": "AcmeCorp"}
    assert c.put("/api/flows/secretish", json={"spec": s2, "prompts": {}}, headers=H).status_code == 200


def _repo(tmp_path, monkeypatch) -> Path:
    if shutil.which("git") is None:
        pytest.skip("git is not installed")
    repo = tmp_path / "repo"
    shutil.copytree(PIPELINE, repo / "flows" / "pipeline")
    (repo / ".gitignore").write_text("flows/*\n!flows/example/\n", encoding="utf-8")
    _git(repo, "init", "-q")
    monkeypatch.chdir(repo)
    (repo / "private_terms.txt").write_text("AcmeCorp\n", encoding="utf-8")
    return repo


def test_create_and_move_public_flows(tmp_path, monkeypatch):
    repo = _repo(tmp_path, monkeypatch)
    c = TestClient(create_app(repo / "flows", allowed_hosts=["testserver"]))
    assert c.post("/api/flows", json={"name": "open", "title": "Open", "location": "public"},
                  headers=H).status_code == 201
    gi = (repo / ".gitignore").read_text(encoding="utf-8").splitlines()
    assert gi == ["flows/*", "!flows/example/", "!flows/open/"]  # right after the flows/* rule
    assert c.get("/api/flows/open").json()["location"] == "public"
    # a public flow cannot be created with private words
    r = c.post("/api/flows", json={"name": "leak", "title": "AcmeCorp tickets", "location": "public"},
               headers=H)
    assert r.status_code == 409 and r.json()["detail"]["private_terms"] == ["AcmeCorp"]
    assert not (repo / "flows" / "leak").exists()
    # pipeline is local; moving it public is checked too, and back again removes the exception
    assert c.get("/api/flows/pipeline").json()["location"] == "local"
    assert c.post("/api/flows/pipeline/move", json={"location": "public"}, headers=H).json() == \
        {"location": "public"}
    assert "!flows/pipeline/" in (repo / ".gitignore").read_text(encoding="utf-8")
    assert c.post("/api/flows/pipeline/move", json={"location": "local"}, headers=H).json() == \
        {"location": "local"}
    assert "!flows/pipeline/" not in (repo / ".gitignore").read_text(encoding="utf-8")
    c.delete("/api/flows/open", headers=H)
    assert "!flows/open/" not in (repo / ".gitignore").read_text(encoding="utf-8")


def test_move_flows_to_and_from_an_extension(client, ext, tmp_path):
    r = client.post("/api/flows/pipeline/move", json={"location": "acme"}, headers=H)
    assert r.json() == {"location": "acme"} and (ext / "flows" / "pipeline" / "flow.yaml").is_file()
    assert not (tmp_path / "flows" / "pipeline").exists()
    assert client.post("/api/flows/pipeline/move", json={"location": "local"}, headers=H).json() \
        == {"location": "local"}
    assert client.post("/api/flows/pipeline/move", json={"location": "nope"},
                       headers=H).status_code == 400


def test_public_library_agents_and_the_library_window_api(tmp_path, monkeypatch):
    core = tmp_path / "core_agents"
    monkeypatch.setattr(library, "CORE_DIR", core)
    monkeypatch.chdir(tmp_path)
    (tmp_path / "private_terms.txt").write_text("AcmeCorp\n", encoding="utf-8")
    c = TestClient(create_app(tmp_path / "flows", allowed_hosts=["testserver"]))
    body = {"id": "helper", "title": "Helper", "prompt": "Help.",
            "agent_yaml": "description: Helps.\ntools: []\n", "location": "public"}
    assert c.post("/api/agents", json=body, headers=H).status_code == 201
    assert (core / "helper" / "agent.yaml").is_file()
    item = next(a for a in c.get("/api/agents").json() if a["id"] == "helper")
    assert item["location"] == "public" and "description: Helps." in item["agent_yaml"]
    r = c.post("/api/agents", json=body | {"id": "leaky", "prompt": "Use AcmeCorp data."}, headers=H)
    assert r.status_code == 409 and r.json()["detail"]["private_terms"] == ["AcmeCorp"]
    assert c.post("/api/agents/helper/move", json={"location": "local"}, headers=H).json() == \
        {"id": "helper", "source": "local"}
    assert not (core / "helper").exists()
    assert c.delete("/api/agents/helper", headers=H).json() == {"deleted": "helper", "source": "local"}
    assert c.delete("/api/agents/helper", headers=H).status_code == 404


def test_public_connectors(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("CONNECTIONS_FILE", str(tmp_path / "connections.yaml"))
    monkeypatch.setenv("AFS_PUBLIC_CONNECTIONS_FILE", str(tmp_path / "connections.public.yaml"))
    (tmp_path / "private_terms.txt").write_text("AcmeCorp\n", encoding="utf-8")
    c = TestClient(create_app(tmp_path / "flows", allowed_hosts=["testserver"]))
    r = c.put("/api/connections/forum", json=API | {"location": "public",
                                                    "description": "A public forum"}, headers=H)
    assert r.status_code == 200
    assert "forum" in (tmp_path / "connections.public.yaml").read_text(encoding="utf-8")
    assert c.get("/api/connections").json()["connections"]["forum"]["location"] == "public"
    r = c.put("/api/connections/forum", json=API | {"description": "AcmeCorp intranet"}, headers=H)
    assert r.status_code == 409 and r.json()["detail"]["private_terms"] == ["AcmeCorp"]

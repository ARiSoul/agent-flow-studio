"""Servidor do editor visual: leitura, validação, gravação e regras de segurança."""
import copy
import shutil
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient

from agent_flow_studio.builder import load_spec
from agent_flow_studio.builder.spec import FlowSpec
from agent_flow_studio.editor.server import create_app
from agent_flow_studio.editor.yaml_io import dump_flow
from agent_flow_studio.tools import artifacts

PIPELINE = Path(__file__).parent / "fixtures" / "flows" / "pipeline"

H = {"X-Flow-Editor": "1"}


@pytest.fixture
def base(tmp_path: Path) -> Path:
    shutil.copytree(PIPELINE, tmp_path / "pipeline")
    return tmp_path


@pytest.fixture
def client(base: Path) -> TestClient:
    return TestClient(create_app(base, allowed_hosts=["testserver"]))


def test_dump_roundtrip_ticket_flow():
    spec, _ = load_spec("pipeline")
    text = dump_flow(spec)
    assert text.startswith("# Flow: ")
    assert FlowSpec.model_validate(yaml.safe_load(text)) == spec


def test_catalog_and_list(client: TestClient):
    cat = client.get("/api/catalog").json()
    assert "artifacts.read_artifact" in {t["id"] for t in cat["tools"]}
    assert isinstance(cat["contracts"], dict)
    flows = client.get("/api/flows").json()
    assert flows == [{"name": "pipeline", "title": "Pipeline de exemplo (testes)",
                      "valid": True, "agents": 7, "gates": 6}]


def test_get_flow_returns_prompts_and_fluxo(client: TestClient):
    r = client.get("/api/flows/pipeline").json()
    assert r["ok"] and r["spec"]["start"] == "triage"
    assert "prompts/triage.md" in r["prompts"]
    assert "## Fluxo" in r["fluxo"]
    assert "mcp__artifacts__read_artifact" in r["allowed_tools"]


def test_validate_reports_errors_per_node(client: TestClient):
    spec = client.get("/api/flows/pipeline").json()["spec"]
    spec["agents"]["triage"]["tools"].append("fake.drop_table")
    r = client.post("/api/flows/pipeline/validate", json={"spec": spec}).json()
    assert not r["ok"]
    assert any("fake.drop_table" in e["msg"] and e["nodes"] == ["triage"] for e in r["errors"])


def test_unreachable_error_points_to_orphan(client: TestClient):
    spec = client.get("/api/flows/pipeline").json()["spec"]
    spec["agents"]["solto"] = {"description": "x", "prompt": "y"}
    r = client.post("/api/flows/pipeline/validate", json={"spec": spec}).json()
    assert [e["nodes"] for e in r["errors"]] == [["solto"]]


def test_validate_missing_prompt_file(client: TestClient):
    spec = client.get("/api/flows/pipeline").json()["spec"]
    spec["agents"]["identity"]["prompt_file"] = "prompts/nao_existe.md"
    r = client.post("/api/flows/pipeline/validate", json={"spec": spec}).json()
    assert not r["ok"] and r["errors"][0]["nodes"] == ["identity"]
    # o mesmo prompt enviado pelo editor torna o flow válido
    r = client.post("/api/flows/pipeline/validate",
                    json={"spec": spec, "prompts": {"prompts/nao_existe.md": "Olá"}}).json()
    assert r["ok"]


def test_save_writes_yaml_prompts_and_backup(client: TestClient, base: Path):
    data = client.get("/api/flows/pipeline").json()
    spec = data["spec"]
    spec["layout"] = {"triage": {"x": 10.4, "y": 20.6}}
    spec["agents"]["triage"]["description"] = "Nova descrição."
    prompts = {"prompts/triage.md": data["prompts"]["prompts/triage.md"] + "\nExtra.\n"}
    r = client.put("/api/flows/pipeline", json={"spec": spec, "prompts": prompts}, headers=H)
    assert r.status_code == 200 and r.json()["saved"]
    saved, _ = load_spec("pipeline", base=base)
    assert saved.agents["triage"].description == "Nova descrição."
    assert saved.layout["triage"].x == 10
    assert (base / "pipeline/prompts/triage.md").read_text(encoding="utf-8").endswith("Extra.\n")
    assert (base / "pipeline/flow.yaml.bak").is_file()


def test_save_invalid_does_not_write(client: TestClient, base: Path):
    before = (base / "pipeline/flow.yaml").read_text(encoding="utf-8")
    spec = client.get("/api/flows/pipeline").json()["spec"]
    spec["start"] = "fantasma"
    r = client.put("/api/flows/pipeline", json={"spec": spec}, headers=H)
    assert r.status_code == 422 and not r.json()["ok"]
    assert (base / "pipeline/flow.yaml").read_text(encoding="utf-8") == before


def test_validation_does_not_leak_artifact_config(client: TestClient):
    before = (artifacts._names, artifacts._contracts)
    client.get("/api/flows/pipeline")
    assert (artifacts._names, artifacts._contracts) == before


@pytest.mark.parametrize("method,url,body", [
    ("put", "/api/flows/pipeline", {"spec": {}}),
    ("post", "/api/flows", {"name": "novo", "title": "Novo"}),
    ("delete", "/api/flows/pipeline", None),
])
def test_mutations_require_csrf_header(client: TestClient, method, url, body):
    r = client.request(method.upper(), url, json=body)
    assert r.status_code == 403


def test_unknown_host_rejected(base: Path):
    c = TestClient(create_app(base, allowed_hosts=["localhost"]), base_url="http://evil.example")
    assert c.get("/api/flows").status_code == 400


@pytest.mark.parametrize("prompts,status", [
    ({"../fora.md": "x"}, 400),
    ({"prompts/../../fora.md": "x"}, 400),
    ({"prompts/a.txt": "x"}, 400),
])
def test_prompt_paths_confined(client: TestClient, prompts, status):
    spec = client.get("/api/flows/pipeline").json()["spec"]
    r = client.put("/api/flows/pipeline", json={"spec": spec, "prompts": prompts}, headers=H)
    assert r.status_code == status


def test_rename_through_put_rejected(client: TestClient):
    spec = client.get("/api/flows/pipeline").json()["spec"]
    spec["name"] = "outro"
    assert client.put("/api/flows/pipeline", json={"spec": spec}, headers=H).status_code == 400


def test_bad_flow_names(client: TestClient):
    assert client.get("/api/flows/..%2F..%2Fetc").status_code in (400, 404)
    assert client.get("/api/flows/Mau").status_code == 400


def test_create_blank_duplicate_and_trash(client: TestClient, base: Path):
    assert client.post("/api/flows", json={"name": "novo", "title": "Novo"}, headers=H).status_code == 201
    assert client.get("/api/flows/novo").json()["ok"]
    r = client.post("/api/flows", json={"name": "copia", "title": "Cópia",
                                        "copy_from": "pipeline"}, headers=H)
    assert r.status_code == 201
    copia = client.get("/api/flows/copia").json()
    assert copia["ok"] and copia["spec"]["title"] == "Cópia" and copia["spec"]["name"] == "copia"
    assert client.post("/api/flows", json={"name": "novo", "title": "x"}, headers=H).status_code == 409
    r = client.delete("/api/flows/novo", headers=H)
    assert r.status_code == 200 and r.json()["trashed"].startswith(".trash")
    assert not (base / "novo").exists() and any((base / ".trash").iterdir())
    assert [f["name"] for f in client.get("/api/flows").json()] == ["copia", "pipeline"]


def test_secrets_never_expanded(client: TestClient, base: Path, monkeypatch):
    monkeypatch.setenv("EXT_TOKEN", "segredo-real")
    spec = client.get("/api/flows/pipeline").json()["spec"]
    spec = copy.deepcopy(spec)
    spec["mcp_servers"] = {"crm": {"type": "stdio", "command": "npx", "tools": ["list_items"],
                                   "env": {"TOKEN": "${EXT_TOKEN}"}, "approved": True,
                                   "read_only": True}}
    spec["agents"]["diagnostician"]["tools"].append("crm.list_items")
    r = client.put("/api/flows/pipeline", json={"spec": spec}, headers=H)
    assert r.status_code == 200
    text = (base / "pipeline/flow.yaml").read_text(encoding="utf-8")
    assert "${EXT_TOKEN}" in text and "segredo-real" not in text
    assert "segredo-real" not in r.text

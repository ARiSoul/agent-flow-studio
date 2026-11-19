"""Connectors: model, secrets, authentication, read-only GET, editor API and use in flows."""
import asyncio
import base64
import copy
import shutil
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from agent_flow_studio.builder import FlowError, compile_flow
from agent_flow_studio.builder.spec import FlowSpec
from agent_flow_studio.connections import client, store
from agent_flow_studio.connections.model import Connection
from agent_flow_studio.editor.server import create_app

PIPELINE = Path(__file__).parent / "fixtures" / "flows" / "pipeline"

H = {"X-Flow-Editor": "1"}
DEVOPS = {"type": "azure_devops", "organization": "contoso", "auth": {"method": "pat"},
          "secrets": {"token": "${DEVOPS_PAT}"}, "allowed_paths": ["/_apis/projects"]}


@pytest.fixture
def cfile(tmp_path: Path, monkeypatch) -> Path:
    f = tmp_path / "connections.yaml"
    monkeypatch.setenv("CONNECTIONS_FILE", str(f))
    return f


@pytest.fixture
def fake_keyring(monkeypatch):
    vault: dict = {}

    class K:
        set_password = staticmethod(lambda s, u, v: vault.__setitem__((s, u), v))
        get_password = staticmethod(lambda s, u: vault.get((s, u)))
        delete_password = staticmethod(lambda s, u: vault.pop((s, u)))

    monkeypatch.setattr(store, "_keyring", lambda: K)
    return vault


@pytest.fixture
def transport(monkeypatch):
    seen: list[httpx.Request] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(req)
        return httpx.Response(200, json={"count": 1, "value": [{"name": "Proj"}]})

    monkeypatch.setattr(client, "_transport", httpx.MockTransport(handler))
    return seen


@pytest.mark.parametrize("patch,needle", [
    ({"type": "github", "auth": {"method": "client_credentials", "client_id": "x"}}, "not supported"),
    ({"organization": None}, "organization"),
    ({"base_url": "http://insecure"}, "https://"),
    ({"secrets": {"token": "literal"}}, "keyring"),
    ({"allowed_paths": ["../x"]}, "invalid path"),
])
def test_connection_model_rules(patch, needle):
    with pytest.raises(ValueError, match=needle):
        Connection.model_validate({**DEVOPS, **patch})


def test_store_roundtrip_without_secrets(cfile: Path, fake_keyring, monkeypatch):
    c = Connection.model_validate({**DEVOPS, "secrets": {"token": "keyring"}})
    store.save({"devops": c})
    store.set_secret("devops", "token", "pat-super-secret")
    assert "pat-super-secret" not in cfile.read_text(encoding="utf-8")
    loaded = store.load()
    assert loaded["devops"] == c
    assert store.get_secret("devops", c, "token") == "pat-super-secret"
    assert store.secret_status("devops", c) == {"field": "token", "source": "keyring", "set": True,
                                                    "optional": False}


def test_devops_pat_get(cfile, transport, monkeypatch):
    monkeypatch.setenv("DEVOPS_PAT", "abc")
    conns = {"devops": Connection.model_validate(DEVOPS)}
    r = asyncio.run(client.get("devops", "/_apis/projects?api-version=7.1", conns=conns))
    assert r["status"] == 200 and "Proj" in r["body"]
    req = transport[0]
    assert req.method == "GET" and str(req.url).startswith("https://dev.azure.com/contoso/_apis/")
    assert req.headers["Authorization"] == "Basic " + base64.b64encode(b":abc").decode()


@pytest.mark.parametrize("path", ["/_apis/git/repos", "https://evil/x", "/_apis/projects/../x",
                                  "relative"])
def test_get_path_restrictions(transport, monkeypatch, path):
    monkeypatch.setenv("DEVOPS_PAT", "abc")
    conns = {"devops": Connection.model_validate(DEVOPS)}
    with pytest.raises(client.ConnectorError):
        asyncio.run(client.get("devops", path, conns=conns))
    assert not transport


def test_missing_secret_reported_without_request(transport, monkeypatch):
    monkeypatch.delenv("DEVOPS_PAT", raising=False)
    conns = {"devops": Connection.model_validate(DEVOPS)}
    r = asyncio.run(client.test("devops", conns))
    assert not r["ok"] and "secret" in r["detail"] and not transport


def test_client_credentials_token_then_get(transport, monkeypatch):
    monkeypatch.setenv("AZ_SECRET", "s3cr3t")
    calls = []

    def handler(req: httpx.Request) -> httpx.Response:
        calls.append(req)
        if "oauth2" in str(req.url):
            assert b"client_secret=s3cr3t" in req.content
            return httpx.Response(200, json={"access_token": "tok", "expires_in": 3600})
        assert req.headers["Authorization"] == "Bearer tok"
        return httpx.Response(200, json={"value": []})

    monkeypatch.setattr(client, "_transport", httpx.MockTransport(handler))
    client._tokens.clear()
    conn = Connection.model_validate({
        "type": "azure", "auth": {"method": "client_credentials", "tenant_id": "t1",
                                  "client_id": "c1"}, "secrets": {"client_secret": "${AZ_SECRET}"}})
    r = asyncio.run(client.test("az", {"az": conn}))
    assert r["ok"] and "login.microsoftonline.com/t1" in str(calls[0].url)
    assert "management.azure.com/.default" in calls[0].content.decode().replace("%2F", "/").replace(
        "%3A", ":")


# --- editor API ------------------------------------------------------------------------------
def test_editor_connections_api(tmp_path, cfile, fake_keyring, transport):
    shutil.copytree(PIPELINE, tmp_path / "flows/pipeline")
    c = TestClient(create_app(tmp_path / "flows", allowed_hosts=["testserver"]))
    body = {k: v for k, v in DEVOPS.items() if k != "secrets"}
    assert c.put("/api/connections/devops", json=body).status_code == 403
    assert c.put("/api/connections/devops", json=body, headers=H).json()["secret"]["set"] is False
    assert c.put("/api/connections/devops", json={**body, "type": "nope"}, headers=H).status_code == 422
    r = c.put("/api/connections/devops/secret", json={"value": "pat-xyz"}, headers=H)
    assert r.json()["secret"] == {"field": "token", "source": "keyring", "set": True,
                                  "optional": False}
    listing = c.get("/api/connections")
    assert "pat-xyz" not in listing.text and "pat-xyz" not in cfile.read_text(encoding="utf-8")
    assert listing.json()["connections"]["devops"]["secrets"] == {"token": "keyring"}
    # editing does not remove the secret reference
    c.put("/api/connections/devops", json={**body, "description": "org"}, headers=H)
    assert store.load()["devops"].secrets == {"token": "keyring"}
    t = c.post("/api/connections/devops/test", headers=H).json()
    assert t["ok"] and t["status"] == 200
    assert "devops" in c.get("/api/catalog").json()["connections"]
    assert c.delete("/api/connections/devops", headers=H).json() == {"deleted": "devops"}
    assert fake_keyring == {}
    assert c.put("/api/connections/Mau", json=body, headers=H).status_code == 400


# --- use in flows ----------------------------------------------------------------------------
FLOW = {
    "name": "mini", "title": "Mini", "task": "Do {input}.", "start": "a",
    "agents": {"a": {"description": "reads", "prompt": "Read.", "tools": ["connections.http_get"],
                     "connections": ["devops"]}},
}


def test_flow_with_connection_compiles_and_guards(tmp_path, cfile, monkeypatch):
    store.save({"devops": Connection.model_validate(DEVOPS)})
    cf = compile_flow(FlowSpec.model_validate(FLOW), tmp_path)
    assert "mcp__connections__http_get" in cf.agent_tools["a"]
    prompt = cf.options.agents["a"].prompt
    assert "## Connectors" in prompt and "`devops`: azure_devops" in prompt


def test_flow_with_unknown_connection_fails(tmp_path, cfile):
    with pytest.raises(FlowError, match="not configured"):
        compile_flow(FlowSpec.model_validate(FLOW), tmp_path)


@pytest.mark.parametrize("mutate,needle", [
    (lambda a: a.update(tools=[]), "does not have connections.http_get"),
    (lambda a: a.update(connections=[]), "no connector"),
])
def test_flow_connection_rules(mutate, needle):
    d = copy.deepcopy(FLOW)
    mutate(d["agents"]["a"])
    with pytest.raises(ValueError, match=needle):
        FlowSpec.model_validate(d)

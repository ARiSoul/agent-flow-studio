"""Connectors phase 2: sign-in on a page (OAuth 2 + PKCE), managed identity and the new types."""
import asyncio
import base64
import hashlib
import json
import shutil
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from fastapi.testclient import TestClient

from agent_flow_studio.connections import client, oauth, store
from agent_flow_studio.connections.model import OAUTH_PRESETS, Connection
from agent_flow_studio.editor.server import create_app

PIPELINE = Path(__file__).parent / "fixtures" / "flows" / "pipeline"
H = {"X-Flow-Editor": "1"}
GRAPH = {"type": "microsoft_graph", "auth": {"method": "oauth_browser", "client_id": "app1"}}
REDIRECT = "http://localhost:8765/api/oauth/callback"


@pytest.fixture
def cfile(tmp_path: Path, monkeypatch) -> Path:
    f = tmp_path / "connections.yaml"
    monkeypatch.setenv("CONNECTIONS_FILE", str(f))
    return f


@pytest.fixture
def vault(monkeypatch) -> dict:
    data: dict = {}

    class K:
        set_password = staticmethod(lambda s, u, v: data.__setitem__((s, u), v))
        get_password = staticmethod(lambda s, u: data.get((s, u)))
        delete_password = staticmethod(lambda s, u: data.pop((s, u)))

    monkeypatch.setattr(store, "_keyring", lambda: K)
    return data


@pytest.fixture(autouse=True)
def _clean():
    oauth._pending.clear()
    client._tokens.clear()
    yield
    oauth._pending.clear()


def mock(monkeypatch, handler) -> list[httpx.Request]:
    seen: list[httpx.Request] = []

    def h(req: httpx.Request) -> httpx.Response:
        seen.append(req)
        return handler(req)

    monkeypatch.setattr(client, "_transport", httpx.MockTransport(h))
    return seen


def form(req: httpx.Request) -> dict[str, str]:
    return {k: v[0] for k, v in parse_qs(req.content.decode()).items()}


def query(url: str) -> dict[str, str]:
    return {k: v[0] for k, v in parse_qs(urlsplit(url).query).items()}


# --- model -----------------------------------------------------------------------------------
@pytest.mark.parametrize("data,needle", [
    ({"type": "atlassian", "auth": {"method": "basic", "username": "a@b.c"}}, "organization"),
    ({"type": "microsoft_graph", "auth": {"method": "oauth_browser"}}, "client_id"),
    ({"type": "http_api", "base_url": "https://api.example.com",
      "auth": {"method": "oauth_browser", "client_id": "x"}}, "https:// authorize_url"),
    ({"type": "http_api", "base_url": "https://api.example.com",
      "auth": {"method": "oauth_browser", "client_id": "x", "authorize_url": "https://a/auth",
               "token_url": "http://a/token"}}, "https:// token_url"),
    ({**GRAPH, "auth": {**GRAPH["auth"], "authorize_params": {"state": "x"}}}, "reserved"),
    ({"type": "github", "auth": {"method": "pat", "token_in": "query"}}, "only for sign-in"),
    ({"type": "atlassian", "organization": "acme", "auth": {"method": "pat"}}, "not supported"),
])
def test_phase2_model_rules(data, needle):
    with pytest.raises(ValueError, match=needle):
        Connection.model_validate(data)


def test_types_resolve_their_defaults():
    g = Connection.model_validate(GRAPH)
    assert g.resolved_oauth() == (
        "https://login.microsoftonline.com/organizations/oauth2/v2.0/authorize",
        "https://login.microsoftonline.com/organizations/oauth2/v2.0/token",
        "https://graph.microsoft.com/User.Read offline_access")
    t = Connection.model_validate({**GRAPH, "auth": {**GRAPH["auth"], "tenant_id": "t1"}})
    assert "/t1/oauth2/" in t.resolved_oauth()[0]
    gl = Connection.model_validate({"type": "gitlab", "base_url": "https://git.example.com/api/v4",
                                    "auth": {"method": "oauth_browser", "client_id": "x"}})
    assert gl.resolved_oauth()[:2] == ("https://git.example.com/oauth/authorize",
                                       "https://git.example.com/oauth/token")
    at = Connection.model_validate({"type": "atlassian", "organization": "acme",
                                    "auth": {"method": "basic", "username": "a@b.c"}})
    assert at.resolved_base_url() == "https://acme.atlassian.net"


@pytest.mark.parametrize("preset", sorted(OAUTH_PRESETS))
def test_every_preset_is_a_valid_connector(preset):
    p = OAUTH_PRESETS[preset]
    c = Connection.model_validate({"type": "http_api", "base_url": p["base_url"],
                                   "test_path": p["test_path"],
                                   "auth": {"method": "oauth_browser", "client_id": "x",
                                            **p["auth"]}})
    assert c.resolved_oauth()[0].startswith("https://")


# --- sign-in ---------------------------------------------------------------------------------
def test_start_builds_a_pkce_url():
    c = Connection.model_validate({"type": "http_api", "base_url": "https://oauth.reddit.com",
                                   "auth": {"method": "oauth_browser", "client_id": "r1",
                                            **OAUTH_PRESETS["reddit"]["auth"]}})
    url = oauth.start("rd", c, REDIRECT)
    q = query(url)
    assert url.startswith("https://www.reddit.com/api/v1/authorize?")
    assert q["client_id"] == "r1" and q["redirect_uri"] == REDIRECT and q["response_type"] == "code"
    assert q["scope"] == "identity read" and q["duration"] == "permanent"
    assert q["code_challenge_method"] == "S256"
    verifier = oauth._pending[q["state"]].verifier
    expected = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=")
    assert q["code_challenge"] == expected.decode() and verifier not in url


def test_finish_stores_tokens_only_in_the_keyring(cfile, vault, monkeypatch):
    conn = Connection.model_validate({**GRAPH, "secrets": {"client_secret": "keyring"}})
    store.save({"graph": conn})
    store.set_secret("graph", "client_secret", "app-secret")

    def handler(req: httpx.Request) -> httpx.Response:
        f = form(req)
        assert f["grant_type"] == "authorization_code" and f["code"] == "c0de"
        assert f["redirect_uri"] == REDIRECT and f["client_secret"] == "app-secret"
        assert len(f["code_verifier"]) >= 43
        return httpx.Response(200, json={"access_token": "AT1", "refresh_token": "RT1",
                                         "expires_in": 3600})

    seen = mock(monkeypatch, handler)
    state = query(oauth.start("graph", conn, REDIRECT))["state"]
    assert asyncio.run(oauth.finish(state, "c0de", store.load())) == "graph"
    assert "login.microsoftonline.com/organizations/oauth2/v2.0/token" in str(seen[0].url)
    saved = json.loads(vault[(store.SERVICE, "graph/oauth_token")])
    assert saved["access_token"] == "AT1" and saved["refresh_token"] == "RT1"
    assert "AT1" not in cfile.read_text(encoding="utf-8")
    assert store.oauth_status("graph", conn)["signed_in"] is True
    with pytest.raises(oauth.OAuthError, match="unknown or expired"):  # state is single use
        asyncio.run(oauth.finish(state, "c0de", store.load()))


def test_token_auth_basic_and_provider_refusal(cfile, vault, monkeypatch):
    conn = Connection.model_validate({"type": "http_api", "base_url": "https://oauth.reddit.com",
                                      "auth": {"method": "oauth_browser", "client_id": "r1",
                                               **OAUTH_PRESETS["reddit"]["auth"]}})
    store.save({"rd": conn})

    def handler(req: httpx.Request) -> httpx.Response:
        assert req.headers["Authorization"] == "Basic " + base64.b64encode(b"r1:").decode()
        assert "client_secret" not in form(req)
        return httpx.Response(400, json={"error": "invalid_grant", "error_description": "bad code"})

    mock(monkeypatch, handler)
    state = query(oauth.start("rd", conn, REDIRECT))["state"]
    with pytest.raises(oauth.OAuthError, match="invalid_grant — bad code"):
        asyncio.run(oauth.finish(state, "x", store.load()))
    assert store.get_oauth_tokens("rd") is None


def test_meta_short_token_is_exchanged_for_a_long_one(cfile, vault, monkeypatch):
    p = OAUTH_PRESETS["facebook"]
    conn = Connection.model_validate({"type": "http_api", "base_url": p["base_url"],
                                      "auth": {"method": "oauth_browser", "client_id": "fb1",
                                               **p["auth"]},
                                      "secrets": {"client_secret": "keyring"}})
    store.save({"fb": conn})
    store.set_secret("fb", "client_secret", "fb-secret")

    def handler(req: httpx.Request) -> httpx.Response:
        if req.method == "POST":
            return httpx.Response(200, json={"access_token": "SHORT", "expires_in": 3600})
        q = dict(req.url.params)
        assert q["grant_type"] == "fb_exchange_token" and q["fb_exchange_token"] == "SHORT"
        assert q["client_secret"] == "fb-secret"
        return httpx.Response(200, json={"access_token": "LONG", "expires_in": 5_000_000})

    mock(monkeypatch, handler)
    state = query(oauth.start("fb", conn, REDIRECT))["state"]
    asyncio.run(oauth.finish(state, "c", store.load()))
    assert store.get_oauth_tokens("fb")["access_token"] == "LONG"


# --- use of the tokens -----------------------------------------------------------------------
def test_expired_token_is_refreshed_and_refresh_token_kept(cfile, vault, monkeypatch):
    conn = Connection.model_validate(GRAPH)
    store.set_oauth_tokens("graph", {"access_token": "OLD", "refresh_token": "RT1",
                                     "expires_at": 1.0})

    def handler(req: httpx.Request) -> httpx.Response:
        if "oauth2" in str(req.url):
            f = form(req)
            assert f["grant_type"] == "refresh_token" and f["refresh_token"] == "RT1"
            assert "offline_access" in f["scope"]
            return httpx.Response(200, json={"access_token": "NEW", "expires_in": 3600})
        assert req.headers["Authorization"] == "Bearer NEW"
        return httpx.Response(200, json={"value": []})

    mock(monkeypatch, handler)
    r = asyncio.run(client.get("graph", "/v1.0/me", conns={"graph": conn}))
    assert r["status"] == 200
    saved = store.get_oauth_tokens("graph")
    assert saved["access_token"] == "NEW" and saved["refresh_token"] == "RT1"


def test_not_signed_in_or_expired_without_refresh(cfile, vault, monkeypatch):
    seen = mock(monkeypatch, lambda req: httpx.Response(200, json={}))
    conns = {"graph": Connection.model_validate(GRAPH)}
    with pytest.raises(client.ConnectorError, match="not signed in"):
        asyncio.run(client.get("graph", "/v1.0/me", conns=conns))
    store.set_oauth_tokens("graph", {"access_token": "OLD", "expires_at": 1.0})
    with pytest.raises(client.ConnectorError, match="sign in again"):
        asyncio.run(client.get("graph", "/v1.0/me", conns=conns))
    assert not seen


def test_token_in_query_and_errors_hide_the_token(cfile, vault, monkeypatch):
    p = OAUTH_PRESETS["instagram"]
    conns = {"ig": Connection.model_validate({
        "type": "http_api", "base_url": p["base_url"],
        "auth": {"method": "oauth_browser", "client_id": "ig1", **p["auth"]}})}
    store.set_oauth_tokens("ig", {"access_token": "IGTOKEN", "expires_at": None})

    def ok(req: httpx.Request) -> httpx.Response:
        assert req.url.params["access_token"] == "IGTOKEN" and "Authorization" not in req.headers
        return httpx.Response(200, json={"id": "1"})

    mock(monkeypatch, ok)
    assert asyncio.run(client.get("ig", "/me", conns=conns))["status"] == 200

    def boom(req: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError(f"cannot reach {req.url}", request=req)

    mock(monkeypatch, boom)
    with pytest.raises(client.ConnectorError) as e:
        asyncio.run(client.get("ig", "/me", conns=conns))
    assert "IGTOKEN" not in str(e.value) and "***" in str(e.value)


# --- managed identity ------------------------------------------------------------------------
def test_managed_identity_imds_and_app_service(monkeypatch):
    conn = Connection.model_validate({"type": "azure", "auth": {"method": "managed_identity",
                                                                "client_id": "uami1"}})
    monkeypatch.delenv("IDENTITY_ENDPOINT", raising=False)
    monkeypatch.delenv("IDENTITY_HEADER", raising=False)

    def imds(req: httpx.Request) -> httpx.Response:
        if req.url.host == "169.254.169.254":
            assert req.headers["Metadata"] == "true"
            q = dict(req.url.params)
            assert q["resource"] == "https://management.azure.com" and q["client_id"] == "uami1"
            return httpx.Response(200, json={"access_token": "MI1", "expires_on": "9999999999"})
        assert req.headers["Authorization"] == "Bearer MI1"
        return httpx.Response(200, json={"value": []})

    mock(monkeypatch, imds)
    assert asyncio.run(client.test("az", {"az": conn}))["ok"]

    client._tokens.clear()
    monkeypatch.setenv("IDENTITY_ENDPOINT", "http://localhost:41000/msi/token")
    monkeypatch.setenv("IDENTITY_HEADER", "hdr")

    def app_service(req: httpx.Request) -> httpx.Response:
        if req.url.host == "localhost":
            assert req.headers["X-IDENTITY-HEADER"] == "hdr"
            assert req.url.params["api-version"] == "2019-08-01"
            return httpx.Response(200, json={"access_token": "MI2", "expires_in": 3600})
        assert req.headers["Authorization"] == "Bearer MI2"
        return httpx.Response(200, json={"value": []})

    mock(monkeypatch, app_service)
    assert asyncio.run(client.test("az", {"az": conn}))["ok"]


def test_managed_identity_not_available(monkeypatch):
    monkeypatch.delenv("IDENTITY_ENDPOINT", raising=False)

    def down(req: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("timeout", request=req)

    mock(monkeypatch, down)
    conn = Connection.model_validate({"type": "microsoft_graph",
                                      "auth": {"method": "managed_identity"}})
    r = asyncio.run(client.test("g", {"g": conn}))
    assert not r["ok"] and "not available here" in r["detail"]


# --- editor API ------------------------------------------------------------------------------
def test_editor_sign_in_flow(tmp_path, cfile, vault, monkeypatch):
    shutil.copytree(PIPELINE, tmp_path / "flows/pipeline")
    c = TestClient(create_app(tmp_path / "flows", allowed_hosts=["testserver"]))
    assert c.put("/api/connections/graph", json=GRAPH, headers=H).status_code == 200
    listing = c.get("/api/connections").json()
    assert listing["oauth_redirect_uri"] == "http://testserver/api/oauth/callback"
    assert listing["connections"]["graph"]["oauth"] == {"signed_in": False, "expires_at": None,
                                                        "refreshable": False}
    assert listing["connections"]["graph"]["secret"]["optional"] is True
    assert "facebook" in listing["oauth_presets"]

    assert c.post("/api/connections/graph/oauth/start").status_code == 403
    r = c.post("/api/connections/graph/oauth/start", headers=H).json()
    q = query(r["url"])
    assert q["redirect_uri"] == "http://testserver/api/oauth/callback"

    mock(monkeypatch, lambda req: httpx.Response(200, json={
        "access_token": "AT9", "refresh_token": "RT9", "expires_in": 3600}))
    page = c.get("/api/oauth/callback", params={"state": q["state"], "code": "c"})
    assert page.status_code == 200 and "signed in" in page.text
    listing = c.get("/api/connections")
    assert listing.json()["connections"]["graph"]["oauth"]["signed_in"] is True
    assert "AT9" not in listing.text and "RT9" not in listing.text
    again = c.get("/api/oauth/callback", params={"state": q["state"], "code": "c"})
    assert again.status_code == 400 and "unknown or expired" in again.text

    # a refused sign-in invalidates its state and escapes the provider's text
    q2 = query(c.post("/api/connections/graph/oauth/start", headers=H).json()["url"])
    bad = c.get("/api/oauth/callback", params={"state": q2["state"], "error": "access_denied",
                                               "error_description": "<script>x</script>"})
    assert bad.status_code == 400 and "<script>" not in bad.text and "&lt;script&gt;" in bad.text
    assert q2["state"] not in oauth._pending

    out = c.delete("/api/connections/graph/oauth", headers=H).json()
    assert out["oauth"]["signed_in"] is False
    store.set_oauth_tokens("graph", {"access_token": "X"})
    c.delete("/api/connections/graph", headers=H)
    assert vault == {}


def test_start_refused_for_other_methods(tmp_path, cfile, vault):
    c = TestClient(create_app(tmp_path, allowed_hosts=["testserver"]))
    body = {"type": "github", "auth": {"method": "pat"}}
    c.put("/api/connections/gh", json=body, headers=H)
    r = c.post("/api/connections/gh/oauth/start", headers=H)
    assert r.status_code == 400 and "does not use sign-in" in r.json()["detail"]

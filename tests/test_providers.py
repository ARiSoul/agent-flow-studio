"""Model providers (providers.py): the file, `provider/model` references, the session environment
(secrets only from ${VAR} or the keyring, the Anthropic key never reaches another provider), the
compile of graph and orchestrator flows, and unknown costs on a gateway. No real model is called."""
import shutil
from pathlib import Path

import pytest
import yaml
from test_engine import FIX, agent_query, run, types, wait

from agent_flow_studio import providers
from agent_flow_studio.builder import load_spec
from agent_flow_studio.builder.compile import FlowError, compile_agent, compile_flow
from agent_flow_studio.editor.runner import Runner
from agent_flow_studio.editor.validation import _check as validate_flow

PROVIDERS = {"providers": {
    "lite": {"kind": "gateway", "base_url": "http://localhost:4000", "key": "${LITE_KEY}",
             "models": ["gpt-5", "gemini-2.5-pro"]},
    "bed": {"kind": "bedrock", "region": "eu-west-1", "aws_profile": "work",
            "models": ["eu.anthropic.claude-sonnet-4-6"], "small_model": "eu.anthropic.claude-haiku-4-5"},
    "azure": {"kind": "foundry", "resource": "my-res"},
    "vault": {"kind": "anthropic", "key": "keyring"},
}}


@pytest.fixture
def setup(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    f = tmp_path / "providers.yaml"
    f.write_text(yaml.safe_dump(PROVIDERS), encoding="utf-8")
    monkeypatch.setenv("PROVIDERS_FILE", str(f))
    monkeypatch.setenv("LITE_KEY", "sk-lite-test")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-of-this-machine")
    return tmp_path


def _flow(tmp_path: Path, execution: str = "graph", **models: str) -> Path:
    shutil.copytree(FIX / "parallel", tmp_path / "flows/parallel")
    f = tmp_path / "flows/parallel/flow.yaml"
    data = yaml.safe_load(f.read_text(encoding="utf-8"))
    data["execution"] = execution
    for name, model in models.items():
        if name == "flow":
            data["model"] = model
        else:
            data["agents"][name]["model"] = model
    f.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    return tmp_path / "flows"


def test_the_file_and_references(setup):
    found = providers.load()
    assert set(found) == {"lite", "bed", "azure", "vault"}
    assert providers.split("sonnet") == (None, "sonnet")
    assert providers.split("lite/gpt-5") == ("lite", "gpt-5")
    assert providers.split("lite/openai/gpt-5") == ("lite", "openai/gpt-5")  # LiteLLM ids
    arn = "arn:aws:bedrock:eu-west-1:1:inference-profile/x"
    assert providers.split(arn) == (None, arn)
    with pytest.raises(providers.ProviderError, match="not set up on this machine"):
        providers.split("nope/model")
    # no secret in the file, and what each kind needs
    for bad, why in [({"kind": "gateway", "base_url": "http://x", "key": "sk-123"}, "never the secret"),
                     ({"kind": "gateway"}, "needs base_url"),
                     ({"kind": "vertex", "region": "x"}, "needs project")]:
        (setup / "providers.yaml").write_text(yaml.safe_dump({"providers": {"p": bad}}), "utf-8")
        with pytest.raises(providers.ProviderError, match=why):
            providers.load()


def test_session_environment(setup, monkeypatch):
    found = providers.load()
    env = providers.session_env("lite", found["lite"], "gpt-5")
    assert env["ANTHROPIC_BASE_URL"] == "http://localhost:4000"
    assert env["ANTHROPIC_AUTH_TOKEN"] == "sk-lite-test"
    assert env["ANTHROPIC_API_KEY"] == ""  # the machine's Anthropic key never goes to a gateway
    assert env["ANTHROPIC_DEFAULT_HAIKU_MODEL"] == env["ANTHROPIC_DEFAULT_SONNET_MODEL"] == "gpt-5"
    assert env["CLAUDE_CODE_USE_BEDROCK"] == ""
    bed = providers.session_env("bed", found["bed"], "eu.anthropic.claude-sonnet-4-6")
    assert bed["CLAUDE_CODE_USE_BEDROCK"] == "1" and bed["AWS_REGION"] == "eu-west-1"
    assert bed["AWS_PROFILE"] == "work" and bed["ANTHROPIC_API_KEY"] == ""
    assert bed["ANTHROPIC_SMALL_FAST_MODEL"] == "eu.anthropic.claude-haiku-4-5"
    az = providers.session_env("azure", found["azure"], "claude-sonnet-4-6")
    assert az["CLAUDE_CODE_USE_FOUNDRY"] == "1" and az["ANTHROPIC_FOUNDRY_RESOURCE"] == "my-res"
    assert az["ANTHROPIC_FOUNDRY_API_KEY"] == ""  # Entra ID sign-in
    # a key that is not there: a clear error, never a session on the default sign-in
    monkeypatch.delenv("LITE_KEY")
    with pytest.raises(providers.ProviderError, match=r"key is not set \(\$\{LITE_KEY\}\)"):
        providers.session_env("lite", found["lite"], "gpt-5")
    assert providers.session_env("lite", found["lite"], "gpt-5",
                                 resolve_secrets=False)["ANTHROPIC_AUTH_TOKEN"] == ""
    # the keyring (a fake one)
    store: dict = {}

    class FakeKeyring:
        def get_password(self, service, key):
            return store.get((service, key))

        def set_password(self, service, key, value):
            store[(service, key)] = value

    monkeypatch.setattr("agent_flow_studio.connections.store._keyring", lambda: FakeKeyring())
    assert providers.key_status("vault", found["vault"]) == "missing"
    providers.set_key("vault", "sk-ant-vault")
    assert providers.key_status("vault", found["vault"]) == "set"
    assert providers.session_env("vault", found["vault"], "claude-opus-5")["ANTHROPIC_API_KEY"] \
        == "sk-ant-vault"
    assert all("sk-" not in str(v) for p in providers.summary() for v in p.values())


def test_graph_agents_on_their_providers_and_unknown_costs(setup):
    base = _flow(setup, writer="lite/gpt-5", planner="bed/eu.anthropic.claude-sonnet-4-6")
    spec, flow_dir = load_spec("parallel", base)
    opts = compile_agent(spec, flow_dir, "writer")
    assert opts.model == "gpt-5" and opts.env["ANTHROPIC_AUTH_TOKEN"] == "sk-lite-test"
    plain = compile_agent(spec, flow_dir, "researcher_a")
    assert plain.env == {}  # no provider: the CLI's own sign-in, as before
    q = agent_query()
    rn = Runner(base, q)

    async def go():
        r = rn.start("parallel", inputs={"topic": "agents"}, run_id="p1")
        await wait(r)
        return r

    r = run(go())
    assert r.status == "gate"
    by = {c["agent"]: c["options"] for c in q.log}
    assert by["planner"].env["CLAUDE_CODE_USE_BEDROCK"] == "1"
    assert by["writer"].model == "gpt-5"
    costs = {e["agent"]: e["cost"] for e in types(r, "result")}
    assert costs["writer"] is None and costs["planner"] == 0.01  # a gateway: unknown, not a guess
    assert r.cost == pytest.approx(0.03)


def test_orchestrator_mode_keeps_one_provider(setup):
    base = _flow(setup, "orchestrator", flow="lite/gpt-5", writer="lite/gemini-2.5-pro")
    spec, flow_dir = load_spec("parallel", base)
    cf = compile_flow(spec, flow_dir, configure_artifacts=False)
    assert cf.options.model == "gpt-5" and cf.options.agents["writer"].model == "gemini-2.5-pro"
    assert cf.options.env["ANTHROPIC_BASE_URL"] == "http://localhost:4000" and not cf.priced
    spec.agents["writer"].model = "bed/eu.anthropic.claude-sonnet-4-6"
    with pytest.raises(FlowError, match="use the graph engine to mix providers"):
        compile_flow(spec, flow_dir, resolve_secrets=False, configure_artifacts=False)


def test_validation_names_an_unknown_provider_without_reading_secrets(setup, monkeypatch):
    monkeypatch.delenv("LITE_KEY")
    raw = yaml.safe_load((FIX / "parallel/flow.yaml").read_text(encoding="utf-8"))
    raw["agents"]["writer"]["model"] = "lite/gpt-5"
    prompts: dict = {}  # the fixture's prompts are inline
    assert validate_flow(raw, prompts)["ok"]  # the key is only read when a session starts
    raw["agents"]["writer"]["model"] = "other/gpt-5"
    out = validate_flow(raw, prompts)
    assert not out["ok"] and "not set up on this machine" in out["errors"][0]["msg"]


def test_the_providers_api_never_shows_or_stores_a_key(tmp_path, monkeypatch):
    import httpx
    from fastapi.testclient import TestClient

    from agent_flow_studio.editor.server import create_app
    f = tmp_path / "providers.yaml"
    monkeypatch.setenv("PROVIDERS_FILE", str(f))
    store: dict = {}

    class FakeKeyring:
        def get_password(self, service, key):
            return store.get((service, key))

        def set_password(self, service, key, value):
            store[(service, key)] = value

        def delete_password(self, service, key):
            store.pop((service, key))

    monkeypatch.setattr("agent_flow_studio.connections.store._keyring", lambda: FakeKeyring())
    c = TestClient(create_app(tmp_path, allowed_hosts=["testserver"]))
    h = {"X-Flow-Editor": "1"}
    assert c.get("/api/providers").json()["providers"] == {}
    assert c.put("/api/providers/lite", json={"kind": "gateway"}).status_code == 403  # no header
    bad = c.put("/api/providers/lite", headers=h, json={"kind": "gateway", "base_url": "http://x",
                                                         "key": "sk-plain"})
    assert bad.status_code == 400 and "never the secret" in bad.text
    assert c.put("/api/providers/lite", headers=h, json={"kind": "gateway"}).status_code == 400
    ok = c.put("/api/providers/lite", headers=h, json={
        "kind": "gateway", "base_url": "http://localhost:4000", "models": ["gpt-5"],
        "location": "local", "key_status": "none"})
    assert ok.status_code == 200 and ok.json()["providers"]["lite"]["key_status"] == "none"
    r = c.put("/api/providers/lite/key", headers=h, json={"value": "sk-lite-secret"})
    assert r.json()["providers"]["lite"]["key"] == "keyring"
    assert r.json()["providers"]["lite"]["key_status"] == "set"
    assert "sk-lite-secret" not in f.read_text("utf-8") and "sk-lite-secret" not in r.text
    assert "lite/gpt-5" in c.get("/api/catalog").json()["models"]
    # the check asks the gateway for its models with the key (nothing is generated)
    seen = {}

    def answer(req):
        seen["auth"] = req.headers.get("authorization")
        return httpx.Response(200, json={"data": [{"id": "gpt-5"}]})

    monkeypatch.setattr(providers, "_transport", httpx.MockTransport(answer))
    t = c.post("/api/providers/lite/test", headers=h).json()
    assert t["ok"] and t["models"] == ["gpt-5"] and seen["auth"] == "Bearer sk-lite-secret"
    assert c.put("/api/providers/lite/key", headers=h, json={"env": "LITE_KEY"}).json()[
        "providers"]["lite"]["key"] == "${LITE_KEY}"
    assert c.delete("/api/providers/lite", headers=h).json()["providers"] == {}
    assert store == {}  # the keyring entry went with it

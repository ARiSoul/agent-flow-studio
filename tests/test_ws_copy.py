"""Copy and duplicate across workspaces: flows (with the dependency check), library agents,
agents of a flow, connectors (secrets never in files; "use the same secret" through a fake
keyring), variables/environments, name clashes, whole workspaces, and the editor API."""
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient

from agent_flow_studio import workspaces
from agent_flow_studio.builder import library
from agent_flow_studio.connections import store
from agent_flow_studio.editor import copying
from agent_flow_studio.editor.copying import Item
from agent_flow_studio.editor.server import create_app
from agent_flow_studio.messages import MsgError

H = {"X-Flow-Editor": "1"}
API = {"type": "http_api", "base_url": "https://shop.example.com", "auth": {"method": "bearer"},
       "secrets": {"token": "keyring"}}


@pytest.fixture
def vault(monkeypatch, tmp_path):
    monkeypatch.setenv("CONNECTIONS_FILE", str(tmp_path / "connections.yaml"))
    data: dict = {}

    class K:
        set_password = staticmethod(lambda s, u, v: data.__setitem__((s, u), v))
        get_password = staticmethod(lambda s, u: data.get((s, u)))
        delete_password = staticmethod(lambda s, u: data.pop((s, u)))

    monkeypatch.setattr(store, "_keyring", lambda: K)
    return data


def _spec(ws: workspaces.Workspace, **data) -> workspaces.Workspace:
    assert ws.spec is not None
    ws.spec = workspaces.WorkspaceSpec.model_validate({**ws.spec.model_dump(mode="json"), **data})
    workspaces.save(ws)
    return ws


@pytest.fixture
def shop(tmp_path, vault) -> workspaces.Workspace:
    """A workspace with a flow that uses a variable, a root, a connector and a library agent."""
    repo = tmp_path / "shop-repo"
    repo.mkdir()
    ws = _spec(workspaces.create("shop", "repo", repo),
               vars={"team": "core"},
               environments={"dev": {"kind": "dev", "vars": {"api": "https://dev"}},
                             "prod": {"kind": "prod", "vars": {"api": "https://prod"}}})
    d = ws.flows_dir / "orders"
    (d / "prompts").mkdir(parents=True)
    (d / "prompts/reader.md").write_text("Read orders for {vars.team} at {vars.api}.",
                                         encoding="utf-8")
    flow = {"version": 1, "name": "orders", "title": "Orders", "task": "Check {input}.",
            "start": "reader", "workspace": {"root": "app"}, "edges": [{"from": "reader", "to": "writer"}],
            "agents": {"reader": {"description": "Reads.", "prompt_file": "prompts/reader.md",
                                  "connections": ["api"], "outputs": {"orders": {"type": "text"}},
                                  "origin": {"agent": "reviewer", "hash": "abcdef12"}},
                       "writer": {"description": "Writes.", "prompt": "Write.",
                                  "inputs": {"orders": {"from": "reader.orders"}}}}}
    (d / "flow.yaml").write_text(yaml.safe_dump(flow, sort_keys=False), encoding="utf-8")
    (ws.agents_dir / "reviewer").mkdir(parents=True)
    (ws.agents_dir / "reviewer/agent.yaml").write_text("description: Reviews.\n", encoding="utf-8")
    (ws.agents_dir / "reviewer/prompt.md").write_text("Review.", encoding="utf-8")
    with workspaces.using(ws):
        conns = store.load()
        conns["api"] = store.Connection.model_validate(API)
        store.save(conns)
        store.set_secret("api", "token", "s3cret")
    return ws


@pytest.fixture
def lab(vault) -> workspaces.Workspace:
    return _spec(workspaces.create("lab", "local"),
                 environments={"dev": {"kind": "dev"}, "prod": {"kind": "prod"}})


def _deps(plan):
    return {f"{d['kind']}:{d['name']}": (d["in_target"], d["choice"]) for d in plan["deps"]}


def test_flow_copy_plan_lists_what_the_target_lacks(shop, lab):
    plan = copying.copy_plan(shop, lab, Item("flow", "orders"))
    assert plan["clash"] is False and plan["name"] == "orders"
    assert _deps(plan) == {"var:team": (False, "copy"), "var:api": (False, "copy"),
                           "root:app": (False, "copy"), "connector:api": (False, "copy"),
                           "agent:reviewer": (False, "copy")}
    # to the studio: variables and roots cannot be copied (it has none)
    plan = copying.copy_plan(shop, workspaces.STUDIO_WS, Item("flow", "orders"))
    assert _deps(plan)["var:team"] == (False, "leave")


def test_copy_a_flow_with_its_dependencies(shop, lab, vault):
    res = copying.copy(shop, lab, Item("flow", "orders"))
    got = {(c["kind"], c["as"]) for c in res.copied}
    assert got == {("var", "team"), ("var", "api"), ("root", "app"), ("connector", "api"),
                   ("agent", "reviewer"), ("flow", "orders")}
    lab = workspaces.get("lab")
    assert lab.spec is not None
    assert lab.spec.vars["team"] == "core"
    assert lab.spec.environments["prod"].vars["api"] == "https://prod"
    assert Path(lab.spec.roots["app"].path) == shop.home  # the same folder, absolute
    assert (lab.flows_dir / "orders/prompts/reader.md").is_file()
    assert (lab.agents_dir / "reviewer/prompt.md").is_file()
    # the connector arrives without its secret
    assert res.missing_secrets == ["api/token"] and res.secrets == []
    text = lab.connections_file.read_text(encoding="utf-8")
    assert "api" in text and "s3cret" not in text
    assert ("Secret", "ws:lab/api/token") not in vault
    # the copy validates in its new workspace
    from agent_flow_studio.builder import load_spec
    with workspaces.using(lab, "prod"):
        spec, _ = load_spec("orders")
        assert "agents" in spec.model_dump()


def test_use_the_same_secret_copies_the_keyring_entry(shop, lab, vault):
    res = copying.copy(shop, lab, Item("connector", "api"), same_secrets=True)
    assert res.secrets == ["api/token"] and not res.missing_secrets
    assert sorted(u for _, u in vault) == ["ws:lab/api/token", "ws:shop/api/token"]
    assert set(vault.values()) == {"s3cret"}
    assert "s3cret" not in workspaces.get("lab").connections_file.read_text(encoding="utf-8")


def test_choices_leave_and_use_the_target(shop, lab):
    _spec(lab, vars={"team": "other"})
    plan = copying.copy_plan(shop, lab, Item("flow", "orders"))
    assert _deps(plan)["var:team"] == (True, "target")
    res = copying.copy(shop, lab, Item("flow", "orders"),
                       deps={"connector:api": "leave", "agent:reviewer": "leave"})
    kinds = {c["kind"] for c in res.copied}
    assert "connector" not in kinds and "agent" not in kinds
    assert workspaces.get("lab").spec.vars["team"] == "other"  # the target's own stays


def test_name_clashes(shop, lab):
    copying.copy(shop, lab, Item("flow", "orders"))
    with pytest.raises(MsgError) as e:
        copying.copy(shop, lab, Item("flow", "orders"))
    assert e.value.msgs[0].code == "copy.clash"
    assert copying.copy_plan(shop, lab, Item("flow", "orders"))["suggestion"] == "orders_2"
    copying.copy(shop, lab, Item("flow", "orders"), new_name="orders_2")
    raw = yaml.safe_load((lab.flows_dir / "orders_2/flow.yaml").read_text(encoding="utf-8"))
    assert raw["name"] == "orders_2"
    copying.copy(shop, lab, Item("flow", "orders"), replace=True)  # the old one goes to .trash
    assert any((lab.flows_dir / ".trash").iterdir())
    # duplicate inside the same workspace: another name is needed
    with pytest.raises(MsgError) as e:
        copying.copy(shop, shop, Item("flow", "orders"), new_name="orders")
    assert e.value.msgs[0].code in ("copy.same_name", "copy.clash")
    copying.copy(shop, shop, Item("flow", "orders"), new_name="orders_copy")
    assert (shop.flows_dir / "orders_copy/flow.yaml").is_file()


def test_copy_an_agent_of_a_flow(shop, lab):
    copying.copy(shop, lab, Item("flow", "orders"))
    # into the target's library: flow-only links (from) are left out
    copying.copy(shop, lab, Item("flow_agent", "orders", agent="writer"), new_name="scribe")
    with workspaces.using(lab):
        a = library.list_agents()["scribe"]
    assert a.source == "workspace" and a.spec.inputs["orders"].from_ is None
    # into another flow: a new node with free inputs and its own prompt file
    copying.copy(shop, lab, Item("flow_agent", "orders", agent="reader", to_flow="orders"),
                 new_name="reader_2")
    raw = yaml.safe_load((lab.flows_dir / "orders/flow.yaml").read_text(encoding="utf-8"))
    assert raw["agents"]["reader_2"]["prompt_file"] == "prompts/reader_2.md"
    assert (lab.flows_dir / "orders/prompts/reader_2.md").is_file()
    with pytest.raises(MsgError) as e:
        copying.copy(shop, lab, Item("flow_agent", "orders", agent="reader", to_flow="orders"))
    assert e.value.msgs[0].code == "copy.clash"


def test_copy_variables_and_environments(shop, lab):
    res = copying.copy(shop, lab, Item("vars", vars=["team"], environments=["prod"]))
    assert {(c["kind"], c["name"]) for c in res.copied} == {("var", "team"),
                                                             ("environment", "prod")}
    lab = workspaces.get("lab")
    assert lab.spec.vars["team"] == "core" and lab.spec.environments["prod"].vars == {
        "api": "https://prod"}
    with pytest.raises(MsgError):
        copying.copy(shop, lab, Item("vars", vars=["nope"]))
    with pytest.raises(MsgError):
        copying.copy(shop, workspaces.STUDIO_WS, Item("vars", vars=["team"]))


def test_duplicate_a_whole_workspace(shop, vault, tmp_path):
    new, res = copying.duplicate(shop, "shop_copy", "local", same_secrets=True)
    assert new.where == "local" and (new.flows_dir / "orders/flow.yaml").is_file()
    assert (new.agents_dir / "reviewer").is_dir() and new.connections_file.is_file()
    assert new.spec.vars == {"team": "core"} and set(new.spec.environments) == {"dev", "prod"}
    assert Path(new.spec.roots["app"].path) == shop.home  # still the same repository
    assert res.secrets == ["api/token"] and ("Secret", "ws:shop_copy/api/token") in {
        (k[0].split(":")[0] if False else "Secret", k[1]) for k in vault}
    assert [w["name"] for w in workspaces.list_all()] == ["studio", "shop", "shop_copy"]
    with pytest.raises(MsgError):
        copying.duplicate(workspaces.STUDIO_WS, "x", "local")
    with pytest.raises(MsgError):
        copying.duplicate(shop, "shop_copy", "local")  # the name is taken


def test_copy_api(shop, lab, tmp_path):
    (tmp_path / "flows").mkdir()
    c = TestClient(create_app(tmp_path / "flows", allowed_hosts=["testserver"]))
    r = c.post("/api/workspaces/shop/copy-plan", headers=H, json={"kind": "flow", "name": "orders",
                                                                  "to": "lab"})
    assert r.status_code == 200 and len(r.json()["deps"]) == 5
    r = c.post("/api/workspaces/shop/copy", headers=H,
               json={"kind": "flow", "name": "orders", "to": "lab", "same_secrets": True})
    assert r.status_code == 200 and r.json()["secrets"] == ["api/token"]
    r = c.post("/api/workspaces/shop/copy", headers=H, json={"kind": "flow", "name": "orders",
                                                             "to": "lab"})
    assert r.status_code == 422 and r.json()["detail"]["code"] == "copy.clash"
    r = c.post("/api/workspaces/shop/duplicate", headers=H, json={"name": "shop2"})
    assert r.status_code == 201 and r.json()["name"] == "shop2"
    audit = (tmp_path / "runs/audit.jsonl").read_text(encoding="utf-8")
    assert "workspace_copy" in audit and "workspace_duplicate" in audit and "s3cret" not in audit
    # a flow copied into the studio's own flows (this machine, never public)
    r = c.post("/api/workspaces/shop/copy", headers=H, json={"kind": "flow", "name": "orders",
                                                             "to": "studio"})
    assert r.status_code == 200 and (tmp_path / "flows/orders/flow.yaml").is_file()

"""Studio workspaces: storage in a repository or in the studio, the registry, variables and
environments, {vars.x}/{roots.x} in flows, the environment kind in the write policy, workspace
agents and connectors layered over the studio ones, runs and per-workspace state, and the
editor API (the workspace comes in a cookie). No real model is called."""
import asyncio
import json
import sys
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient

from agent_flow_studio import workspaces
from agent_flow_studio.builder import FlowError, compile_flow, library, load_spec
from agent_flow_studio.builder.spec import FlowSpec
from agent_flow_studio.connections import store
from agent_flow_studio.editor import chats
from agent_flow_studio.editor.runner import Runner
from agent_flow_studio.editor.server import create_app
from agent_flow_studio.messages import MsgError
from agent_flow_studio.tools import io as io_tool

sys.path.insert(0, str(Path(__file__).parent))  # the fake SDK helpers of the engine tests
from test_engine import agent_query, write_flow
from test_writes import call

H = {"X-Flow-Editor": "1"}


def _ws(tmp_path: Path, **spec) -> workspaces.Workspace:
    """A workspace in the studio with two environments (dev and prod)."""
    ws = workspaces.create("shop", "local", title="Shop")
    assert ws.spec is not None
    data = {**ws.spec.model_dump(mode="json"),
            "vars": {"team": "core", "branch": "main"},
            "environments": {"dev": {"kind": "dev", "vars": {"api": "https://dev.example.com"}},
                             "prod": {"kind": "prod", "vars": {"api": "https://example.com",
                                                               "branch": "release"}}},
            **spec}
    ws.spec = workspaces.WorkspaceSpec.model_validate(data)
    workspaces.save(ws)
    return ws


def _flow(**changes) -> dict:
    d = {"version": 1, "name": "hello", "title": "Hello", "execution": "graph",
         "task": "Help team {vars.team} on {vars.branch} with {input}.", "start": "writer",
         "agents": {"writer": {"description": "Writes.", "prompt": "Call {vars.api}.",
                               "outputs": {"text": {"type": "text"}}}}}
    d.update(changes)
    return d


# --- storage and registry -----------------------------------------------------------------------
def test_create_in_the_studio_and_in_a_repo(tmp_path):
    local = workspaces.create("shop", "local", title="Shop")
    assert local.where == "local" and local.folder == (tmp_path / "workspaces/shop").resolve()
    assert (local.folder / "workspace.yaml").is_file() and local.flows_dir.is_dir()
    repo = tmp_path / "game"
    repo.mkdir()
    inrepo = workspaces.create("game", "repo", repo)
    assert inrepo.where == "repo" and inrepo.folder == (repo / ".agent-flow").resolve()
    assert inrepo.home == repo.resolve()
    assert inrepo.resolve(None)[1] == {"app": str(repo.resolve())}  # the repo is root `app`
    names = [w["name"] for w in workspaces.list_all()]
    assert names == ["studio", "shop", "game"]
    # forget it, then open it again from the repository folder
    workspaces.unregister("game")
    assert workspaces.get_or_none("game") is None
    assert workspaces.open_folder(repo).name == "game"
    assert workspaces.get("game").folder == inrepo.folder


@pytest.mark.parametrize("call_, code", [
    (lambda tmp: workspaces.create("studio", "local"), "ws.exists"),
    (lambda tmp: workspaces.create("game", "repo", None), "ws.repo_needed"),
    (lambda tmp: workspaces.create("game", "repo", tmp / "nope"), "ws.repo_missing"),
    (lambda tmp: workspaces.get("nobody"), "ws.unknown"),
    (lambda tmp: workspaces.open_folder(tmp / "empty"), "ws.missing"),
])
def test_storage_errors(tmp_path, call_, code):
    with pytest.raises(MsgError) as e:
        call_(tmp_path)
    assert e.value.msgs[0].code == code


def test_a_moved_workspace_shows_as_missing(tmp_path):
    ws = workspaces.create("shop", "local")
    (ws.folder / "workspace.yaml").unlink()
    item = workspaces.list_all()[1]
    assert item["name"] == "shop" and item["where"] == "missing" and not item["ok"]


@pytest.mark.parametrize("patch, code", [
    ({"vars": {"token": "ghp_" + "a" * 36}}, "ws.var_secret"),
    ({"vars": {"Bad": "x"}}, "ws.var_name"),
    ({"vars": {"long": "x" * 3000}}, "ws.var_long"),
    ({"environments": {"Prod": {"kind": "prod"}}}, "ws.key_name"),
    ({"default_environment": "qa"}, "ws.default_env"),
    ({"name": "studio"}, "ws.reserved"),
])
def test_spec_rules(patch, code):
    data = {"version": 1, "name": "shop", "environments": {"dev": {"kind": "dev"}}, **patch}
    with pytest.raises(ValueError) as e:
        workspaces.WorkspaceSpec.model_validate(data)
    assert code in repr(e.value) or code in json.dumps(
        [x.get("ctx", {}).get("error").msgs[0].code for x in e.value.errors()
         if isinstance(x.get("ctx", {}).get("error"), MsgError)])


def test_environment_vars_override_the_workspace_ones(tmp_path):
    ws = _ws(tmp_path)
    values, _, kind = ws.resolve("prod")
    assert values == {"team": "core", "branch": "release", "api": "https://example.com"}
    assert kind == "prod"
    assert ws.resolve(None)[0]["branch"] == "main"  # the default environment (dev)
    assert ws.known_vars() == {"team", "branch", "api"}  # api: every environment has it


# --- variables in flows -----------------------------------------------------------------------
def test_vars_are_filled_in_flows_and_prompts(tmp_path):
    ws = _ws(tmp_path)
    write_flow(ws.flows_dir, _flow())
    (ws.flows_dir / "hello/prompts").mkdir()
    (ws.flows_dir / "hello/prompts/w.md").write_text("Use {roots.app} {{vars.team}}.",
                                                     encoding="utf-8")
    with workspaces.using(ws, "prod"):
        spec, _ = load_spec("hello")
        assert spec.task == "Help team core on release with {input}."
        assert spec.agents["writer"].prompt == "Call https://example.com."
    with workspaces.using(ws):  # the default environment
        assert load_spec("hello")[0].task == "Help team core on main with {input}."
    with workspaces.using(workspaces.STUDIO_WS), pytest.raises(FlowError):
        load_spec("hello", base=ws.flows_dir)  # the studio has no such variables


def test_unknown_vars_are_errors(tmp_path):
    from agent_flow_studio.editor.validation import _check

    ws = _ws(tmp_path)
    write_flow(ws.flows_dir, _flow(task="Use {vars.nope} {input}."))
    with workspaces.using(ws):
        with pytest.raises(FlowError, match="vars.nope"):
            load_spec("hello")
        errs = _check(_flow(task="Use {vars.nope} {input}."), {})["errors"]
        assert errs[0]["code"] == "spec.var_unknown"
        bad = _flow(edges=[], gates={"g": {"kind": "stop", "label": "G"}})
        bad["edges"] = [{"from": "writer", "to": "g", "if": "vars.nope == 'x'"}]
        codes = {e.get("code") for e in _check(bad, {})["errors"]}
        assert "spec.condition_var" in codes or "spec.edge_reason" in codes
        ok = _flow(gates={"g": {"kind": "stop", "label": "G"}},
                   edges=[{"from": "writer", "to": "g", "if": "vars.team == 'core'"}])
        assert _check(ok, {})["ok"], _check(ok, {})["errors"]


def test_reserved_names():
    for name in ("vars", "roots"):
        with pytest.raises(ValueError):
            FlowSpec.model_validate(_flow(gates={name: {"kind": "stop", "label": "x"}}))


def test_workspace_root_and_env_kind_drive_the_policy(tmp_path):
    repo = tmp_path / "game"
    repo.mkdir()
    ws = workspaces.create("game", "repo", repo)
    assert ws.spec is not None
    ws.spec = workspaces.WorkspaceSpec.model_validate({
        **ws.spec.model_dump(mode="json"),
        "environments": {"dev": {"kind": "dev"}, "prod": {"kind": "prod"}}})
    workspaces.save(ws)
    d = {"name": "dev", "title": "Dev", "task": "Do {input}.", "start": "coder",
         "autonomy": "approve_prod", "workspace": {"root": "app"},
         "agents": {"coder": {"description": "code", "prompt": "Write code.",
                              "tools": ["workspace.read", "workspace.edit"]}}}
    with workspaces.using(ws, "dev"):
        cf = compile_flow(FlowSpec.model_validate(d), tmp_path)
        assert cf.policy is not None and cf.policy.workspace == repo.resolve()
        assert call(cf, "Edit", file_path="a.py", old_string="a", new_string="b") is None
    with workspaces.using(ws, "prod"):
        cf = compile_flow(FlowSpec.model_validate(d), tmp_path)
        assert "human approval" in call(cf, "Edit", file_path="a.py", old_string="a",
                                        new_string="b")
    with pytest.raises(FlowError, match="not a root"):
        compile_flow(FlowSpec.model_validate(d), tmp_path)  # the studio has no root `app`
    with pytest.raises(ValueError):
        FlowSpec.model_validate({**d, "workspace": {"root": "app", "path": "."}})


# --- workspace agents and connectors ------------------------------------------------------------
def _agent(folder: Path, agent_id: str, prompt: str) -> None:
    (folder / agent_id).mkdir(parents=True)
    (folder / agent_id / "agent.yaml").write_text(
        yaml.safe_dump({"title": agent_id, "description": "Reviews."}), encoding="utf-8")
    (folder / agent_id / "prompt.md").write_text(prompt, encoding="utf-8")


def test_workspace_agents_win_over_studio_ones(tmp_path):
    ws = _ws(tmp_path)
    _agent(library.local_dir(), "reviewer", "Studio review.")
    _agent(ws.agents_dir, "reviewer", "Shop review.")
    assert library.list_agents()["reviewer"].source == "local"
    with workspaces.using(ws):
        a = library.list_agents()["reviewer"]
        assert a.source == "workspace" and a.prompt == "Shop review."
        saved, _ = library.save_local("checker", "Checker", {"description": "Checks."},
                                      "Check.", location="workspace")
        assert saved.folder.parent == ws.agents_dir
    assert "checker" not in library.list_agents()  # not seen from the studio


@pytest.fixture
def fake_keyring(monkeypatch):
    vault: dict = {}

    class K:
        set_password = staticmethod(lambda s, u, v: vault.__setitem__((s, u), v))
        get_password = staticmethod(lambda s, u: vault.get((s, u)))
        delete_password = staticmethod(lambda s, u: vault.pop((s, u)))

    monkeypatch.setattr(store, "_keyring", lambda: K)
    return vault


def test_workspace_connectors_win_and_have_their_own_secrets(tmp_path, monkeypatch,
                                                              fake_keyring):
    monkeypatch.setenv("CONNECTIONS_FILE", str(tmp_path / "connections.yaml"))
    api = {"type": "http_api", "base_url": "https://dev.example.com", "auth": {"method": "bearer"},
           "secrets": {"token": "keyring"}}
    (tmp_path / "connections.yaml").write_text(
        yaml.safe_dump({"version": 1, "connections": {"api": api}}), encoding="utf-8")
    ws = _ws(tmp_path)
    ws.connections_file.write_text(yaml.safe_dump({"version": 1, "connections": {
        "api": {**api, "base_url": "https://shop.example.com"}}}), encoding="utf-8")
    assert str(store.load()["api"].base_url).startswith("https://dev.")
    store.set_secret("api", "token", "studio-secret")
    with workspaces.using(ws):
        conns = store.load()
        assert str(conns["api"].base_url).startswith("https://shop.")
        assert store.location("api") == "workspace"
        assert store.get_secret("api", conns["api"], "token") is None  # not the studio's
        store.set_secret("api", "token", "shop-secret")
        assert store.location("new_one") == "workspace"  # new connectors stay in the workspace
    assert sorted(u for _, u in fake_keyring) == ["api/token", "ws:shop/api/token"]
    assert "shop-secret" not in ws.connections_file.read_text(encoding="utf-8")


# --- runs and per-workspace state ---------------------------------------------------------------
def test_a_run_keeps_its_workspace_and_environment(tmp_path):
    ws = _ws(tmp_path)
    write_flow(ws.flows_dir, _flow())
    q = agent_query({"writer": lambda rid, n: {"text": "hi"}})
    rn = Runner(tmp_path / "flows", q)  # the studio's flows: empty; the workspace has its own

    async def go():
        with workspaces.using(ws, "prod"):
            r = rn.start("hello", "r1")
        while r.task and not r.task.done():
            await asyncio.sleep(0.01)
        return r
    r = asyncio.run(go())
    assert r.status == "done", r.events[-3:]
    start = r.events[0]
    assert (start["workspace"], start["environment"]) == ("shop", "prod")
    assert "Help team core on release with r1." in q.log[0]["prompt"]
    assert r.summary()["workspace"] == "shop"
    prev = Path(io_tool.settings.runs_dir) / ".previous" / "@shop" / "hello.json"
    assert prev.is_file()  # previous.* is kept per workspace
    with workspaces.using(ws):
        assert [x["input"] for x in rn.past_runs("hello")] == ["r1"]
    assert rn.past_runs("hello") == []  # not a run of the studio


def test_chats_are_kept_per_workspace(tmp_path):
    ws = _ws(tmp_path)
    assert chats.chats_dir().name == "chats"
    with workspaces.using(ws):
        assert chats.chats_dir().name == "@shop"


# --- editor API -------------------------------------------------------------------------------
@pytest.fixture
def client(tmp_path: Path) -> TestClient:
    (tmp_path / "flows").mkdir()
    return TestClient(create_app(tmp_path / "flows", allowed_hosts=["testserver"]))


def test_workspace_api_and_the_cookie(client, tmp_path):
    r = client.post("/api/workspaces", json={"name": "shop", "title": "Shop"}, headers=H)
    assert r.status_code == 201 and r.json()["where"] == "local"
    r = client.post("/api/workspaces", json={"name": "shop"}, headers=H)
    assert r.status_code == 422 and r.json()["detail"]["code"] == "ws.exists"
    r = client.put("/api/workspaces/shop", headers=H, json={
        "vars": {"team": "core"},
        "environments": {"dev": {"kind": "dev"}, "prod": {"kind": "prod", "vars": {"x": "1"}}}})
    assert r.status_code == 200 and r.json()["spec"]["vars"] == {"team": "core"}
    r = client.put("/api/workspaces/shop", headers=H, json={"vars": {"t": "ghp_" + "a" * 36}})
    assert r.status_code == 422
    # flows of the studio vs flows of the workspace (chosen with the cookie)
    assert client.post("/api/flows", headers=H, json={"name": "a", "title": "A"}).status_code == 201
    client.cookies.set("afs_ws", "shop")
    client.cookies.set("afs_env", "prod")
    assert client.get("/api/flows").json() == []
    r = client.post("/api/flows", headers=H, json={"name": "b", "title": "B"})
    assert r.status_code == 201
    assert (tmp_path / "workspaces/shop/flows/b/flow.yaml").is_file()
    flows = client.get("/api/flows").json()
    assert [(f["name"], f["location"]) for f in flows] == [("b", "workspace")]
    cat = client.get("/api/catalog").json()
    assert cat["workspace"] == {"name": "shop", "where": "local", "environment": "prod",
                                "environments": ["dev", "prod"], "kind": "prod", "title": "Shop",
                                "vars": ["team"], "roots": []}
    assert cat["locations"][0] == "workspace"
    r = client.post("/api/flows/b/move", headers=H, json={"location": "public"})
    assert r.status_code == 400
    assert client.get("/api/workspaces").json()["current"]["name"] == "shop"
    client.cookies.clear()
    assert [f["name"] for f in client.get("/api/flows").json()] == ["a"]
    assert client.delete("/api/workspaces/shop", headers=H).json() == {"forgotten": "shop"}
    assert [w["name"] for w in client.get("/api/workspaces").json()["workspaces"]] == ["studio"]
    assert (tmp_path / "workspaces/shop/workspace.yaml").is_file()  # the files stay


def test_an_unknown_workspace_cookie_falls_back_to_the_studio(client):
    client.cookies.set("afs_ws", "nobody")
    assert client.get("/api/catalog").json()["workspace"]["name"] == "studio"


# --- command line -----------------------------------------------------------------------------
def test_cli_runs_a_flow_in_a_workspace_and_environment(tmp_path):
    from agent_flow_studio import main as cli

    ws = _ws(tmp_path)
    write_flow(ws.flows_dir, _flow())
    q = agent_query({"writer": lambda rid, n: {"text": "hi"}})
    lines: list[str] = []
    cli.main(["r1", "--flow", "hello", "--workspace", "shop", "--env", "prod"], query_fn=q,
             out=lines.append)
    assert "Help team core on release with r1." in q.log[0]["prompt"]
    assert (tmp_path / "runs/@shop/r1").is_dir()
    # by folder (not registered) and with an unknown environment
    assert workspaces.from_cli(str(ws.folder)).name == "shop"
    with pytest.raises(SystemExit, match="no environment"):
        cli.main(["r1", "--flow", "hello", "--workspace", "shop", "--env", "qa"], query_fn=q,
                 out=lines.append)
    with pytest.raises(SystemExit, match="unknown workspace"):
        cli.main(["r1", "--flow", "hello", "--workspace", "nope"], query_fn=q, out=lines.append)


def test_folder_browser_lists_subfolders(client, tmp_path):
    (tmp_path / "repos/game/.git").mkdir(parents=True)
    (tmp_path / "repos/shop/.agent-flow").mkdir(parents=True)
    (tmp_path / "repos/shop/.agent-flow/workspace.yaml").write_text("name: shop\n", encoding="utf-8")
    (tmp_path / "repos/.hidden").mkdir()
    (tmp_path / "repos/notes.txt").write_text("x", encoding="utf-8")
    r = client.get("/api/folders", params={"path": str(tmp_path / "repos")}).json()
    assert [(f["name"], f["repo"], f["workspace"]) for f in r["folders"]] == [
        ("game", True, False), ("shop", False, True)]
    assert r["parent"] == str(tmp_path.resolve())
    assert client.get("/api/folders", params={"path": str(tmp_path / "nope")}).status_code == 400

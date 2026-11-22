# ruff: noqa: F811  (the fixtures of test_ws_copy are imported and used as arguments)
"""Move across workspaces: copy, then the original leaves the source (flows and library agents
to .trash, connectors with their keyring secret, variables/environments, an agent out of its
flow). Refused into the same workspace, for shared items and for the start agent; nothing
leaves the source when the copy fails."""
import sys
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

sys.path.insert(0, str(Path(__file__).parent))
from test_ws_copy import H, lab, shop, vault  # noqa: F401 (fixtures)


def _code(e: pytest.ExceptionInfo) -> str:
    return e.value.msgs[0].code


def test_move_a_flow(shop, lab):
    copying.move(shop, lab, Item("flow", "orders"), deps={"connector:api": "leave",
                                                         "agent:reviewer": "leave"})
    assert (lab.flows_dir / "orders/flow.yaml").is_file()
    assert not (shop.flows_dir / "orders").exists()
    assert any(p.name.startswith("orders-") for p in (shop.flows_dir / ".trash").iterdir())
    # the dependencies stay in the source
    assert (shop.agents_dir / "reviewer").is_dir()


def test_move_a_connector_with_its_secret(shop, lab, vault):
    res = copying.move(shop, lab, Item("connector", "api"))
    assert res.secrets == ["api/token"]
    with workspaces.using(shop):
        assert "api" not in store.load()
    with workspaces.using(lab):
        assert store.location("api") == "workspace"
    assert sorted(u for _, u in vault) == ["ws:lab/api/token"]


def test_move_a_library_agent(shop, lab):
    copying.move(shop, lab, Item("agent", "reviewer"))
    with workspaces.using(lab):
        assert library.list_agents()["reviewer"].source == "workspace"
    with workspaces.using(shop):
        assert "reviewer" not in library.list_agents()


def test_move_variables_and_an_environment(shop, lab):
    _ = lab
    workspaces.save(shop)
    copying.move(shop, workspaces.get("lab"), Item("vars", vars=["team"], environments=["prod"]))
    s = workspaces.get("shop").spec
    assert "team" not in s.vars and set(s.environments) == {"dev"}
    assert workspaces.get("lab").spec.vars["team"] == "core"
    with pytest.raises(MsgError) as e:
        copying.move(workspaces.get("shop"), workspaces.get("lab"),
                     Item("vars", environments=["dev"]))
    assert _code(e) == "copy.move_last_env"


def test_move_an_agent_out_of_its_flow(shop, lab):
    copying.move(shop, lab, Item("flow_agent", "orders", agent="writer"), new_name="scribe")
    raw = yaml.safe_load((shop.flows_dir / "orders/flow.yaml").read_text(encoding="utf-8"))
    assert "writer" not in raw["agents"] and raw["edges"] == []
    with workspaces.using(lab):
        assert "scribe" in library.list_agents()
    with pytest.raises(MsgError) as e:  # the start agent stays
        copying.move(shop, lab, Item("flow_agent", "orders", agent="reader"))
    assert _code(e) == "copy.move_start"


def test_move_refusals_and_a_failed_copy(shop, lab):
    with pytest.raises(MsgError) as e:
        copying.move(shop, shop, Item("flow", "orders"), new_name="other")
    assert _code(e) == "copy.move_same"
    with pytest.raises(MsgError) as e:  # a built-in library agent is not the workspace's
        copying.move(shop, lab, Item("agent", "repo_explorer"))
    assert _code(e) == "copy.move_shared"
    copying.copy(shop, lab, Item("flow", "orders"))
    with pytest.raises(MsgError) as e:  # the name is taken there: nothing moves
        copying.move(shop, lab, Item("flow", "orders"))
    assert _code(e) == "copy.clash"
    assert (shop.flows_dir / "orders/flow.yaml").is_file()


def test_move_api(shop, lab, tmp_path):
    (tmp_path / "flows").mkdir()
    c = TestClient(create_app(tmp_path / "flows", allowed_hosts=["testserver"]))
    r = c.post("/api/workspaces/shop/move", headers=H, json={"kind": "flow", "name": "orders",
                                                             "to": "lab"})
    assert r.status_code == 200 and not (shop.flows_dir / "orders").exists()
    r = c.post("/api/workspaces/shop/move", headers=H, json={"kind": "flow", "name": "orders",
                                                             "to": "shop"})
    assert r.status_code == 422
    assert '"workspace_move"' in (tmp_path / "runs/audit.jsonl").read_text(encoding="utf-8")

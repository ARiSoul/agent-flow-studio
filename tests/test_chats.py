"""History of the assistant conversations (runs/chats/<flow>/<id>.json)."""
import shutil
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from agent_flow_studio.editor import chats
from agent_flow_studio.editor.server import create_app

PIPELINE = Path(__file__).parent / "fixtures" / "flows" / "pipeline"
H = {"X-Flow-Editor": "1"}
ITEMS = [{"kind": "user", "text": "Add a reviewer", "cmd": "New agent"},
         {"kind": "tool", "name": "set_agent", "arg": "reviewer", "edit": True},
         {"kind": "assistant", "text": "Done."},
         {"kind": "library", "p": {"id": "x", "title": "X"}}]


@pytest.fixture
def client(tmp_path: Path) -> TestClient:
    shutil.copytree(PIPELINE, tmp_path / "flows" / "pipeline")
    return TestClient(create_app(tmp_path / "flows", allowed_hosts=["testserver"]))


def test_store_roundtrip_and_order(tmp_path):
    s = chats.save("pipeline", "aaaaaaaa", {"title": "First", "session_id": "s-1", "cost": 0.1,
                                            "items": ITEMS})
    assert s["messages"] == 1 and (tmp_path / "runs" / "chats" / "pipeline" / "aaaaaaaa.json").is_file()
    created = chats.get("pipeline", "aaaaaaaa")["created"]
    chats.save("pipeline", "bbbbbbbb", {"title": "Second", "items": []})
    chats.save("pipeline", "aaaaaaaa", {"title": "First again", "items": ITEMS[:1]})
    c = chats.get("pipeline", "aaaaaaaa")
    assert c["created"] == created and c["items"][0]["cmd"] == "New agent"  # extra fields kept
    assert [x["id"] for x in chats.list_chats("pipeline")] == ["aaaaaaaa", "bbbbbbbb"]
    assert chats.list_chats("other") == []
    chats.delete("pipeline", "bbbbbbbb")
    with pytest.raises(FileNotFoundError):
        chats.get("pipeline", "bbbbbbbb")


def test_store_rejects_bad_names_and_shapes():
    for flow, cid in (("../x", "aaaaaaaa"), ("pipeline", "../../etc"), ("pipeline", "short")):
        with pytest.raises(chats.ChatError):
            chats.save(flow, cid, {"items": []})
    with pytest.raises(chats.ChatError):
        chats.save("pipeline", "aaaaaaaa", {"items": [{"kind": "system", "text": "x"}]})
    with pytest.raises(chats.ChatError):
        chats.save("pipeline", "aaaaaaaa", {"items": [{"kind": "user", "text": "x" * 2_100_000}]})


def test_api(client: TestClient):
    url = "/api/chats/pipeline/cccccccc"
    assert client.put(url, json={"items": ITEMS}).status_code == 403  # no X-Flow-Editor header
    assert client.put(url, json={"title": "T", "items": ITEMS}, headers=H).status_code == 200
    assert client.get("/api/chats/pipeline").json()[0]["title"] == "T"
    assert client.get(url).json()["items"] == ITEMS
    assert client.get("/api/chats/pipeline/dddddddd").status_code == 404
    assert client.put("/api/chats/pipeline/BAD", json={"items": []}, headers=H).status_code == 422
    assert client.delete(url, headers=H).status_code == 200
    assert client.get("/api/chats/pipeline").json() == []


def test_rename_keeps_the_order():
    chats.save("pipeline", "aaaaaaaa", {"title": "A", "items": ITEMS})
    chats.save("pipeline", "bbbbbbbb", {"title": "B", "items": ITEMS})
    chats.save("pipeline", "aaaaaaaa", {"title": "A renamed", "items": ITEMS})  # same items
    assert [c["title"] for c in chats.list_chats("pipeline")] == ["B", "A renamed"]

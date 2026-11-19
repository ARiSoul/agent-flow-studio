"""Read-only endpoint with the saved values of a run (GET /api/io/{run_id}?flow=...)."""
import shutil
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from agent_flow_studio.builder import load_spec
from agent_flow_studio.editor.server import create_app
from agent_flow_studio.tools import io as io_tool

PARALLEL = Path(__file__).parent / "fixtures" / "flows" / "parallel"


@pytest.fixture
def base(tmp_path: Path) -> Path:
    shutil.copytree(PARALLEL, tmp_path / "flows" / "parallel")
    return tmp_path / "flows"


@pytest.fixture
def client(base: Path) -> TestClient:
    return TestClient(create_app(base, allowed_hosts=["testserver"]))


def test_io_returns_inputs_outputs_gates_and_given(client: TestClient, base: Path):
    spec, _ = load_spec("parallel", base=base)
    rid = "par-001"
    io_tool.save_flow_inputs(rid, {"topic": "caching", "depth": "quick"})
    io_tool.store_output(spec, rid, "planner", "question_a", "What is a cache?")
    io_tool.store_output(spec, rid, "planner", "size", 2)
    io_tool.record_gate(rid, "approve", "approve", "looks good")
    io_tool.save_given(rid, "writer", {"a": "text A"})

    r = client.get(f"/api/io/{rid}", params={"flow": "parallel"})
    assert r.status_code == 200
    body = r.json()
    assert body["inputs"] == {"topic": "caching", "depth": "quick"}
    assert body["outputs"] == {"planner": {"question_a": "What is a cache?", "size": 2}}
    assert body["gates"] == {"approve": {"decision": "approve", "comment": "looks good"}}
    assert body["given"] == {"writer": {"a": "text A"}}


def test_io_of_unknown_run_is_empty(client: TestClient):
    r = client.get("/api/io/nothing-here", params={"flow": "parallel"})
    assert r.status_code == 200
    assert r.json() == {"inputs": {}, "outputs": {}, "gates": {}, "given": {}}


@pytest.mark.parametrize("rid", ["..", "a.b", "x" * 41, "with space"])
def test_io_rejects_bad_run_ids(client: TestClient, rid: str):
    assert client.get(f"/api/io/{rid}", params={"flow": "parallel"}).status_code in (400, 404)


def test_io_needs_a_valid_existing_flow(client: TestClient):
    assert client.get("/api/io/par-001").status_code == 422  # flow is required
    assert client.get("/api/io/par-001", params={"flow": "../x"}).status_code == 400
    assert client.get("/api/io/par-001", params={"flow": "missing"}).status_code == 404

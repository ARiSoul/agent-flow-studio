"""Text and markdown outputs are also files the person can open (runs/<id>/outputs/), the agent is
told where, and the editor can open the run folder."""
import asyncio
import json

import pytest

from agent_flow_studio.builder.spec import FlowSpec
from agent_flow_studio.tools import io as io_tool

SPEC = FlowSpec.model_validate({
    "version": 1, "name": "prompt-maker", "title": "Prompt maker", "execution": "graph",
    "start": "writer",
    "agents": {"writer": {"description": "Writes.", "prompt": "Write.",
                          "outputs": {"final_prompt": {"type": "markdown", "draft": True},
                                      "notes": {"type": "text", "multiple": True},
                                      "data": {"type": "json"}}}},
    "gates": {"end": {"kind": "stop", "label": "End"}}, "edges": [{"from": "writer", "to": "end"}],
})


def test_text_outputs_are_files(tmp_path):
    io_tool.store_output(SPEC, "r1", "writer", "final_prompt", "# The prompt\n\nDo it.")
    io_tool.store_output(SPEC, "r1", "writer", "notes", ["one", "two"])
    io_tool.store_output(SPEC, "r1", "writer", "data", {"a": 1})
    out = io_tool.run_dir("r1") / "outputs"
    assert sorted(p.name for p in out.iterdir()) == ["writer.final_prompt.md", "writer.notes.txt"]
    md = (out / "writer.final_prompt.md").read_text("utf-8")
    assert "# The prompt" in md and "DRAFT" in md.upper()  # the draft marker is kept
    assert (out / "writer.notes.txt").read_text("utf-8") == "one\n\n---\n\ntwo"
    io_tool.clear_outputs("r1", "writer")  # a re-run of the agent starts clean
    assert list(out.iterdir()) == []


def test_set_output_says_where_the_file_is():
    io_tool.configure(SPEC, None)
    try:
        r = asyncio.run(io_tool.set_output.handler(
            {"run_id": "r2", "agent": "writer", "name": "final_prompt", "value": "Hello"}))
        body = json.loads(r["content"][0]["text"])
        assert body["file"] == "runs/r2/outputs/writer.final_prompt.md"
        r = asyncio.run(io_tool.set_output.handler(
            {"run_id": "r2", "agent": "writer", "name": "data", "value": {"a": 1}}))
        assert "file" not in json.loads(r["content"][0]["text"])
    finally:
        io_tool.configure(None, None)


def test_reveal_opens_the_run_folder(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from agent_flow_studio.editor import server

    opened = []
    from agent_flow_studio.editor.api import runs as run_routes
    monkeypatch.setattr(run_routes, "_open_folder", opened.append)  # the runs router opens it
    (tmp_path / "flows").mkdir()
    client = TestClient(server.create_app(tmp_path / "flows", allowed_hosts=["testserver"]))
    h = {"X-Flow-Editor": "1"}
    assert client.post("/api/runs/nothing-here/reveal", headers=h).status_code == 404
    assert client.post("/api/runs/..%2Fx/reveal", headers=h).status_code in (400, 404, 405)
    assert opened == []  # nothing outside runs/ was opened
    io_tool.store_output(SPEC, "r3", "writer", "final_prompt", "x")
    assert client.post("/api/runs/r3/reveal").status_code == 403  # no editor header
    r = client.post("/api/runs/r3/reveal", headers=h)
    assert r.status_code == 200 and opened[-1] == io_tool.run_dir("r3") / "outputs"


@pytest.fixture(autouse=True)
def _no_spec():
    yield
    io_tool.configure(None, None)

"""The files of a run for the editor's previews: listed from files/, outputs/ and inputs/ only;
served with a media type from their bytes (images), never as HTML; nothing outside those folders."""
import shutil
import sys
from pathlib import Path

from fastapi.testclient import TestClient

from agent_flow_studio.editor.server import create_app
from agent_flow_studio.tools import io as io_tool

sys.path.insert(0, str(Path(__file__).parent))
from test_engine import FIX

PNG = b"\x89PNG\r\n\x1a\n" + b"\0" * 32


def test_run_files_are_listed_and_served_safely(tmp_path):
    shutil.copytree(FIX / "parallel", tmp_path / "flows/parallel")
    rd = io_tool.run_dir("rf1")
    (rd / "files/painter").mkdir(parents=True)
    (rd / "files/painter/ladybug.png").write_bytes(PNG)
    (rd / "files/painter/fake.png").write_bytes(b"<html>not an image</html>")
    (rd / "files/painter/icon.svg").write_text("<svg onload='x()'/>", encoding="utf-8")
    (rd / "files/painter/notes.md").write_text("# notes\n", encoding="utf-8")
    (rd / "io").mkdir(exist_ok=True)
    (rd / "io/secret.json").write_text("{}", encoding="utf-8")
    (rd / "events.jsonl").write_text('{"type": "start", "run": "x", "flow": "parallel"}\n', encoding="utf-8")
    c = TestClient(create_app(tmp_path / "flows", allowed_hosts=["testserver"]))
    files = {f["path"]: f["kind"] for f in c.get("/api/runs/rf1/files").json()["files"]}
    assert files == {"files/painter/fake.png": "image", "files/painter/icon.svg": "text",
                     "files/painter/ladybug.png": "image", "files/painter/notes.md": "text"}
    r = c.get("/api/runs/rf1/file", params={"path": "files/painter/ladybug.png"})
    assert r.status_code == 200 and r.headers["content-type"] == "image/png" and r.content == PNG
    assert r.headers["x-content-type-options"] == "nosniff"
    r = c.get("/api/runs/rf1/file", params={"path": "files/painter/fake.png"})
    assert r.headers["content-type"] == "application/octet-stream"  # the bytes decide, not the name
    assert c.get("/api/runs/rf1/file", params={"path": "files/painter/icon.svg"}).headers["content-type"].startswith("text/plain")
    for bad in ("io/secret.json", "events.jsonl", "files/../io/secret.json", "../rf1/io/secret.json", "files/.hidden"):
        assert c.get("/api/runs/rf1/file", params={"path": bad}).status_code == 404, bad

"""The workspace's web app for the agents: the preview URL is local only, a path stays on that
site, and `take` opens the page headless, saves a PNG and returns the console errors (skipped
without Playwright or a Chrome/Edge)."""
import asyncio
import threading
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from agent_flow_studio import workspaces
from agent_flow_studio.builder.catalog import BUILTIN_TOOLS
from agent_flow_studio.tools import preview

PAGE = """<!doctype html><title>game</title><canvas id="c" width="64" height="64"></canvas>
<script>
const gl = document.getElementById('c').getContext('webgl');
if (gl) { gl.clearColor(0.84, 0.16, 0.16, 1); gl.clear(gl.COLOR_BUFFER_BIT); }
document.title = 'ready';
console.error('species ladybug: missing texture');
fetch('https://example.com/track').catch(() => {});
</script>"""


def test_the_preview_url_is_local_and_paths_stay_on_it(monkeypatch):
    with pytest.raises(ValidationError):
        workspaces.Preview(url="https://example.com/game")
    assert workspaces.Preview(url="http://localhost:5173/").url == "http://localhost:5173/"
    spec = SimpleNamespace(preview=workspaces.Preview(url="http://localhost:5173/"))
    monkeypatch.setattr(workspaces, "current", lambda: SimpleNamespace(spec=spec))
    assert preview.preview_url() == "http://localhost:5173/"
    assert preview.preview_url("?species=ladybug") == "http://localhost:5173/?species=ladybug"
    assert preview.preview_url("/level/2") == "http://localhost:5173/level/2"
    for bad in ("https://evil.example/x", "//evil.example/x", "level", "/x y"):
        with pytest.raises(ValueError):
            preview.preview_url(bad)
    monkeypatch.setattr(workspaces, "current", lambda: SimpleNamespace(spec=None))
    with pytest.raises(ValueError, match="no preview URL"):
        preview.preview_url()


def test_the_tool_is_in_the_catalog_and_only_writes_in_the_run():
    assert BUILTIN_TOOLS["preview.screenshot"].kind == "write_local"


def test_a_screenshot_of_a_local_page(tmp_path: Path):
    pytest.importorskip("playwright")
    (tmp_path / "index.html").write_text(PAGE, encoding="utf-8")

    class Quiet(SimpleHTTPRequestHandler):
        def __init__(self, *a, **k):
            super().__init__(*a, directory=str(tmp_path), **k)

        def log_message(self, *a):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Quiet)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    out = tmp_path / "shot.png"
    try:
        errors = asyncio.run(preview.take(f"http://127.0.0.1:{server.server_address[1]}/", out,
                                          wait_ms=200, selector="canvas", width=320, height=240))
    except ValueError as e:
        if "no Chrome or Edge" in str(e):
            pytest.skip(str(e))
        raise
    finally:
        server.shutdown()
    assert out.read_bytes().startswith(b"\x89PNG")
    assert any("missing texture" in x for x in errors)

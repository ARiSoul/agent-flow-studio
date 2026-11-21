"""Testing one action from the Connectors window, and what a failed website action says: the
step, the page, and the requests a read action blocked. No real site: a local one, and Playwright
with the installed Edge (that part is skipped without them)."""
import asyncio
import http.server
import threading

import pytest
from fastapi.testclient import TestClient

from agent_flow_studio.connections import actions, store, web
from agent_flow_studio.connections.model import Connection
from agent_flow_studio.editor.server import create_app

H = {"X-Flow-Editor": "1"}
# the list only appears after a POST that loads the data (as some single-page apps do)
PAGE = """<div id=me>me</div><div id=list></div><script>
fetch('/api/data', {method: 'POST'}).then(r => r.text()).then(t => {
  document.getElementById('list').innerHTML = '<span id=count>' + t + '</span>';
});
</script>"""


def site(port: int, **action) -> Connection:
    return Connection.model_validate({
        "type": "website", "base_url": f"http://127.0.0.1:{port}", "auth": {"method": "browser_session"},
        "web": {"timeout_s": 5},
        "actions": {"count": {"description": "Counts.", "steps": [{"goto": "/list"},
                                                                  {"extract": {"n": "#count"}}],
                              **action}}})


@pytest.fixture
def local_site():
    class H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            body = PAGE.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self):
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            self.wfile.write(b"42")

        def log_message(self, *a):
            pass

    srv = http.server.HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield srv.server_port
    srv.shutdown()


def test_a_failed_step_says_where_and_what_was_blocked(tmp_path, monkeypatch, local_site):
    pytest.importorskip("playwright")
    monkeypatch.setenv("AFS_BROWSER_DIR", str(tmp_path / "browser"))
    (tmp_path / "browser" / "portal").mkdir(parents=True)  # as after a sign-in
    c = site(local_site)
    try:
        with pytest.raises(web.WebError) as e:
            asyncio.run(web.run_action("portal", c, "count", c.actions["count"], {}))
    except web.WebError as x:  # pragma: no cover — no browser here
        pytest.skip(f"no browser for Playwright here: {x}")
    text = str(e.value)
    if "Executable doesn't exist" in text or "channel" in text.lower():
        pytest.skip(f"no browser for Playwright here: {text}")
    assert "portal.count: step 2 (extract #count) on /list, window 1280x720: TimeoutError" in text
    assert "blocked POST /api/data" in text and "allow_post: true" in text
    assert e.value.blocked == ["POST /api/data"]
    # with allow_post the page loads its data and the action works
    c = site(local_site, allow_post=True)
    r = asyncio.run(web.run_action("portal", c, "count", c.actions["count"], {}))
    assert r["values"] == {"n": "42"} and "blocked" not in r
    # the window size of the action: the layout it was recorded at
    narrow = site(local_site, allow_post=True, viewport={"width": 400, "height": 700},
                  steps=[{"goto": "/list"}, {"extract": {"n": "#nope"}}])
    with pytest.raises(web.WebError) as e:
        asyncio.run(web.run_action("portal", narrow, "count", narrow.actions["count"], {}))
    assert "window 400x700" in str(e.value) and "never appeared" not in str(e.value)
    wide = site(local_site, allow_post=True, steps=[{"goto": "/list"}, {"extract": {"n": "#nope"}}])
    with pytest.raises(web.WebError) as e:
        asyncio.run(web.run_action("portal", wide, "count", wide.actions["count"], {}))
    assert "the element never appeared" in str(e.value) and "viewport" in str(e.value)


def test_step_text():
    assert web.step_text(3, "fill", {"selector": "#q", "value": "x"}) == "step 3 (fill #q)"
    assert web.step_text(1, "goto", "/a  b") == "step 1 (goto /a b)"


def test_test_endpoint(tmp_path, monkeypatch):
    monkeypatch.setenv("CONNECTIONS_FILE", str(tmp_path / "connections.yaml"))
    monkeypatch.setenv("AFS_PUBLIC_CONNECTIONS_FILE", str(tmp_path / "connections.public.yaml"))
    store.save({
        "site": site(1234),
        "api": Connection.model_validate({
            "type": "http_api", "base_url": "https://api.example.com", "access": "write",
            "actions": {"get": {"description": "Reads.", "path": "/x"},
                        "add": {"description": "Writes.", "access": "write", "method": "POST",
                                "path": "/x"}}}),
    })
    calls = []

    async def fake_run(name, action, params=None, conns=None, *, visible=False):
        calls.append((name, action, params, visible))
        if action == "get":
            raise actions.ActionError("get: required")
        return {"action": action, "values": {"n": "42"}}

    monkeypatch.setattr(actions, "run", fake_run)
    (tmp_path / "flows").mkdir()
    client = TestClient(create_app(tmp_path / "flows", allowed_hosts=["testserver"]))
    url = "/api/connections/{}/actions/{}/test"
    assert client.post(url.format("site", "count"), json={}).status_code == 403  # no header
    r = client.post(url.format("site", "count"), json={"params": {"a": 1}}, headers=H)
    assert r.json() == {"ok": True, "result": {"action": "count", "values": {"n": "42"}}}
    assert calls[-1] == ("site", "count", {"a": 1}, True)  # a website test is visible
    r = client.post(url.format("api", "get"), json={}, headers=H)
    assert r.json() == {"ok": False, "error": "get: required", "blocked": []}
    assert calls[-1][3] is False
    assert client.post(url.format("api", "add"), json={}, headers=H).status_code == 400
    assert client.post(url.format("api", "nope"), json={}, headers=H).status_code == 404
    assert client.post(url.format("nobody", "x"), json={}, headers=H).status_code == 404
    # the last test of each action stays (to look at it later); it goes with the connector
    tests = client.get("/api/connections/site/tests").json()["tests"]
    assert tests["count"]["ok"] and tests["count"]["result"]["values"] == {"n": "42"}
    assert tests["count"]["params"] == {"a": 1} and tests["count"]["ts"] > 0
    assert client.get("/api/connections/api/tests").json()["tests"]["get"]["error"] == "get: required"
    assert client.delete("/api/connections/site", headers=H).status_code == 200
    assert client.get("/api/connections/site/tests").json() == {"tests": {}}


def test_allow_post_endpoint(tmp_path, monkeypatch):
    monkeypatch.setenv("CONNECTIONS_FILE", str(tmp_path / "connections.yaml"))
    monkeypatch.setenv("AFS_PUBLIC_CONNECTIONS_FILE", str(tmp_path / "connections.public.yaml"))
    store.save({"site": site(1234), "api": Connection.model_validate({
        "type": "http_api", "base_url": "https://api.example.com",
        "actions": {"get": {"description": "Reads.", "path": "/x"}}})})
    (tmp_path / "flows").mkdir()
    client = TestClient(create_app(tmp_path / "flows", allowed_hosts=["testserver"]))
    url = "/api/connections/{}/actions/{}/allow-post"
    assert client.post(url.format("site", "count"), json={}, headers=H).json() == {"allow_post": True}
    assert store.load()["site"].actions["count"].allow_post is True
    client.post(url.format("site", "count"), json={"allow": False}, headers=H)
    assert store.load()["site"].actions["count"].allow_post is False
    assert client.post(url.format("api", "get"), json={}, headers=H).status_code == 400
    assert client.post(url.format("site", "nope"), json={}, headers=H).status_code == 404


def test_steps_use_the_first_visible_match(tmp_path, monkeypatch):
    """A page with a hidden copy of its menu (the phone layout) before the visible one: the
    click goes to the visible link, not to the hidden first match."""
    pytest.importorskip("playwright")
    pages = {"/": "<nav style='display:none'><a href='/wrong'>More</a></nav>"
                  "<aside><a href='/users'>More</a></aside>",
             "/users": "<span class=total>42</span>", "/wrong": "<span class=total>0</span>"}

    class Site(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            body = pages.get(self.path, "").encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass

    srv = http.server.HTTPServer(("127.0.0.1", 0), Site)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    monkeypatch.setenv("AFS_BROWSER_DIR", str(tmp_path / "browser"))
    (tmp_path / "browser" / "portal").mkdir(parents=True)
    c = site(srv.server_port, steps=[{"goto": "/"}, {"click": "a:text-is(\"More\")"},
                                     {"extract": {"users": "span.total"}}])
    try:
        r = asyncio.run(web.run_action("portal", c, "count", c.actions["count"], {}))
    except web.WebError as e:
        if "Executable doesn't exist" in str(e) or "channel" in str(e).lower():
            pytest.skip(f"no browser for Playwright here: {e}")
        raise
    finally:
        srv.shutdown()
    assert r["values"] == {"users": "42"} and r["url"].endswith("/users")


def test_playwright_errors_keep_their_reasons():
    log = ("Locator.click: Timeout 30000ms exceeded.\nCall log:\n"
           "  - waiting for locator(\"a:text-is(\\\"Mais\\\")\").first\n"
           "    - locator resolved to <a href=\"#m\">Mais</a>\n  - attempting click action\n"
           "    2 \u00d7 waiting for element to be visible, enabled and stable\n"
           "      - element is not visible\n    - retrying click action\n      - waiting 100ms\n"
           "    6 \u00d7 waiting for element to be visible, enabled and stable\n"
           "      - element is not visible\n")
    out = web._pw_error(Exception(log))
    assert out.startswith("Locator.click: Timeout 30000ms exceeded.")
    assert "locator resolved to <a href=\"#m\">Mais</a>" in out and "element is not visible" in out
    assert "retrying" not in out and out.count("element is not visible") == 1


def _serve(pages: dict[str, str]):
    class Site(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            body = pages.get(self.path.split("?")[0], "").encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass

    srv = http.server.HTTPServer(("127.0.0.1", 0), Site)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def _run(c: Connection):
    try:
        return asyncio.run(web.run_action("portal", c, "count", c.actions["count"], {}))
    except web.WebError as e:
        if "Executable doesn't exist" in str(e) or "channel" in str(e).lower():
            pytest.skip(f"no browser for Playwright here: {e}")
        raise


def test_a_link_with_hidden_text_inside_is_found_by_its_visible_text(tmp_path, monkeypatch):
    """The recorder reads the visible text ("More"); :text-is also counts hidden text inside the
    link (a counter badge "0" drawn by the page later), so the step looks at the visible text."""
    pytest.importorskip("playwright")
    srv = _serve({"/": "<aside><a href='/users'>More<span style='visibility:hidden'>0</span></a>"
                       "</aside>", "/users": "<span class=total>42</span>"})
    monkeypatch.setenv("AFS_BROWSER_DIR", str(tmp_path / "browser"))
    (tmp_path / "browser" / "portal").mkdir(parents=True)
    try:
        c = site(srv.server_port, steps=[{"goto": "/"}, {"click": "a:text-is(\"More\")"},
                                         {"extract": {"users": "span.total"}}])
        assert _run(c)["values"] == {"users": "42"}
    finally:
        srv.shutdown()


def test_the_action_runs_in_its_language(tmp_path, monkeypatch):
    """A multilingual site: the texts follow the browser language, and the steps find links by
    their texts; the action keeps the language it was recorded in."""
    pytest.importorskip("playwright")
    home = ("<div id=nav></div><script>document.getElementById('nav').innerHTML = "
            "navigator.language.startsWith('pt') ? \"<a href='/users'>Mais</a>\" "
            ": \"<a href='/users'>More</a>\";</script>")
    srv = _serve({"/": home, "/users": "<span class=total>42</span>"})
    monkeypatch.setenv("AFS_BROWSER_DIR", str(tmp_path / "browser"))
    (tmp_path / "browser" / "portal").mkdir(parents=True)
    steps = [{"goto": "/"}, {"click": "a:text-is(\"Mais\")"}, {"extract": {"users": "span.total"}}]
    try:
        assert _run(site(srv.server_port, locale="pt-PT", steps=steps))["values"] == {"users": "42"}
        with pytest.raises(web.WebError, match="step 2"):
            _run(site(srv.server_port, locale="en-US", steps=steps))
    finally:
        srv.shutdown()

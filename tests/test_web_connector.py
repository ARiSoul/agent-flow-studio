"""Website connectors: model rules and the step runner (with a fake browser; Playwright optional)."""
import asyncio

import pytest
from pydantic import ValidationError

from agent_flow_studio.builder import FlowError, compile_flow
from agent_flow_studio.builder.spec import FlowSpec
from agent_flow_studio.connections import actions, client, store, web
from agent_flow_studio.connections.model import Connection

SITE = {"type": "website", "base_url": "https://portal.example.com",
        "auth": {"method": "browser_session"}, "allowed_paths": ["/ticket", "/users", "/"],
        "web": {"signed_in_selector": "#me", "signed_out_url": "/login"},
        "actions": {"open_ticket": {
            "description": "Opens a ticket.", "params": {"number": {"pattern": "INC[0-9]{4}"}},
            "steps": [{"goto": "/ticket/{number}"}, {"click": "text=Notes"},
                      {"extract": {"summary": "#summary"}}, {"extract_all": {"notes": ".note"}}]},
            "search_user": {
            "description": "Finds a user.", "params": {"email": {}},
            "steps": [{"goto": "/users"}, {"fill": {"selector": "input[name=q]", "value": "{email}"}},
                      {"press": {"selector": "input[name=q]", "key": "Enter"}},
                      {"wait_for": "table"}, {"extract_table": "table"}]}}}


class FakePage:
    """A tiny site: pages by path; records what the steps did."""

    def __init__(self, signed_in=True, redirect_to=None, click_to=None):
        self.calls, self._url = [], "about:blank"
        self.signed_in, self.redirect_to, self.click_to = signed_in, redirect_to, click_to

    async def goto(self, url):
        self.calls.append(("goto", url))
        self._url = self.redirect_to or url

    async def click(self, s):
        self.calls.append(("click", s))
        if self.click_to:
            self._url = self.click_to

    async def fill(self, s, v):
        self.calls.append(("fill", s, v))

    async def select(self, s, v):
        self.calls.append(("select", s, v))

    async def press(self, s, k):
        self.calls.append(("press", s, k))

    async def wait_for(self, s):
        self.calls.append(("wait_for", s))

    async def exists(self, s):
        return self.signed_in

    async def text(self, s):
        return f"text of {s}"

    async def texts(self, s):
        return ["a", "b"]

    async def table(self, s):
        return [["Name", "Email"], ["Ana", "ana@example.com"]]

    def url(self):
        return self._url


def conn(**patch) -> Connection:
    return Connection.model_validate(SITE | patch)


def steps(c: Connection, action: str, params: dict, page: FakePage):
    a = c.actions[action]
    return asyncio.run(web.run_steps(c, a, actions.check_params(a, params), page))


@pytest.mark.parametrize("patch,needle", [
    ({"base_url": None}, "needs 'base_url'"),
    ({"base_url": "http://portal.example.com"}, "https://"),
    ({"actions": {"x": {"description": "d"}}}, "needs 'steps'"),
    ({"actions": {"x": {"description": "d", "steps": [{"jump": "/"}]}}}, "unknown step"),
    ({"actions": {"x": {"description": "d", "steps": [{"goto": "https://evil.example"}]}}},
     "relative to the base URL"),
    ({"actions": {"x": {"description": "d", "steps": [{"fill": {"selector": "#a"}}]}}},
     "needs selector, value"),
    ({"actions": {"x": {"description": "d", "path": "/a", "steps": [{"goto": "/"}]}}},
     "use steps, not path"),
    ({"web": {"login_url": "http://x"}}, "login_url"),
])
def test_website_rules(patch, needle):
    with pytest.raises(ValidationError, match=needle):
        Connection.model_validate(SITE | patch)


def test_local_http_site_is_allowed():
    assert conn(base_url="http://127.0.0.1:8899").resolved_base_url() == "http://127.0.0.1:8899"


def test_steps_run_and_extract():
    page = FakePage()
    r = steps(conn(), "open_ticket", {"number": "INC0042"}, page)
    assert page.calls[:2] == [("goto", "https://portal.example.com/ticket/INC0042"),
                              ("click", "text=Notes")]
    assert r["values"] == {"summary": "text of #summary", "notes": ["a", "b"]}
    page = FakePage()
    r = steps(conn(), "search_user", {"email": "ana@example.com"}, page)
    assert ("fill", "input[name=q]", "ana@example.com") in page.calls
    assert r["values"]["table"][1] == ["Ana", "ana@example.com"]


def test_signed_out_and_leaving_the_site():
    with pytest.raises(web.SignedOut, match="signed-in element"):
        steps(conn(), "open_ticket", {"number": "INC0042"}, FakePage(signed_in=False))
    with pytest.raises(web.SignedOut, match="asks to sign in"):
        steps(conn(), "open_ticket", {"number": "INC0042"},
              FakePage(redirect_to="https://portal.example.com/login?next=/ticket"))
    with pytest.raises(web.SignedOut, match="login.example.net"):
        steps(conn(), "open_ticket", {"number": "INC0042"},
              FakePage(redirect_to="https://login.example.net/sso"))
    with pytest.raises(web.WebError, match="left the allowed site"):
        steps(conn(), "open_ticket", {"number": "INC0042"},
              FakePage(click_to="https://other.example.org/x"))
    with pytest.raises(web.WebError, match="left the allowed"):
        steps(conn(allowed_paths=["/ticket"]), "open_ticket", {"number": "INC0042"},
              FakePage(click_to="https://portal.example.com/admin"))


def test_no_profile_means_sign_in_first(tmp_path, monkeypatch):
    monkeypatch.setenv("AFS_BROWSER_DIR", str(tmp_path / "browser"))
    c = conn()
    with pytest.raises(web.SignedOut, match="Sign in"):
        asyncio.run(web.run_action("portal", c, "open_ticket", c.actions["open_ticket"],
                                   {"number": "INC0001"}))
    assert asyncio.run(web.check_session("portal", c))["ok"] is False


def test_http_tools_refuse_websites_and_compile_rules(tmp_path, monkeypatch):
    monkeypatch.setenv("CONNECTIONS_FILE", str(tmp_path / "connections.yaml"))
    store.save({"portal": conn(), "empty": conn(actions={})})
    with pytest.raises(client.ConnectorError, match="is a website"):
        asyncio.run(client.get("portal", "/ticket"))
    flow = {"name": "mini", "title": "Mini", "task": "Do {input}.", "start": "a",
            "agents": {"a": {"description": "reads", "prompt": "Read.", "connections": ["portal"]}}}
    cf = compile_flow(FlowSpec.model_validate(flow), tmp_path)
    assert "mcp__connections__run_action" in cf.agent_tools["a"]
    assert "portal" in cf.policy.actions_only  # a website is only used through its actions
    assert "`open_ticket`(number: text)" in cf.options.agents["a"].prompt
    flow["agents"]["a"]["connections"] = ["empty"]
    with pytest.raises(FlowError, match="has no actions yet"):
        compile_flow(FlowSpec.model_validate(flow), tmp_path)


def test_playwright_missing_is_a_clear_error(monkeypatch):
    import builtins
    real = builtins.__import__

    def fake(name, *a, **k):
        if name.startswith("playwright"):
            raise ImportError(name)
        return real(name, *a, **k)

    monkeypatch.setattr(builtins, "__import__", fake)
    with pytest.raises(web.WebError, match=r"pip install -e \"\.\[web\]\""):
        web._playwright()


def test_real_browser_reads_a_local_site(tmp_path, monkeypatch):
    """End to end with Playwright and the installed Edge, on a local site (skipped without it)."""
    pytest.importorskip("playwright")
    import http.server
    import threading

    pages = {"/ticket/INC0007": "<div id=me>me</div><h1 id=summary>Printer down</h1>"
                                "<div class=note>n1</div><div class=note>n2</div>"
                                "<button onclick=\"fetch('/x',{method:'POST'})\">Notes</button>"}
    posts = []

    class H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            body = pages.get(self.path, "<div id=me></div>").encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self):
            posts.append(self.path)
            self.send_response(204)
            self.end_headers()

        def log_message(self, *a):
            pass

    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    monkeypatch.setenv("AFS_BROWSER_DIR", str(tmp_path / "browser"))
    (tmp_path / "browser" / "portal").mkdir(parents=True)  # as after a sign-in
    c = conn(base_url=f"http://127.0.0.1:{srv.server_port}")
    try:
        r = asyncio.run(web.run_action("portal", c, "open_ticket", c.actions["open_ticket"],
                                       {"number": "INC0007"}))
    except web.WebError as e:
        if "Executable doesn't exist" in str(e) or "channel" in str(e).lower():
            pytest.skip(f"no browser for Playwright here: {e}")
        raise
    finally:
        srv.shutdown()
    assert r["values"] == {"summary": "Printer down", "notes": ["n1", "n2"]}
    assert posts == []  # the click tried a POST: a read action blocks it


class JsonPage(FakePage):
    def __init__(self, status=200, final=None, ctype="application/json", text=""):
        super().__init__()
        self.answer = (status, final, ctype, text)

    async def get(self, url):
        self.calls.append(("get", url))
        status, final, ctype, text = self.answer
        return status, final or url, ctype, text


TICKET_JSONP = ('callback({"id": 123456, "title": "Printer down", "state": "Open", '
                '"requester_email": "someone@example.com", "requester_tax_id": "123456789", '
                '"journal": [{"creationDate": "2026-11-20", "user": {"name": "Ana"}, '
                '"value": "<p>Checked the <b>driver</b></p><p>Waiting</p>"}], '
                '"description": [{"value": "Cannot print &amp; scan"}]});')


def json_conn() -> Connection:
    return Connection.model_validate(SITE | {
        "allowed_paths": ["/api/tickets/", "/api/alerts"],
        "web": {"signed_out_url": "login.html", "check_path": "/api/alerts"},
        "actions": {"get_ticket": {"description": "Reads a ticket.",
                                   "params": {"number": {"pattern": "[0-9]{6}"}},
                                   "steps": [{"get_json": {
                                       "path": "/api/tickets/{number}",
                                       "fields": {"number": "id", "title": "title",
                                                  "status": "state",
                                                  "description": "description[].value",
                                                  "notes": "journal[].{creationDate,user.name,value}"}}}]}}})


def test_get_json_keeps_only_the_listed_fields():
    c = json_conn()
    page = JsonPage(text=TICKET_JSONP)
    r = steps(c, "get_ticket", {"number": "123456"}, page)
    assert page.calls == [("get", "https://portal.example.com/api/tickets/123456")]
    v = r["values"]
    assert v["number"] == 123456 and v["title"] == "Printer down" and v["status"] == "Open"
    assert v["description"] == ["Cannot print & scan"]
    assert v["notes"] == [{"creationDate": "2026-11-20", "user.name": "Ana",
                           "value": "Checked the driver\nWaiting"}]
    assert "someone@example.com" not in str(r) and "123456789" not in str(r)  # not listed: not sent
    assert r["url"] is None  # no page was opened


def test_get_json_detects_the_sign_in_page():
    c = json_conn()
    for page in (JsonPage(final="https://portal.example.com/login.html"),
                 JsonPage(ctype="text/html; charset=utf-8", text="<html>login</html>"),
                 JsonPage(status=401)):
        with pytest.raises(web.SignedOut):
            steps(c, "get_ticket", {"number": "123456"}, page)
    with pytest.raises(web.WebError, match="HTTP 500"):
        steps(c, "get_ticket", {"number": "123456"}, JsonPage(status=500))
    with pytest.raises(actions.ActionError, match="format"):
        steps(c, "get_ticket", {"number": "12345"}, JsonPage(text=TICKET_JSONP))


def test_get_json_rules():
    bad = {"get_json": {"path": "/api/x", "fields": {"a": "b..c"}}}
    with pytest.raises(ValidationError, match="fields = name"):
        Connection.model_validate(SITE | {"actions": {"x": {"description": "d", "steps": [bad]}}})
    with pytest.raises(ValidationError, match="check_path"):
        Connection.model_validate(SITE | {"web": {"check_path": "https://x"}})

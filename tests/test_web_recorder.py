"""Recording the steps of a website action: what the page reports becomes a draft action that
passes the same rules as hand-written ones (typed values become params, passwords are skipped)."""
import time

import pytest
import yaml

from agent_flow_studio.connections import recorder
from agent_flow_studio.connections.model import Action, Connection
from agent_flow_studio.connections.recorder import Recording, recorder_js


def conn(**patch) -> Connection:
    return Connection.model_validate({"type": "website", "base_url": "https://tickets.example.com/app",
                                      "allowed_paths": ["/ticket", "/search"],
                                      "auth": {"method": "browser_session"}, **patch})


def started(**patch) -> Recording:
    rec = Recording(conn(**patch))
    rec.begin("/ticket")
    rec._t_start -= 60  # past the redirects of the first page
    return rec


def test_clicks_fields_and_extracts_become_steps():
    rec = started()
    rec.add({"kind": "fill", "selector": "input[name=\"q\"]", "name": "Ticket number"})
    rec.add({"kind": "press", "selector": "input[name=\"q\"]", "key": "Enter"})
    rec.add({"kind": "fill", "selector": "input[name=\"q\"]", "name": "Ticket number"})  # change
    rec.add({"kind": "click", "selector": "a:text-is(\"Notes\")"})
    rec.add({"kind": "select", "selector": "#status", "value": "open"})
    rec.add({"kind": "extract", "selector": "#summary", "name": "summary"})
    rec.add({"kind": "extract", "selector": "#notes", "name": "summary"})
    assert rec.steps == [
        {"goto": "/ticket"},
        {"fill": {"selector": "input[name=\"q\"]", "value": "{ticket_number}"}},
        {"press": {"selector": "input[name=\"q\"]", "key": "Enter"}},
        {"click": "a:text-is(\"Notes\")"},
        {"select": {"selector": "#status", "value": "open"}},
        {"extract": {"summary": "#summary", "summary_2": "#notes"}},
    ]
    assert rec.params == {"ticket_number": {"description": "text for the field 'Ticket number'"}}
    rec.done = True
    assert rec.problems() == []
    draft = yaml.safe_load(rec.yaml("open_ticket"))["open_ticket"]
    a = Action.model_validate(draft)  # the draft is a valid action
    assert a.access == "read" and a.params["ticket_number"].required


def test_typed_values_are_params_or_chosen_fixed_values_and_passwords_never_kept():
    rec = started()
    rec.add({"kind": "fill", "selector": "#pw", "name": "Password", "secret": True,
             "value": "hunter2"})
    rec.add({"kind": "fill", "selector": "#q", "name": "Pages, actions, help…", "value": "users"})
    rec.add({"kind": "click", "selector": "#search-option-1"})
    rec.done = True
    assert "hunter2" not in rec.yaml("x") + str(rec.summary())  # a password never leaves
    assert [w.code for w in rec.warnings] == ["web.record_password", "web.record_no_extract"]
    # the typed text goes to the editor (local) to choose; the draft has a param by default
    assert rec.summary()["typed"] == {"pages_actions_help": {"label": "Pages, actions, help…",
                                                             "value": "users"}}
    draft = yaml.safe_load(rec.yaml("x"))["x"]
    assert "users" not in rec.yaml("x") and draft["params"] == {
        "pages_actions_help": {"description": "text for the field 'Pages, actions, help…'"}}
    assert draft["steps"][1] == {"fill": {"selector": "#q", "value": "{pages_actions_help}"}}
    # "fixed value": the action always types what was typed, and the param is gone
    fixed = yaml.safe_load(rec.yaml("x", {"pages_actions_help", "unknown"}))["x"]
    assert "params" not in fixed and fixed["steps"][1] == {"fill": {"selector": "#q",
                                                                    "value": "users"}}
    Action.model_validate(fixed)
    rec.typed["pages_actions_help"]["value"] = "a {b} c"  # braces would read as a {param}
    assert yaml.safe_load(rec.yaml("x", {"pages_actions_help"}))["x"]["steps"][1]["fill"][
        "value"] == "a (b) c"


def test_navigation_rules():
    rec = started()
    rec.navigated("https://tickets.example.com/app/search?q=a%7Bb")  # typed in the address bar
    assert rec.steps[-1] == {"goto": "/search?q=a%7Bb"}
    rec.add({"kind": "click", "selector": "#next"})
    rec.navigated("https://tickets.example.com/app/ticket/2")  # caused by the click: not recorded
    assert rec.steps[-1] == {"click": "#next"}
    rec._t_event = time.monotonic() - 60
    rec.navigated("https://tickets.example.com/app/search/{x}")
    assert rec.steps[-1] == {"goto": "/search/%7Bx%7D"}  # braces are not placeholders
    rec.navigated("https://sso.example.org/login")
    rec.navigated("https://tickets.example.com/app/admin")
    assert [w.code for w in rec.warnings] == ["web.record_left_site"] * 2
    assert rec.steps[-1] == {"goto": "/search/%7Bx%7D"}


def test_posts_new_tabs_limits_and_bad_events():
    rec = started()
    rec.request("POST", "https://tickets.example.com/app/api/search")
    rec.request("POST", "https://telemetry.example.net/collect")  # another site: not the action's
    rec.request("GET", "https://tickets.example.com/app/ticket/1")
    rec.new_tab()
    assert [w.code for w in rec.warnings] == ["web.record_post", "web.record_new_tab"]
    for bad in (None, "x", {"kind": "click"}, {"kind": "click", "selector": "a" * 600},
                {"kind": "eval", "selector": "#x"}, {"kind": "press", "selector": "#x", "key": "a"}):
        rec.add(bad)
    assert rec.steps == [{"goto": "/ticket"}]
    for i in range(recorder.MAX_STEPS + 5):
        rec.add({"kind": "click", "selector": f"#b{i}"})
    assert len(rec.steps) == recorder.MAX_STEPS
    assert rec.warnings[-1].code == "web.record_limit"
    rec.done = True
    rec.add({"kind": "click", "selector": "#late"})
    assert len(rec.steps) == recorder.MAX_STEPS


def test_summary_and_script():
    rec = started()
    s = rec.summary()
    assert s["recording"] and s["steps"] == [{"goto": "/ticket"}] and s["problems"] == []
    rec.request("POST", "https://tickets.example.com/app/x")
    w = rec.summary()["warnings"][0]
    assert w["code"] == "web.record_post" and w["params"] == {"method": "POST"}
    js = recorder_js('Gravar: "aspas" </script>')
    assert '"Gravar: \\"aspas\\" </script>"' in js and "__BANNER__" not in js
    assert "Close this window" in recorder_js("  ")


def test_record_routes(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from agent_flow_studio.connections import store, web
    from agent_flow_studio.editor.server import create_app

    monkeypatch.setenv("CONNECTIONS_FILE", str(tmp_path / "connections.yaml"))
    monkeypatch.setenv("AFS_PUBLIC_CONNECTIONS_FILE", str(tmp_path / "connections.public.yaml"))
    store.save({"site": conn(), "api": Connection.model_validate(
        {"type": "http_api", "base_url": "https://api.example.com"})})
    (tmp_path / "flows").mkdir()
    client = TestClient(create_app(tmp_path / "flows", allowed_hosts=["testserver"]))
    h = {"X-Flow-Editor": "1"}
    assert client.get("/api/connections/site/browser/record").status_code == 404
    assert client.post("/api/connections/api/browser/record", json={}, headers=h).status_code == 400

    def no_playwright():
        raise web.WebError("website connectors need Playwright")
    monkeypatch.setattr(web, "_playwright", no_playwright)
    r = client.post("/api/connections/site/browser/record", json={}, headers=h)
    assert r.status_code == 400 and "Playwright" in r.json()["detail"]
    assert client.delete("/api/connections/site/browser/record", headers=h).json() == \
        {"recording": False}


@pytest.mark.parametrize("name, expected", [("Ticket number", "ticket_number"), ("123 abc", "abc"),
                                            ("", "value"), ("é", "e"), ("…", "value"), ("a" * 60, "a" * 40),
                                            ("Páginas, ações, ajuda…", "paginas_acoes_ajuda")])
def test_param_names(name, expected):
    assert recorder._slug(name, "value") == expected


def test_real_browser_recording(tmp_path, monkeypatch):
    """The page script, the binding and the navigation events with Playwright and the installed
    Edge, headless (skipped without them): a person's clicks become steps."""
    pytest.importorskip("playwright")
    import asyncio
    import http.server
    import threading

    page_html = ("<form action=/search><label for=q>Ticket number</label><input id=q name=q>"
                 "<input type=password name=pw><button>Search</button></form>"
                 "<a href=/ticket/1 role=tab>Notes</a><div id=summary>Printer down</div>")

    class H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            body = page_html.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass

    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    monkeypatch.setenv("AFS_BROWSER_DIR", str(tmp_path / "browser"))
    c = conn(base_url=f"http://127.0.0.1:{srv.server_port}", allowed_paths=[])
    rec = Recording(c)

    async def person() -> None:
        task = asyncio.create_task(recorder.record("portal", c, rec, "rec", headless=True))
        for _ in range(200):
            if rec.page is not None or rec.done:
                break
            await asyncio.sleep(0.05)
        assert rec.page is not None, rec.error
        p = rec.page
        await p.wait_for_load_state()
        rec._t_start -= 60  # the first page has loaded: later navigations are the person's
        await p.fill("input[name=pw]", "not-a-real-one")
        await p.dispatch_event("input[name=pw]", "change")
        await p.click("#q")
        await p.keyboard.type("INC0001")
        await p.keyboard.press("Enter")  # submits the form: /search?q=…
        await p.wait_for_url("**/search*")
        await p.click("text=Notes")
        await p.wait_for_url("**/ticket/1")
        await p.click("#summary", modifiers=["Alt"])
        await asyncio.sleep(0.3)
        rec.stop.set()
        await asyncio.wait_for(task, 30)

    try:
        asyncio.run(person())
    finally:
        srv.shutdown()
    if rec.error and ("Executable doesn't exist" in rec.error or "channel" in rec.error.lower()):
        pytest.skip(f"no browser for Playwright here: {rec.error}")
    assert rec.error is None and rec.done
    assert rec.steps == [
        {"goto": "/"},
        {"fill": {"selector": "#q", "value": "{ticket_number}"}},
        {"press": {"selector": "#q", "key": "Enter"}},
        {"goto": "/ticket/1"},  # a link: its address, not its text
        {"extract": {"summary": "#summary"}},
    ]
    assert [w.code for w in rec.warnings] == ["web.record_password"]
    assert "INC0001" not in rec.yaml("x") and "not-a-real-one" not in rec.yaml("x")


def test_a_sign_in_during_the_recording_is_left_out():
    """Recorded on a site that was not signed in yet: the action runs with the saved session, so
    the clicks that led to the sign-in page (and on it) are not steps."""
    rec = started(web={"login_url": "/auth/login"}, allowed_paths=[])
    rec.add({"kind": "click", "selector": "a:text-is(\"Sign in\")"})
    rec.navigated("https://tickets.example.com/app/auth/login?next=/")
    rec.add({"kind": "click", "selector": "button:text-is(\"Sign in with a passkey\")"})  # ignored
    rec._t_event = time.monotonic() - 60
    rec.navigated("https://tickets.example.com/app/")  # back after the sign-in: a redirect
    rec.add({"kind": "fill", "selector": "#search", "name": "Search"})
    rec.add({"kind": "extract", "selector": "#count", "name": "count"})
    assert rec.steps == [{"goto": "/ticket"},
                         {"fill": {"selector": "#search", "value": "{search}"}},
                         {"extract": {"count": "#count"}}]
    assert [w.code for w in rec.warnings] == ["web.record_signed_in"]
    rec2 = started(web={"login_url": "https://sso.example.org/login"}, allowed_paths=[])
    rec2.navigated("https://sso.example.org/login?app=1")
    rec2.add({"kind": "click", "selector": "#me"})
    assert rec2.steps == [{"goto": "/ticket"}] and rec2.warnings[0].code == "web.record_signed_in"


def test_a_draft_without_extract_says_so():
    rec = started()
    rec.add({"kind": "click", "selector": "#go"})
    rec.done = True
    assert [w["code"] for w in rec.summary()["warnings"]] == ["web.record_no_extract"]
    rec = started()
    rec.add({"kind": "extract", "selector": "#x", "name": "x"})
    rec.done = True
    assert rec.summary()["warnings"] == []


def test_posted_pages_can_get_allow_post():
    rec = started()
    rec.add({"kind": "extract", "selector": "#n", "name": "n"})
    assert rec.summary()["posted"] is False
    rec.request("POST", "https://tickets.example.com/app/hub/negotiate")
    rec.done = True
    assert rec.summary()["posted"] is True
    assert "allow_post" not in yaml.safe_load(rec.yaml("x"))["x"]  # the person decides
    draft = yaml.safe_load(rec.yaml("x", allow_post=True))["x"]
    assert draft["allow_post"] is True
    Action.model_validate(draft)


def test_real_browser_options_by_text_and_extracts_not(tmp_path, monkeypatch):
    """A search list whose items have positional ids (search-option-1): the click is recorded by
    the item's text; an Alt+click on a list item is not (its text is the value)."""
    pytest.importorskip("playwright")
    import asyncio
    import http.server
    import threading

    page_html = ("<ul role=listbox><li id=search-option-0 role=option>Pages</li>"
                 "<li id=search-option-1 role=option>Users</li></ul>"
                 "<ul><li class=total>42</li></ul>")

    class H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            body = page_html.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass

    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    monkeypatch.setenv("AFS_BROWSER_DIR", str(tmp_path / "browser"))
    c = conn(base_url=f"http://127.0.0.1:{srv.server_port}", allowed_paths=[])
    rec = Recording(c)

    async def person() -> None:
        task = asyncio.create_task(recorder.record("portal", c, rec, "rec", headless=True))
        for _ in range(200):
            if rec.page is not None or rec.done:
                break
            await asyncio.sleep(0.05)
        assert rec.page is not None, rec.error
        await rec.page.wait_for_load_state()
        await rec.page.click("#search-option-1")
        await rec.page.click("li.total", modifiers=["Alt"])
        await asyncio.sleep(0.3)
        rec.stop.set()
        await asyncio.wait_for(task, 30)

    try:
        asyncio.run(person())
    finally:
        srv.shutdown()
    if rec.error and ("Executable doesn't exist" in rec.error or "channel" in rec.error.lower()):
        pytest.skip(f"no browser for Playwright here: {rec.error}")
    assert rec.steps[1:] == [{"click": "li:text-is(\"Users\")"}, {"extract": {"total": "li.total"}}]


def test_the_window_size_is_recorded():
    rec = started()
    rec.add({"kind": "click", "selector": "a:text-is(\"More\")", "vw": 800, "vh": 900})
    rec.add({"kind": "extract", "selector": "#n", "name": "n", "vw": 810, "vh": 900})
    assert rec.warnings == []  # a small change is not a resize
    assert yaml.safe_load(rec.yaml("x"))["x"]["viewport"] == {"width": 800, "height": 900}
    rec.add({"kind": "click", "selector": "#b", "vw": 1400, "vh": 900})
    assert [w.code for w in rec.warnings] == ["web.record_resized"]
    assert rec.viewport == {"width": 800, "height": 900}
    rec.add({"kind": "click", "selector": "#c", "vw": "x", "vh": 10})  # ignored
    Action.model_validate(yaml.safe_load(rec.yaml("x"))["x"])
    with pytest.raises(ValueError, match="only for website"):
        Connection.model_validate({"type": "http_api", "base_url": "https://api.example.com",
                                   "actions": {"a": {"description": "x", "path": "/x",
                                                     "viewport": {"width": 800, "height": 600}}}})


def test_the_language_is_recorded():
    rec = started()
    rec.add({"kind": "click", "selector": "#a", "lang": "pt-PT"})
    rec.add({"kind": "click", "selector": "#b", "lang": "en-US"})  # the first one counts
    rec.add({"kind": "click", "selector": "#c", "lang": "<script>"})  # ignored
    draft = yaml.safe_load(rec.yaml("x"))["x"]
    assert draft["locale"] == "pt-PT"
    Action.model_validate(draft)
    with pytest.raises(ValueError):
        Action.model_validate({**draft, "locale": "not a locale"})


def test_links_become_addresses_and_only_the_last_one_stays():
    rec = started()  # goto /ticket
    rec.add({"kind": "open", "selector": "a:text-is(\"More\")",
             "href": "https://tickets.example.com/app/ticket/more"})
    rec.add({"kind": "open", "selector": "a:text-is(\"Users\")",
             "href": "https://tickets.example.com/app/search/users?page=1"})
    rec.add({"kind": "extract", "selector": "span.total", "name": "total"})
    rec.add({"kind": "open", "selector": "a:text-is(\"Help\")", "href": "https://help.example.org/x"})
    assert rec.steps == [{"goto": "/search/users?page=1"}, {"extract": {"total": "span.total"}},
                         {"click": "a:text-is(\"Help\")"}]  # another site: the click stays
    rec.navigated("https://tickets.example.com/app/search/users?page=1")  # the same page: nothing


def test_real_browser_menu_clicks_become_one_address(tmp_path, monkeypatch):
    """Home → "More" → "Users" (a relative link) → the count: the draft goes straight to the
    users page, so it does not depend on the language of the texts or on the menu layout."""
    pytest.importorskip("playwright")
    import asyncio
    import http.server
    import threading

    pages = {"/": "<aside><a href='/more'>Mais</a></aside>",
             "/more": "<a href='admin/users'>Utilizadores</a>",
             "/admin/users": "<span class=total>42</span>"}

    class Site(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            body = pages.get(self.path, "").encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass

    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Site)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    monkeypatch.setenv("AFS_BROWSER_DIR", str(tmp_path / "browser"))
    c = conn(base_url=f"http://127.0.0.1:{srv.server_port}", allowed_paths=[])
    rec = Recording(c)

    async def person() -> None:
        task = asyncio.create_task(recorder.record("portal", c, rec, "rec", headless=True))
        for _ in range(200):
            if rec.page is not None or rec.done:
                break
            await asyncio.sleep(0.05)
        assert rec.page is not None, rec.error
        p = rec.page
        await p.wait_for_load_state()
        await p.click("text=Mais")
        await p.wait_for_url("**/more")
        await p.click("text=Utilizadores")
        await p.wait_for_url("**/admin/users")
        await p.click("span.total", modifiers=["Alt"])
        await asyncio.sleep(0.3)
        rec.stop.set()
        await asyncio.wait_for(task, 30)

    try:
        asyncio.run(person())
    finally:
        srv.shutdown()
    if rec.error and ("Executable doesn't exist" in rec.error or "channel" in rec.error.lower()):
        pytest.skip(f"no browser for Playwright here: {rec.error}")
    assert rec.steps == [{"goto": "/admin/users"}, {"extract": {"total": "span.total"}}]

"""Named connector actions: model rules, parameter checks, requests, prompt, guard/policy."""
import asyncio
import json
from pathlib import Path

import httpx
import pytest
from pydantic import ValidationError

from agent_flow_studio.builder import FlowError, compile_flow
from agent_flow_studio.builder.spec import FlowSpec
from agent_flow_studio.connections import actions, client, store
from agent_flow_studio.connections.model import Connection
from agent_flow_studio.policy import RUN_ACTION_TOOL, WritePolicy

TICKETS = {
    "type": "http_api", "base_url": "https://tickets.example.com", "auth": {"method": "none"},
    "access": "write", "actions_only": True, "allowed_paths": ["/api/tickets"],
    "actions": {
        "get_ticket": {"description": "Reads one ticket.", "path": "/api/tickets/{number}",
                       "query": {"fields": "summary,notes", "lang": "{lang}"},
                       "params": {"number": {"pattern": "INC[0-9]{4}"},
                                  "lang": {"type": "choice", "options": ["en", "pt"],
                                           "required": False}}},
        "add_note": {"description": "Adds a work note.", "access": "write", "method": "POST",
                     "path": "/api/tickets/{number}/notes",
                     "body": {"text": "{text}", "internal": "{internal}"},
                     "params": {"number": {"pattern": "INC[0-9]{4}"}, "text": {"max_length": 500},
                                "internal": {"type": "boolean"}}},
    },
}


@pytest.fixture
def cfile(tmp_path: Path, monkeypatch) -> Path:
    f = tmp_path / "connections.yaml"
    monkeypatch.setenv("CONNECTIONS_FILE", str(f))
    return f


@pytest.fixture
def seen(monkeypatch):
    reqs: list[httpx.Request] = []

    def handler(req: httpx.Request) -> httpx.Response:
        reqs.append(req)
        return httpx.Response(200, json={"ok": True})

    monkeypatch.setattr(client, "_transport", httpx.MockTransport(handler))
    return reqs


# --- model ------------------------------------------------------------------------------------
@pytest.mark.parametrize("patch,needle", [
    ({"actions": {"x": {"description": "d", "path": "/a/{nope}"}}}, "without a parameter"),
    ({"access": "read"}, "write actions need a connector with access: write"),
    ({"actions": {"x": {"description": "d", "path": "/a", "method": "POST"}}}, "read actions use GET"),
    ({"actions": {"x": {"description": "d", "path": "a"}}}, "must start with /"),
    ({"actions": {"Bad": {"description": "d", "path": "/a"}}}, "invalid action name"),
    ({"actions": {}, "actions_only": True}, "at least one action"),
    ({"actions": {"x": {"description": "token: ghp_abcdefghijklmnopqrstu12345", "path": "/a"}}},
     "looks like a secret"),
])
def test_action_rules(patch, needle):
    with pytest.raises(ValidationError, match=needle):
        Connection.model_validate(TICKETS | patch)


def test_check_params():
    a = Connection.model_validate(TICKETS).actions["add_note"]
    assert actions.check_params(a, {"number": "INC0001", "text": "hi", "internal": "true"}) == \
        {"number": "INC0001", "text": "hi", "internal": True}
    for bad, needle in [({"number": "X1", "text": "t", "internal": True}, "format"),
                        ({"number": "INC0001", "internal": True}, "text: required"),
                        ({"number": "INC0001", "text": "t", "internal": "maybe"}, "true or false"),
                        ({"number": "INC0001", "text": "a\nb", "internal": True}, "control"),
                        ({"number": "INC0001", "text": "t", "internal": True, "x": 1}, "unknown")]:
        with pytest.raises(actions.ActionError, match=needle):
            actions.check_params(a, bad)


# --- requests ---------------------------------------------------------------------------------
def test_read_action_builds_the_request(cfile, seen):
    store.save({"tickets": Connection.model_validate(TICKETS)})
    r = asyncio.run(actions.run("tickets", "get_ticket", {"number": "INC0042", "lang": "pt"}))
    assert r["status"] == 200 and r["request"] == "GET /api/tickets/INC0042"
    assert seen[0].method == "GET"
    assert str(seen[0].url) == "https://tickets.example.com/api/tickets/INC0042?fields=summary%2Cnotes&lang=pt"
    asyncio.run(actions.run("tickets", "get_ticket", {"number": "INC0042"}))
    assert "lang" not in str(seen[1].url)  # optional and empty: left out


def test_write_action_typed_body(cfile, seen):
    store.save({"tickets": Connection.model_validate(TICKETS)})
    asyncio.run(actions.run("tickets", "add_note",
                            {"number": "INC0042", "text": "Checked", "internal": False}))
    assert seen[0].method == "POST" and seen[0].url.path == "/api/tickets/INC0042/notes"
    assert json.loads(seen[0].content) == {"text": "Checked", "internal": False}


def test_path_params_are_encoded_and_allowed_paths_apply(cfile, seen):
    conn = json.loads(json.dumps(TICKETS))
    conn["actions"]["get_ticket"]["params"]["number"] = {}  # no pattern
    store.save({"tickets": Connection.model_validate(conn)})
    asyncio.run(actions.run("tickets", "get_ticket", {"number": "a/b?c=1"}))
    assert str(seen[0].url).startswith("https://tickets.example.com/api/tickets/a%2Fb%3Fc%3D1?")
    with pytest.raises(client.ConnectorError, match="invalid path"):  # '..' is refused, encoded too
        asyncio.run(actions.run("tickets", "get_ticket", {"number": "../admin"}))
    with pytest.raises(actions.ActionError, match="no action"):
        asyncio.run(actions.run("tickets", "delete_all", {}))


# --- compile: implicit tool, prompt, rules ----------------------------------------------------
FLOW = {"name": "mini", "title": "Mini", "task": "Do {input}.", "start": "reader",
        "agents": {"reader": {"description": "reads", "prompt": "Read.", "connections": ["tickets"]},
                   "writer": {"description": "writes", "prompt": "Write.",
                              "write_connections": ["tickets"]}},
        "edges": [{"from": "reader", "to": "writer"}]}


def test_compile_gives_run_action_and_lists_the_actions(tmp_path, cfile):
    store.save({"tickets": Connection.model_validate(TICKETS)})
    cf = compile_flow(FlowSpec.model_validate(FLOW), tmp_path)
    assert RUN_ACTION_TOOL in cf.agent_tools["reader"] and RUN_ACTION_TOOL in cf.agent_tools["writer"]
    assert "connections" in cf.options.mcp_servers
    reader, writer = cf.options.agents["reader"].prompt, cf.options.agents["writer"].prompt
    assert "`get_ticket`(number: text; lang: one of en/pt, optional)" in reader
    assert "add_note" not in reader  # write actions only for its write connectors
    assert "`add_note`" in writer and "[write: follows the autonomy/approval]" in writer
    assert "These are the only operations allowed on it." in reader
    assert cf.policy.connection_actions["tickets"] == {"get_ticket": "read", "add_note": "write"}


def test_connector_without_actions_still_needs_http_get(tmp_path, cfile):
    plain = {k: v for k, v in TICKETS.items() if k not in ("actions", "actions_only")}
    store.save({"tickets": Connection.model_validate(plain)})
    with pytest.raises(FlowError, match="needs connections.http_get"):
        compile_flow(FlowSpec.model_validate(FLOW), tmp_path)


# --- policy -----------------------------------------------------------------------------------
def _policy(approver=None) -> WritePolicy:
    return WritePolicy(read_connections={"reader": {"tickets"}},
                       write_connections={"writer": {"tickets"}},
                       connection_env={"tickets": "prod"},
                       connection_actions={"tickets": {"get_ticket": "read", "add_note": "write"}},
                       actions_only={"tickets"}, approver=approver)


def check(p, tool, agent, args):
    return asyncio.run(p.check(tool, agent, args))


def test_policy_for_actions():
    p = _policy()
    get = {"connection": "tickets", "action": "get_ticket", "params": {"number": "INC0001"}}
    note = {"connection": "tickets", "action": "add_note", "params": {}}
    assert check(p, RUN_ACTION_TOOL, "reader", get) is None
    assert check(p, RUN_ACTION_TOOL, "writer", get) is None  # write access includes reading
    assert "not assigned" in check(p, RUN_ACTION_TOOL, "other", get)
    assert "no action" in check(p, RUN_ACTION_TOOL, "reader", get | {"action": "drop"})
    assert "no write access" in check(p, RUN_ACTION_TOOL, "reader", note)
    assert "requires human approval" in check(p, RUN_ACTION_TOOL, "writer", note)
    assert "orchestrator" in check(p, RUN_ACTION_TOOL, None, get)
    assert "only be used through its named actions" in check(
        p, "mcp__connections__http_get", "reader", {"connection": "tickets", "path": "/api/tickets"})

    asked = []

    async def approver(req):
        asked.append(req)
        return True, ""

    assert check(_policy(approver), RUN_ACTION_TOOL, "writer", note) is None
    assert asked[0].summary == "Action tickets.add_note"


@pytest.mark.parametrize("text,kinds,expected", [
    ("Call me at 912 345 678 or +351 912345678.", ["phone"], "Call me at [phone] or [phone]."),
    ("Ticket 123456 on 2026-11-20 10:21:33.000, ref 12345678", ["phone"],
     "Ticket 123456 on 2026-11-20 10:21:33.000, ref 12345678"),  # tickets, dates: kept
    ("Write to ana.silva@example.com, tel 21 123 45 67", ["phone"],
     "Write to ana.silva@example.com, tel [phone]"),
    ("Write to ana.silva@example.com", ["email"], "Write to [email]"),
])
def test_redact(text, kinds, expected):
    assert actions.redact(text, kinds) == expected
    assert actions.redact({"notes": [{"value": text}], "n": 5}, kinds) == \
        {"notes": [{"value": expected}], "n": 5}

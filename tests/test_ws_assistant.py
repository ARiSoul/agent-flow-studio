"""Workspace assistant (Board chat): read tools, checked proposals that never write, locked-down
options, and the endpoint (the workspace comes in the cookie). No real model is called."""
import asyncio
import json
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient

from agent_flow_studio import context, workspaces
from agent_flow_studio.editor import chats, ws_assistant
from agent_flow_studio.editor.server import create_app
from agent_flow_studio.editor.ws_assistant import Proposals, build_options

H = {"X-Flow-Editor": "1"}


def call(p: Proposals, tool_name: str, /, **args) -> tuple[bool, str]:
    t = {t.name: t for t in p.tools()}[tool_name]
    r = asyncio.run(t.handler(args))
    return not r.get("is_error"), r["content"][0]["text"]


@pytest.fixture
def ws(tmp_path: Path) -> workspaces.Workspace:
    w = workspaces.create("shop", "local", title="Shop")
    assert w.spec is not None
    (tmp_path / "app").mkdir()
    w.spec = workspaces.WorkspaceSpec.model_validate({
        **w.spec.model_dump(mode="json"), "vars": {"team": "core"},
        "roots": {"app": {"path": str(tmp_path / "app")}},
        "environments": {"dev": {"kind": "dev"}, "prod": {"kind": "prod"}}})
    workspaces.save(w)
    with workspaces.using(w):
        yield w


def _flow(name: str = "hello") -> dict:
    return {"version": 1, "name": name, "title": "Hello", "execution": "graph",
            "task": "Help team {vars.team} with {input}.", "start": "writer",
            "agents": {"writer": {"description": "Writes.", "prompt_file": "prompts/writer.md",
                                  "outputs": {"text": {"type": "text"}}}}}


def test_view_tools(ws, tmp_path):
    p = Proposals()
    context.set_section(ws, "app", "A shop in Python.")
    good, text = call(p, "view_workspace")
    data = json.loads(text)
    assert good and data["roots"]["app"]["exists"] and data["roots"]["app"]["explored"]
    assert data["vars"] == {"team": "core"} and set(data["environments"]) == {"dev", "prod"}
    assert "app" in call(p, "view_context")[1]
    assert "A shop in Python." in call(p, "view_context", section="app")[1]
    assert not call(p, "view_context", section="nope")[0]
    assert "no flows" in call(p, "view_flows")[1]
    (ws.flows_dir / "hello" / "prompts").mkdir(parents=True)
    (ws.flows_dir / "hello" / "flow.yaml").write_text(yaml.safe_dump(_flow()), encoding="utf-8")
    (ws.flows_dir / "hello" / "prompts" / "writer.md").write_text("Write.", encoding="utf-8")
    assert "hello" in json.loads(call(p, "view_flows")[1])
    assert "Write." in call(p, "view_flow", name="hello")[1]
    assert not call(p, "view_flow", name="other")[0]
    assert "code-review" in call(p, "view_catalog")[1]


def test_workspace_proposal_is_checked_and_never_saved(ws, tmp_path):
    p = Proposals()
    before = (ws.folder / "workspace.yaml").read_text(encoding="utf-8")
    good, _ = call(p, "propose_workspace_change", set_vars={"region": "eu"},
                   set_roots={"docs": {"path": "docs"}})
    assert good
    good, _ = call(p, "propose_workspace_change", remove_environments=["prod"],
                   description="The shop.")
    assert good  # the second call adds up to the same card
    (card,) = p.items.values()
    assert card["kind"] == "workspace" and card["name"] == "shop"
    assert set(card["changes"]) == {"vars", "roots", "environments", "description"}
    assert card["changes"]["vars"] == {"team": "core", "region": "eu"}
    assert set(card["changes"]["environments"]) == {"dev"}
    assert (ws.folder / "workspace.yaml").read_text(encoding="utf-8") == before
    assert not call(p, "propose_workspace_change", set_roots={"Bad-Name": {"path": "x"}})[0]
    assert not call(p, "propose_workspace_change", remove_vars=["missing"])[0]
    assert not call(Proposals(), "propose_workspace_change", default_environment="qa")[0]
    with workspaces.using(workspaces.STUDIO_WS):
        assert not call(Proposals(), "propose_workspace_change", title="x")[0]


def test_flow_proposal(ws, tmp_path):
    p = Proposals()
    good, text = call(p, "propose_flow", name="hello", title="Hello", spec=_flow(),
                      prompts={"prompts/writer.md": "Write for {vars.team}."})
    assert good, text
    card = p.items["flow:hello"]
    assert card["agents"] == ["writer"] and card["prompts"]["prompts/writer.md"]
    assert not (ws.flows_dir / "hello").exists()  # nothing written
    # from a template
    good, text = call(p, "propose_flow", name="review", title="Review", template="code-review",
                      params={"workspace": str(tmp_path / "app")})
    assert good, text
    assert p.items["flow:review"]["template"] == "code-review"
    # refused: problems come back, auto autonomy, bad names, both or neither source
    bad = _flow() | {"start": "nobody"}
    good, text = call(p, "propose_flow", name="x1", title="X", spec=bad)
    assert not good and "problems" in text
    good, text = call(p, "propose_flow", name="x2", title="X", spec=_flow() | {"autonomy": "auto"},
                      prompts={"prompts/writer.md": "w"})
    assert not good and "auto" in text
    assert not call(p, "propose_flow", name="Bad Name", title="X", spec=_flow())[0]
    assert not call(p, "propose_flow", name="x3", title="X")[0]
    assert not call(p, "propose_flow", name="x4", title="X", spec=_flow(),
                    prompts={"../evil.md": "x"})[0]
    (ws.flows_dir / "hello").mkdir(parents=True)
    (ws.flows_dir / "hello" / "flow.yaml").write_text(yaml.safe_dump(_flow()), encoding="utf-8")
    assert not call(Proposals(), "propose_flow", name="hello", title="H", spec=_flow())[0]


def test_flow_proposal_leaves_external_mcps_unapproved(ws):
    p = Proposals()
    spec = _flow() | {"mcp_servers": {"docs": {"type": "http", "url": "https://mcp.example.com",
                                               "tools": ["search"], "approved": True,
                                               "read_only": True}}}
    call(p, "propose_flow", name="hello", title="Hello", spec=spec,
         prompts={"prompts/writer.md": "w"})
    mcp = p.items["flow:hello"]["spec"]["mcp_servers"]["docs"]
    assert not mcp.get("approved") and not mcp.get("read_only")  # a person approves it


def test_connector_proposal_has_no_secrets_and_reads_only(ws, tmp_path, monkeypatch):
    monkeypatch.setenv("CONNECTIONS_FILE", str(tmp_path / "connections.yaml"))
    (tmp_path / "connections.yaml").write_text(yaml.safe_dump({"version": 1, "connections": {
        "studio_api": {"type": "http_api", "base_url": "https://api.example.com"}}}),
        encoding="utf-8")
    p = Proposals()
    d = {"type": "http_api", "base_url": "https://shop.example.com", "auth": {"method": "bearer"},
         "description": "Shop API",
         "actions": {"get_order": {"description": "One order", "path": "/orders/{id}",
                                   "params": {"id": {"type": "integer"}}}},
         "actions_only": True}
    good, text = call(p, "propose_connector", name="shop_api", definition=d)
    assert good, text
    card = p.items["connector:shop_api"]
    assert card["secret_field"] == "token" and not card["exists"] and not card["sign_in"]
    assert "secrets" not in card["connector"] and card["connector"]["type"] == "http_api"
    assert not ws.connections_file.exists()  # nothing written
    assert not call(p, "propose_connector", name="x", definition=d | {
        "secrets": {"token": "keyring"}})[0]
    assert not call(p, "propose_connector", name="x", definition=d | {"secret": "s3cr3t"})[0]
    assert not call(p, "propose_connector", name="x", definition=d | {"access": "write"})[0]
    w = {**d, "actions": {"del": {"description": "x", "access": "write", "method": "DELETE",
                                  "path": "/x"}}}
    assert not call(p, "propose_connector", name="x", definition=w)[0]
    assert not call(p, "propose_connector", name="x", definition={"type": "http_api"})[0]
    good, text = call(p, "propose_connector", name="studio_api", definition=d)
    assert not good and "studio connector" in text  # the workspace gets its own name


def test_options_are_locked_down():
    o = build_options(Proposals(), None, "pt")
    assert o.tools == [] and o.permission_mode == "dontAsk" and o.setting_sources == []
    assert set(o.mcp_servers) == {"workspace_builder"}
    assert all(t.startswith("mcp__workspace_builder__") for t in o.allowed_tools)
    assert {"Agent", "Task", "Bash", "Write", "Read"} <= set(o.disallowed_tools)
    assert o.hooks["PreToolUse"][0].on_failure == "block"
    assert "European Portuguese" in o.system_prompt


def test_endpoint_streams_in_the_workspace_of_the_cookie(tmp_path, monkeypatch):
    seen: dict = {}

    async def fake_chat(message, session_id=None, lang=None, command=None):
        seen.update(message=message, ws=workspaces.current().name, lang=lang)
        yield {"type": "text", "text": "Hello"}
        yield {"type": "done", "proposals": []}

    monkeypatch.setattr(ws_assistant, "chat", fake_chat)
    (tmp_path / "flows").mkdir()
    c = TestClient(create_app(tmp_path / "flows", allowed_hosts=["testserver"]))
    assert c.post("/api/workspaces", json={"name": "shop"}, headers=H).status_code == 201
    c.cookies.set("afs_ws", "shop")
    body = {"message": "hi", "lang": "es"}
    assert c.post("/api/assist/workspace", json=body).status_code == 403  # no header
    r = c.post("/api/assist/workspace", json=body, headers=H)
    assert [json.loads(x)["type"] for x in r.text.splitlines()] == ["text", "done"]
    assert seen == {"message": "hi", "ws": "shop", "lang": "es"}
    assert c.post("/api/assist/workspace", json={**body, "session_id": "../x"},
                  headers=H).status_code == 422
    # the conversation history of the workspace assistant
    item = {"kind": "proposal", "p": {"id": "flow:x"}}
    r = c.put("/api/chats/@workspace/abcdefgh12", headers=H,
              json={"title": "t", "session_id": None, "cost": 0, "items": [item]})
    assert r.status_code == 200, r.text
    assert [x["id"] for x in c.get("/api/chats/@workspace").json()] == ["abcdefgh12"]
    assert chats.FLOW.fullmatch("@workspace") and not chats.FLOW.fullmatch("@other")


def test_a_short_flow_is_filled_in(ws):
    p = Proposals()
    short = {"agents": {"analyst": {"description": "Reads the item.", "tools": []},
                        "reviewer": {"description": "Reviews the analysis."}},
             "edges": [{"from": "analyst", "to": "reviewer"}]}
    good, text = call(p, "propose_flow", name="short", title="Short", spec=short)
    assert good, text
    assert "Filled in" in text
    card = p.items["flow:short"]
    assert card["spec"]["start"] == "analyst" and card["spec"].get("execution", "graph") == "graph"
    assert "{input}" in card["spec"]["task"]
    assert set(card["prompts"]) == {"prompts/analyst.md", "prompts/reviewer.md"}
    assert "Reads the item." in card["prompts"]["prompts/analyst.md"]


def test_all_the_problems_come_back_at_once_with_where(ws):
    bad = {"agents": {"a": {"description": "A.", "tools": ["nope.tool"]},
                      "b": {"description": "B.", "tools": ["other.tool"]}},
           "edges": [{"from": "a", "to": "b"}, {"from": "a", "to": "ghost"}]}
    good, text = call(Proposals(), "propose_flow", name="bad", title="Bad", spec=bad)
    assert not good
    body = json.loads(text.removeprefix("ERROR: "))
    assert len(body["problems"]) >= 2 and body["hint"]  # all of them, not the first one
    assert all({"where", "nodes", "problem"} <= set(x) for x in body["problems"])


def _fake_query(*messages, error=None):
    async def q(prompt, options):
        for m in messages:
            yield m
        if error:
            raise error
    return q


def _assistant_msg(mid, *blocks):
    from claude_agent_sdk import AssistantMessage
    return AssistantMessage(content=list(blocks), model="m", message_id=mid)


def _events(monkeypatch, q):
    from agent_flow_studio.editor import assistant
    monkeypatch.setattr(assistant, "query", q)

    async def go():
        return [e async for e in ws_assistant.chat("hi")]
    return asyncio.run(go())


def test_turns_are_counted_and_the_limit_is_not_an_error(ws, monkeypatch):
    from claude_agent_sdk import ResultError, ResultMessage, TextBlock, ToolUseBlock
    tool = ToolUseBlock(id="t1", name="mcp__workspace_builder__view_flows", input={})
    result = ResultMessage(subtype="error_max_turns", duration_ms=1, duration_api_ms=1,
                           is_error=True, num_turns=60, session_id="s-1", total_cost_usd=0.5)
    err = ResultError("Reached maximum number of turns (60)",
                      {"subtype": "error_max_turns", "session_id": "s-1"}, exit_code=1)
    evs = _events(monkeypatch, _fake_query(
        _assistant_msg("m1", TextBlock(text="Looking.")), _assistant_msg("m1", tool),
        _assistant_msg("m2", TextBlock(text="More.")), result, error=err))
    kinds = [e["type"] for e in evs]
    assert kinds == ["turn", "text", "tool", "turn", "text", "done"]  # no error event
    assert [e["n"] for e in evs if e["type"] == "turn"] == [1, 2]
    assert evs[0]["max"] == ws_assistant.MAX_TURNS == 60
    done = evs[-1]
    assert done["limit"] and not done["is_error"] and done["session_id"] == "s-1"
    assert done["proposals"] == []


def test_the_limit_reported_only_in_the_error_still_gives_a_session(ws, monkeypatch):
    from claude_agent_sdk import ResultError
    err = ResultError("Reached maximum number of turns",
                      {"subtype": "error_max_turns", "session_id": "s-2"}, exit_code=1)
    evs = _events(monkeypatch, _fake_query(error=err))
    assert [e["type"] for e in evs] == ["done"] and evs[0]["limit"]
    assert evs[0]["session_id"] == "s-2"


def test_other_errors_are_still_errors(ws, monkeypatch):
    from claude_agent_sdk import ResultError
    err = ResultError("API Error: overloaded", {"subtype": "success",
                                                "terminal_reason": "api_error"}, exit_code=1)
    evs = _events(monkeypatch, _fake_query(error=err))
    assert [e["type"] for e in evs] == ["error"] and "overloaded" in evs[0]["message"]


def test_a_flow_input_no_agent_declares_is_refused(ws):
    spec = {"inputs": {"work_item": {"type": "text"}},
            "agents": {"reader": {"description": "Reads the item.",
                                  "outputs": {"item": {"type": "text"}}}}}
    good, text = call(Proposals(), "propose_flow", name="reads", title="Reads", spec=spec)
    assert not good and "work_item" in text and "warnings" in text
    fixed = {**spec, "agents": {"reader": {**spec["agents"]["reader"],
                                           "inputs": {"work_item": {"from": "input.work_item"}}}}}
    p = Proposals()
    assert call(p, "propose_flow", name="reads", title="Reads", spec=fixed)[0]

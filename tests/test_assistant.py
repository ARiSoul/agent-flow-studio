"""Editor assistant: tools over the draft, safety rules and chat endpoint."""
import asyncio
import json
import shutil
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient

from agent_flow_studio.editor import assistant
from agent_flow_studio.editor.assistant import Draft, build_options
from agent_flow_studio.editor.server import create_app

PIPELINE = Path(__file__).parent / "fixtures" / "flows" / "pipeline"

H = {"X-Flow-Editor": "1"}


def _load() -> tuple[dict, dict]:
    spec = yaml.safe_load((PIPELINE / "flow.yaml").read_text(encoding="utf-8"))
    prompts = {f"prompts/{p.name}": p.read_text(encoding="utf-8")
               for p in (PIPELINE / "prompts").glob("*.md")}
    return spec, prompts


@pytest.fixture
def draft() -> Draft:
    return Draft(*_load())


def call(d: Draft, tool_name: str, /, **args) -> tuple[bool, str]:
    t = {t.name: t for t in d.tools()}[tool_name]
    r = asyncio.run(t.handler(args))
    return not r.get("is_error"), r["content"][0]["text"]


def test_add_agent_and_connect(draft: Draft):
    ok, text = call(draft, "set_agent", name="summary", description="Sums up the ticket.",
                    prompt="Sum up.", tools=["artifacts.read_artifact"], reads=["01_triage.json"])
    assert ok and "not reachable" in text  # not connected yet
    assert draft.spec["agents"]["summary"]["prompt_file"] == "prompts/summary.md"
    assert draft.prompts["prompts/summary.md"] == "Sum up."
    ok, text = call(draft, "add_edge", **{"from": "diagnostician", "to": "summary", "when": "always"})
    assert ok and "flow valid" in text and draft.changed


def test_update_existing_prompt_goes_to_file(draft: Draft):
    call(draft, "set_agent", name="triage", prompt="New prompt.")
    assert draft.prompts["prompts/triage.md"] == "New prompt."
    assert call(draft, "view_prompt", node="triage")[1] == "New prompt."


def test_invalid_tool_is_reported(draft: Draft):
    _, text = call(draft, "set_agent", name="triage", tools=["fake.drop_table"])
    assert "does not exist in the catalog" in text and not draft.validation()["ok"]


def test_rename_and_remove(draft: Draft):
    assert call(draft, "rename_node", old="triage", new="triage_step")[0]
    assert draft.spec["start"] == "triage_step"
    assert all("triage" not in (e["from"], e["to"]) for e in draft.spec["edges"])
    assert not call(draft, "remove_node", name="triage_step")[0]  # the start node cannot be removed
    assert call(draft, "remove_node", name="approve_fix")[0]
    assert "approve_fix" not in draft.spec["gates"]


def test_mcp_never_approved_by_assistant(draft: Draft):
    call(draft, "set_mcp", name="crm", type="stdio", command="npx", tools=["list_items"],
         env={"TOKEN": "${CRM_TOKEN}"})
    m = draft.spec["mcp_servers"]["crm"]
    assert m["approved"] is False and m["read_only"] is False
    _, text = call(draft, "set_agent", name="diagnostician",
                   tools=["artifacts.read_artifact", "artifacts.save_artifact", "crm.list_items"])
    assert "not approved" in text


def test_mcp_literal_secret_rejected(draft: Draft):
    _, text = call(draft, "set_mcp", name="crm", type="stdio", command="npx", tools=["x"],
                   env={"TOKEN": "abc123"})
    assert "secrets stay out of the flow" in text


def test_read_tools_do_not_mark_changed(draft: Draft):
    for name in ("view_flow", "view_catalog", "validate"):
        assert call(draft, name)[0]
    assert not draft.changed
    assert "artifacts.save_artifact" in call(draft, "view_catalog")[1]


def test_options_are_locked_down(draft: Draft):
    o = build_options(draft, None)
    assert o.tools == [] and o.permission_mode == "dontAsk" and o.setting_sources == []
    assert set(o.mcp_servers) == {"flow_builder"}
    assert all(t.startswith("mcp__flow_builder__") for t in o.allowed_tools)
    assert {"Agent", "Task", "Bash", "Write", "Read"} <= set(o.disallowed_tools)
    assert o.hooks["PreToolUse"][0].on_failure == "block"


def test_assist_endpoint_streams_ndjson(tmp_path: Path, monkeypatch):
    shutil.copytree(PIPELINE, tmp_path / "pipeline")
    before = (tmp_path / "pipeline/flow.yaml").read_text(encoding="utf-8")
    seen = {}

    async def fake_chat(message, flow, spec, prompts, session_id=None, lang=None, command=None):
        seen.update(message=message, flow=flow, n_prompts=len(prompts), session=session_id,
                    lang=lang)
        yield {"type": "text", "text": "Hello"}
        yield {"type": "done", "changed": False, "doc": None}

    monkeypatch.setattr(assistant, "chat", fake_chat)
    c = TestClient(create_app(tmp_path, allowed_hosts=["testserver"]))
    spec, _ = _load()
    body = {"flow": "pipeline", "message": "hi", "spec": spec, "session_id": "abc-1"}
    assert c.post("/api/assist", json=body).status_code == 403  # no header
    r = c.post("/api/assist", json=body, headers=H)
    events = [json.loads(line) for line in r.text.splitlines()]
    assert [e["type"] for e in events] == ["text", "done"]
    assert seen == {"message": "hi", "flow": "pipeline", "n_prompts": 8, "session": "abc-1",
                    "lang": None}
    assert (tmp_path / "pipeline/flow.yaml").read_text(encoding="utf-8") == before
    bad = {**body, "session_id": "../x"}
    assert c.post("/api/assist", json=bad, headers=H).status_code == 422
    c.post("/api/assist", json={**body, "lang": "es"}, headers=H)
    assert seen["lang"] == "es"  # the endpoint passes the UI language to the assistant


@pytest.mark.parametrize("lang,expected", [
    ("pt", "European Portuguese"),
    ("en", "English"),
    ("es", "Spanish"),
    (None, "English"),  # missing → English
    ("fr", "English"),  # unknown → English
])
def test_system_prompt_ends_with_reply_language(draft: Draft, lang, expected):
    o = build_options(draft, None, lang)
    tail = o.system_prompt.rstrip().rsplit("## Language", 1)[1]
    assert f"Reply to the user in {expected}" in tail
    assert o.system_prompt.startswith(assistant.PROMPT.split("\n", 1)[0])


def test_typed_io_graph_execution_and_if_conditions(draft: Draft):
    ok, text = call(draft, "update_flow", execution="graph",
                    inputs={"ticket": {"type": "text", "label": "Ticket"}}, task="Handle {ticket}.")
    assert ok, text
    ok, text = call(draft, "set_agent", name="triage", inputs={"ticket": {"from": "input.ticket"}},
                    outputs={"severity": {"type": "choice", "options": ["low", "high"]}})
    assert ok, text
    ok, text = call(draft, "add_edge", **{"from": "triage", "to": "diagnostician",
                                          "if": 'triage.severity == "high"'})
    assert ok and draft.spec["agents"]["triage"]["outputs"]["severity"]["type"] == "choice"
    edge = next(e for e in draft.spec["edges"] if e["from"] == "triage"
                and e["to"] == "diagnostician")
    assert edge["if"] == 'triage.severity == "high"'
    ok, text = call(draft, "add_edge", **{"from": "triage", "to": "diagnostician",
                                          "if": "triage.nope > 1"})
    assert ok and "has no output 'nope'" in text  # the summary reports the problem


def test_rename_updates_from_refs_and_expressions(draft: Draft):
    call(draft, "set_agent", name="triage",
         outputs={"severity": {"type": "choice", "options": ["low", "high"]}})
    call(draft, "set_agent", name="diagnostician", inputs={"sev": {"from": "triage.severity"}})
    call(draft, "add_edge", **{"from": "triage", "to": "diagnostician",
                               "if": 'triage.severity == "high" and not xtriage.a'})
    ok, _ = call(draft, "rename_node", old="triage", new="sorting")
    assert ok
    assert draft.spec["agents"]["diagnostician"]["inputs"]["sev"]["from"] == "sorting.severity"
    edge = next(e for e in draft.spec["edges"] if e["from"] == "sorting"
                and e["to"] == "diagnostician")
    assert edge["if"] == 'sorting.severity == "high" and not xtriage.a'


def test_library_tools(draft: Draft):
    ok, text = call(draft, "view_library")
    assert ok and "summarizer" in text and "Summarizer" in text and not draft.changed
    ok, text = call(draft, "view_library", id="summarizer")
    assert ok and "Summarize" in text  # the prompt
    assert not call(draft, "view_library", id="nope")[0]

    ok, text = call(draft, "add_library_agent", id="summarizer")
    assert ok and "copied from the library agent summarizer" in text and draft.changed
    a = draft.spec["agents"]["summarizer"]
    assert a["origin"]["agent"] == "summarizer" and a["prompt_file"] == "prompts/summarizer.md"
    assert draft.prompts["prompts/summarizer.md"].startswith("Summarize")
    assert call(draft, "add_library_agent", id="summarizer")[0]  # a second copy: summarizer_2
    assert "summarizer_2" in draft.spec["agents"]
    assert not call(draft, "add_library_agent", id="summarizer", name="triage")[0]  # name taken

    # tuned here, then updated from the library: the input link and icon stay, the rest returns
    call(draft, "set_agent", name="summarizer", model="haiku", icon="chat", prompt="Mine.",
         inputs={"text": {"from": "triage.summary"}})
    assert _origins(draft)["summarizer"]["changed"] is True
    ok, _ = call(draft, "update_from_library", name="summarizer")
    a = draft.spec["agents"]["summarizer"]
    assert ok and "model" not in a and a["icon"] == "chat"
    assert a["inputs"]["text"]["from"] == "triage.summary"
    assert draft.prompts["prompts/summarizer.md"].startswith("Summarize")
    assert _origins(draft)["summarizer"]["changed"] is False
    assert not call(draft, "update_from_library", name="triage")[0]  # not from the library


def _origins(d: Draft) -> dict:
    from agent_flow_studio.editor.validation import _validate
    return _validate(d.spec, d.prompts)["origins"]


def test_propose_library_agent_creates_and_edits_without_saving(draft: Draft, tmp_path):
    from agent_flow_studio.builder import library

    # new, from scratch: checked, not saved, not a flow change
    ok, text = call(draft, "propose_library_agent", id="ticket_triage", title="Ticket triage",
                    description="Classifies a ticket.", prompt="Classify the ticket.",
                    inputs={"ticket": {"type": "markdown"}},
                    outputs={"severity": {"type": "choice", "options": ["low", "high"]}})
    assert ok and "not saved yet" in text and not draft.changed
    p = draft.library_proposals["ticket_triage"]
    assert p["exists"] is False and p["prompt"] == "Classify the ticket."
    assert not (tmp_path / "agents" / "ticket_triage").exists()

    # from an agent of the flow: flow-only fields are dropped
    ok, _ = call(draft, "propose_library_agent", id="my_triage", from_agent="triage")
    assert ok and "prompt_file" not in draft.library_proposals["my_triage"]["agent"]
    assert draft.library_proposals["my_triage"]["prompt"] == draft.prompts["prompts/triage.md"]

    # edit a local library agent: same id, marked as replacing it
    library.save_local("notes", "Notes", {"description": "Takes notes."}, "Write notes.")
    ok, _ = call(draft, "propose_library_agent", id="notes", from_library="notes",
                 description="Takes short notes.")
    p = draft.library_proposals["notes"]
    assert ok and p["exists"] is True and p["title"] == "Notes"
    assert p["agent"]["description"] == "Takes short notes." and p["prompt"].strip() == "Write notes."

    # built-in agents cannot be changed; invalid agents are reported
    assert "(built-in) library agent" in call(draft, "propose_library_agent", id="summarizer",
                                        from_library="summarizer", model="haiku")[1]
    assert "not in the catalog" in call(draft, "propose_library_agent", id="bad", description="x",
                                        prompt="p", tools=["crm.list_items"])[1]
    assert not call(draft, "propose_library_agent", id="empty", description="x")[0]  # no prompt

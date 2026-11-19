"""Assistente do editor: ferramentas sobre o rascunho, regras de segurança e endpoint de chat."""
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
    ok, text = call(draft, "definir_agente", name="resumo", description="Resume o ticket.",
                    prompt="Resume.", tools=["artifacts.read_artifact"], reads=["01_triage.json"])
    assert ok and "sem ligação" in text  # ainda solto
    assert draft.spec["agents"]["resumo"]["prompt_file"] == "prompts/resumo.md"
    assert draft.prompts["prompts/resumo.md"] == "Resume."
    ok, text = call(draft, "ligar", **{"from": "diagnostician", "to": "resumo", "when": "sempre"})
    assert ok and "flow válido" in text and draft.changed


def test_update_existing_prompt_goes_to_file(draft: Draft):
    call(draft, "definir_agente", name="triage", prompt="Novo prompt.")
    assert draft.prompts["prompts/triage.md"] == "Novo prompt."
    assert call(draft, "ver_prompt", node="triage")[1] == "Novo prompt."


def test_invalid_tool_is_reported(draft: Draft):
    _, text = call(draft, "definir_agente", name="triage", tools=["fake.drop_table"])
    assert "não existe no catálogo" in text and not draft.validation()["ok"]


def test_rename_and_remove(draft: Draft):
    assert call(draft, "renomear_no", old="triage", new="triagem")[0]
    assert draft.spec["start"] == "triagem"
    assert all("triage" not in (e["from"], e["to"]) for e in draft.spec["edges"])
    assert not call(draft, "remover_no", name="triagem")[0]  # início não se remove
    assert call(draft, "remover_no", name="approve_fix")[0]
    assert "approve_fix" not in draft.spec["gates"]


def test_mcp_never_approved_by_assistant(draft: Draft):
    call(draft, "definir_mcp", name="crm", type="stdio", command="npx", tools=["list_items"],
         env={"TOKEN": "${CRM_TOKEN}"})
    m = draft.spec["mcp_servers"]["crm"]
    assert m["approved"] is False and m["read_only"] is False
    _, text = call(draft, "definir_agente", name="diagnostician",
                   tools=["artifacts.read_artifact", "artifacts.save_artifact", "crm.list_items"])
    assert "não aprovado" in text


def test_mcp_literal_secret_rejected(draft: Draft):
    _, text = call(draft, "definir_mcp", name="crm", type="stdio", command="npx", tools=["x"],
                   env={"TOKEN": "abc123"})
    assert "segredos não ficam no flow" in text


def test_read_tools_do_not_mark_changed(draft: Draft):
    for name in ("ver_flow", "ver_catalogo", "validar"):
        assert call(draft, name)[0]
    assert not draft.changed
    assert "artifacts.save_artifact" in call(draft, "ver_catalogo")[1]


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

    async def fake_chat(message, flow, spec, prompts, session_id=None):
        seen.update(message=message, flow=flow, n_prompts=len(prompts), session=session_id)
        yield {"type": "text", "text": "Olá"}
        yield {"type": "done", "changed": False, "doc": None}

    monkeypatch.setattr(assistant, "chat", fake_chat)
    c = TestClient(create_app(tmp_path, allowed_hosts=["testserver"]))
    spec, _ = _load()
    body = {"flow": "pipeline", "message": "oi", "spec": spec, "session_id": "abc-1"}
    assert c.post("/api/assist", json=body).status_code == 403  # sem cabeçalho
    r = c.post("/api/assist", json=body, headers=H)
    events = [json.loads(line) for line in r.text.splitlines()]
    assert [e["type"] for e in events] == ["text", "done"]
    assert seen == {"message": "oi", "flow": "pipeline", "n_prompts": 8, "session": "abc-1"}
    assert (tmp_path / "pipeline/flow.yaml").read_text(encoding="utf-8") == before
    bad = {**body, "session_id": "../x"}
    assert c.post("/api/assist", json=bad, headers=H).status_code == 422

"""Flow builder: format validation, compilation and security rules."""
import copy
from pathlib import Path

import pytest
import yaml

from agent_flow_studio.builder import FlowError, compile_flow, list_flows, load_spec
from agent_flow_studio.builder.compile import flow_section
from agent_flow_studio.builder.spec import FlowSpec

MINIMAL = {
    "name": "mini",
    "title": "Mini",
    "task": "Do {input}.",
    "start": "a",
    "agents": {
        "a": {"description": "first", "prompt": "Read the ticket.",
              "tools": ["artifacts.read_artifact", "artifacts.save_artifact"],
              "writes": [{"name": "01_a.json"}]},
        "b": {"description": "second", "prompt": "Conclude.", "tools": ["artifacts.read_artifact"]},
    },
    "gates": {"ok": {"kind": "human", "label": "Approve"}},
    "edges": [{"from": "a", "to": "b"}, {"from": "b", "to": "ok", "when": "finished"}],
}
EXT = {"type": "stdio", "command": "npx", "args": ["-y", "srv"], "tools": ["list_items"],
       "env": {"TOKEN": "${EXT_TOKEN}"}, "approved": True, "read_only": True}


def _spec(**changes) -> dict:
    d = copy.deepcopy(MINIMAL)
    d.update(changes)
    return d


def _err(data: dict) -> str:
    with pytest.raises(ValueError) as e:
        FlowSpec.model_validate(data)
    return str(e.value)


def test_minimal_flow_compiles(tmp_path: Path):
    spec = FlowSpec.model_validate(MINIMAL)
    cf = compile_flow(spec, tmp_path)
    assert set(cf.options.agents) == {"a", "b"}
    assert sorted(cf.options.mcp_servers) == ["artifacts", "flow_control"]
    assert "mcp__flow_control__reach_gate" in cf.options.allowed_tools
    assert all("reach_gate" not in t for ts in cf.agent_tools.values() for t in ts)
    assert "(gate `ok`)" in cf.options.system_prompt and "reach_gate" in cf.options.system_prompt
    assert cf.agent_tools["b"] == ["mcp__artifacts__read_artifact"]
    assert "STOP and ask for human approval: **Approve**" in cf.options.system_prompt
    assert cf.task("INC1") == "Do INC1."
    with pytest.raises(FlowError):
        cf.task("../etc")


def test_validation_compile_has_no_global_side_effects(tmp_path: Path):
    from agent_flow_studio.tools import artifacts
    before = (artifacts._names, artifacts._contracts)
    compile_flow(FlowSpec.model_validate(MINIMAL), tmp_path, configure_artifacts=False)
    assert (artifacts._names, artifacts._contracts) == before


def test_flows_dir_lists_ticket_access():
    assert "pipeline" in list_flows()
    spec, _ = load_spec("pipeline")
    assert spec.start == "triage"


@pytest.mark.parametrize("mutate,needle", [
    (lambda d: d.update(start="zz"), "start 'zz'"),
    (lambda d: d["edges"].append({"from": "a", "to": "ghost"}), "'ghost' does not exist"),
    (lambda d: d["agents"]["a"]["tools"].append("fake.*"), "no *"),
    (lambda d: d["agents"]["a"]["tools"].append("fake.drop_table"), "does not exist in the catalog"),
    (lambda d: d["agents"]["b"].update(writes=[{"name": "x.json"}]), "does not have artifacts.save_artifact"),
    (lambda d: d["agents"]["a"]["writes"].append({"name": "y.json", "contract": "Magia"}),
     "unknown contract"),
    (lambda d: d["agents"]["a"]["writes"].append({"name": "../x.json"}), "invalid artifact name"),
    (lambda d: d["agents"].update(c={"description": "loose", "prompt": "x"}), "not reachable"),
    (lambda d: d["agents"]["a"].update(prompt_file="p.md"), "exactly one"),
    (lambda d: d.update(task="no marker"), "{input}"),
    (lambda d: d["gates"].update(a={"kind": "stop", "label": "x"}), "names repeated"),
    (lambda d: d["agents"]["a"].update(extra=1), "Extra inputs"),
])
def test_invalid_flows_rejected(mutate, needle):
    d = _spec()
    mutate(d)
    assert needle in _err(d)


def test_external_mcp_allowed_when_approved_and_read_only(tmp_path, monkeypatch):
    d = _spec(mcp_servers={"crm": EXT})
    d["agents"]["b"]["tools"].append("crm.list_items")
    monkeypatch.setenv("EXT_TOKEN", "test-secret")
    cf = compile_flow(FlowSpec.model_validate(d), tmp_path)
    assert cf.options.mcp_servers["crm"] == {
        "type": "stdio", "command": "npx", "args": ["-y", "srv"], "env": {"TOKEN": "test-secret"}}
    assert "mcp__crm__list_items" in cf.options.allowed_tools
    assert "mcp__crm__list_items" in cf.agent_tools["b"]
    assert "mcp__crm__list_items" not in cf.agent_tools["a"]


def test_external_mcp_secret_var_missing(tmp_path, monkeypatch):
    d = _spec(mcp_servers={"crm": EXT})
    d["agents"]["b"]["tools"].append("crm.list_items")
    monkeypatch.delenv("EXT_TOKEN", raising=False)
    with pytest.raises(FlowError, match="EXT_TOKEN"):
        compile_flow(FlowSpec.model_validate(d), tmp_path)
    compile_flow(FlowSpec.model_validate(d), tmp_path, resolve_secrets=False)  # validate without secrets


@pytest.mark.parametrize("patch,needle", [
    ({"approved": False}, "not approved"),
    ({"read_only": False}, "read_only"),
    ({"tools": ["*"]}, "no wildcards"),
    ({"env": {"TOKEN": "abc123literal"}}, "secrets stay out of the flow"),
    ({"type": "http", "url": "http://insecure"}, "https://"),
])
def test_external_mcp_rules(patch, needle):
    ext = {**EXT, **patch}
    d = _spec(mcp_servers={"crm": ext})
    d["agents"]["b"]["tools"].append("crm.list_items")
    assert needle in _err(d)


def test_external_tool_not_in_allowlist():
    d = _spec(mcp_servers={"crm": EXT})
    d["agents"]["b"]["tools"].append("crm.delete_items")
    assert "is not in the allowlist" in _err(d)


def test_http_headers_must_reference_env():
    ext = {"type": "http", "url": "https://mcp.example", "tools": ["q"], "approved": True,
           "read_only": True, "headers": {"Authorization": "Bearer ${CRM_TOKEN}"}}
    d = _spec(mcp_servers={"crm": ext})
    d["agents"]["b"]["tools"].append("crm.q")
    FlowSpec.model_validate(d)
    ext["headers"] = {"Authorization": "Bearer abc"}
    assert "secrets" in _err(d)


def test_prompt_file_must_stay_inside_flow(tmp_path: Path):
    d = _spec()
    d["agents"]["a"] = {**d["agents"]["a"], "prompt": None, "prompt_file": "../outside.md"}
    with pytest.raises(FlowError, match="outside the flow folder"):
        compile_flow(FlowSpec.model_validate(d), tmp_path)


def test_yaml_roundtrip_and_flow_section(tmp_path: Path):
    flow_dir = tmp_path / "mini"
    flow_dir.mkdir()
    (flow_dir / "flow.yaml").write_text(yaml.safe_dump(MINIMAL, allow_unicode=True), encoding="utf-8")
    spec, _ = load_spec("mini", base=tmp_path)
    text = flow_section(spec)
    assert text.index("`a`") < text.index("`b`")
    assert "delegate to `b`" in text

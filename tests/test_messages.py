"""Server messages shown in the editor: every msg(code, template, …) in the code has a translation
in editor/src/i18n/server.<lang>.ts with the same placeholders (English needs none: the editor
shows the server text). Also: a code always has the same template, and Msg keeps the English text."""
import ast
import re
import string
from pathlib import Path

import pytest

from agent_flow_studio.messages import Msg, MsgError, msg, reason, to_json

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src" / "agent_flow_studio"
I18N = ROOT / "editor" / "src" / "i18n"
LANGS = ("pt", "es")
CALLS = {"msg", "m_", "Msg"}
# pydantic's own errors get a `pydantic.<type>` code in editor/validation.py (no msg() call)
PYDANTIC = {"missing", "extra_forbidden", "string_type", "int_type", "int_parsing", "float_type",
            "float_parsing", "bool_type", "bool_parsing", "dict_type", "list_type", "literal_error",
            "greater_than_equal", "less_than_equal", "string_too_short", "string_too_long",
            "too_short", "string_pattern_mismatch", "enum"}


def source_codes() -> dict[str, str]:
    codes: dict[str, str] = {}
    for f in SRC.rglob("*.py"):
        tree = ast.parse(f.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                    and node.func.id in CALLS and len(node.args) >= 2):
                continue
            code, tpl = node.args[0], node.args[1]
            if not (isinstance(code, ast.Constant) and isinstance(tpl, ast.Constant)):
                continue  # Msg(f"pydantic.{type}", …) in validation.py
            assert codes.get(code.value, tpl.value) == tpl.value, \
                f"{code.value}: two different templates in the code ({f.name})"
            codes[code.value] = tpl.value
    return codes


def placeholders(text: str) -> set[str]:
    return {name for _, name, _, _ in string.Formatter().parse(text) if name}


def table(lang: str) -> dict[str, str]:
    text = (I18N / f"server.{lang}.ts").read_text(encoding="utf-8")
    return {m.group(1): bytes(m.group(2), "utf-8").decode("unicode_escape").encode("latin-1")
            .decode("utf-8") for m in re.finditer(r'^\s*"([a-z_.]+)":\s*"((?:[^"\\]|\\.)*)",?\s*$',
                                                  text, re.MULTILINE)}


CODES = source_codes()


def test_there_are_codes():
    assert len(CODES) > 100 and "type.required" in CODES and "spec.edge_reason" in CODES


@pytest.mark.parametrize("lang", LANGS)
def test_every_code_is_translated(lang):
    t = table(lang)
    missing = sorted(set(CODES) - set(t))
    assert not missing, f"server.{lang}.ts misses: {missing}"
    extra = sorted(k for k in set(t) - set(CODES)
                   if not (k.startswith("pydantic.") and k.removeprefix("pydantic.") in PYDANTIC))
    assert not extra, f"server.{lang}.ts has codes that no longer exist: {extra}"
    for code, tpl in CODES.items():
        assert placeholders(t[code]) == placeholders(tpl), f"{lang} {code}: placeholders differ"


def test_msg_is_the_english_text():
    inner = msg("type.required", "required")
    m = msg("type.item", "item {n}: {reason}", n=2, reason=inner)
    assert m == "item 2: required" and isinstance(m, str)
    assert to_json(m) == {"code": "type.item",
                          "params": {"n": "2", "reason": {"code": "type.required", "params": {}}}}
    assert reason(MsgError(inner)) is inner and reason(ValueError("plain")) == "plain"
    assert to_json("plain") is None and isinstance(Msg("a.b", "x"), str)


# --- the API sends code + params next to the English text ---------------------------------------
def test_validation_errors_carry_codes():
    import yaml

    from agent_flow_studio.editor.validation import _check

    flow = ROOT / "tests" / "fixtures" / "flows" / "parallel" / "flow.yaml"
    raw = yaml.safe_load(flow.read_text(encoding="utf-8"))
    raw["edges"][0]["if"] = "planner.nope > 1"
    errors = _check(raw, {})["errors"]
    edge = next(e for e in errors if e.get("code") == "spec.edge_reason")
    assert edge["msg"].startswith("edge planner→researcher_a: condition: agent 'planner'")
    assert edge["params"]["reason"]["code"] == "spec.condition_output"
    # pydantic's own errors stop before the graph checks: they get a pydantic.<type> code
    raw["agents"]["writer"]["extra_field"] = 1
    errors = _check(raw, {})["errors"]
    assert any(e.get("code") == "pydantic.extra_forbidden" for e in errors)


def test_run_form_errors_carry_codes(tmp_path):
    import shutil

    from agent_flow_studio.editor.runner import RunError, Runner

    shutil.copytree(ROOT / "tests" / "fixtures" / "flows" / "parallel", tmp_path / "flows/parallel")
    rn = Runner(tmp_path / "flows")
    with pytest.raises(RunError) as e:
        rn.start("parallel", inputs={"depth": "huge", "nope": 1})
    d = e.value.detail
    assert d["inputs"]["topic"] == "required" and d["codes"]["topic"]["code"] == "type.required"
    assert d["codes"]["depth"]["code"] == "type.choice" and "quick" in d["codes"]["depth"]["params"]["options"]
    assert d["codes"]["nope"]["code"] == "run.not_flow_input"

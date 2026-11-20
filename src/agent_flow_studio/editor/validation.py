"""Validation shared by the editor server and the assistant (format, graph and prompts)."""
from __future__ import annotations

import os
import re
import tempfile
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from ..builder.compile import FlowError, compile_flow, flow_section
from ..builder.library import origin_status
from ..builder.spec import FlowSpec
from ..messages import Msg, MsgError, to_json


# --- structured errors (so the editor can highlight nodes) ----------------------------------------
def _nodes_in(text: str, nodes: set[str]) -> list[str]:
    return sorted(n for n in nodes if re.search(rf"(?<![A-Za-z0-9_]){re.escape(n)}(?![A-Za-z0-9_])",
                                                text))


def _parts(e: dict[str, Any]) -> list[str]:
    """The messages of one pydantic error: the Msg of a MsgError, or pydantic's own text with a
    `pydantic.<type>` code (so the editor can translate it too)."""
    err = (e.get("ctx") or {}).get("error")
    if isinstance(err, MsgError):
        return list(err.msgs)
    text = str(e["msg"]).removeprefix("Value error, ")
    if e.get("type") == "value_error":
        return text.split("; ")
    ctx = {k: v for k, v in (e.get("ctx") or {}).items() if k != "error"}
    return [Msg(f"pydantic.{e['type']}", text.replace("{", "{{").replace("}", "}}"),
                **{k: str(v) for k, v in ctx.items()})]


def _entry(loc: str, part: str, found: list[str]) -> dict[str, Any]:
    out: dict[str, Any] = {"loc": loc, "msg": str(part), "nodes": found}
    j = to_json(part)
    if j is not None:  # code + params: the editor shows it in the UI language
        out |= j
    return out


def _structured(exc: Exception, raw: dict[str, Any]) -> list[dict[str, Any]]:
    nodes = set(raw.get("agents") or {}) | set(raw.get("gates") or {})
    out: list[dict[str, Any]] = []
    if isinstance(exc, ValidationError):
        for e in exc.errors(include_url=False):
            loc = ".".join(str(p) for p in e["loc"])
            for part in _parts(e):
                # "agents not reachable from 'x': ['y']" → the culprit is what comes after ": "
                tail = _nodes_in(part.rsplit(": ", 1)[-1], nodes) if ": " in part else []
                found = (tail or _nodes_in(part, nodes)
                         or [p for p in map(str, e["loc"]) if p in nodes])
                out.append(_entry(loc, part, found))
    elif isinstance(exc, MsgError):
        for part in exc.msgs:
            out.append(_entry("", part, _nodes_in(str(part), nodes)))
    else:
        text = str(exc)
        out.append({"loc": "", "msg": text, "nodes": _nodes_in(text, nodes)})
    return out


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8", newline="\n")
    os.replace(tmp, path)


def _validate(raw: dict[str, Any], prompts: dict[str, str]) -> dict[str, Any]:
    """Validates format, graph and prompts (full compilation, without resolving secrets), plus the
    state of the agents copied from the library (`origins`)."""
    return _check(raw, prompts) | {"origins": origin_status(raw, prompts)}


def _check(raw: dict[str, Any], prompts: dict[str, str]) -> dict[str, Any]:
    try:
        spec = FlowSpec.model_validate(raw)
    except ValidationError as e:
        return {"ok": False, "errors": _structured(e, raw)}
    try:
        with tempfile.TemporaryDirectory() as tmp:
            for k, v in prompts.items():
                _atomic_write(Path(tmp) / k, v)
            # no global side effects: it can run at the same time as a run
            cf = compile_flow(spec, Path(tmp), resolve_secrets=False, configure_artifacts=False)
    except FlowError as e:
        return {"ok": False, "errors": _structured(e, raw)}
    return {"ok": True, "errors": [], "spec": spec, "flow_section": flow_section(spec),
            "allowed_tools": [t for t in cf.options.allowed_tools if t != "Agent"]}


def _public(result: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in result.items() if k != "spec"}


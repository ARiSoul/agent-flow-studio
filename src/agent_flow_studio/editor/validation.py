"""Validation shared by the editor server and the assistant (format, graph and prompts)."""
from __future__ import annotations

import os
import re
import tempfile
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from ..builder.compile import FlowError, compile_flow, flow_section
from ..builder.convert import orchestrator_limit
from ..builder.library import origin_status
from ..builder.spec import FlowSpec
from ..messages import Msg, MsgError, msg, to_json


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
    from .. import workspaces
    raw, unknown = workspaces.substitute(raw)  # {vars.x}/{roots.x} as the run will see them
    if unknown:
        err = workspaces.unknown_error(unknown).msgs[0]
        return {"ok": False, "errors": [{"loc": "", "msg": str(err), "nodes": [],
                                         **(to_json(err) or {})}]}
    prompts = {k: workspaces.substitute_text(v)[0] for k, v in prompts.items()}
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
    warnings = unused_inputs(spec, prompts)
    large = orchestrator_limit(spec)
    if large:
        warnings.append(_entry("execution", large, []))
    return {"ok": True, "errors": [], "warnings": warnings, "spec": spec,
            "flow_section": flow_section(spec),
            "allowed_tools": [t for t in cf.options.allowed_tools if t != "Agent"]}


def unused_inputs(spec: FlowSpec, prompts: dict[str, str]) -> list[dict[str, Any]]:
    """Warnings (the flow still runs): a typed flow input that nothing uses. In graph and
    orchestrator mode an agent only gets the flow inputs it declares (`inputs: {x: {from:
    input.x}}`) or that the `task` names (`{x}`), so an unused input never reaches a model."""
    import json

    if not spec.inputs:
        return []
    graph = json.dumps({"agents": {n: a.model_dump(mode="json", exclude={"description"})
                                   for n, a in spec.agents.items()},
                        "gates": {n: g.model_dump(mode="json") for n, g in spec.gates.items()},
                        "edges": [e.model_dump(mode="json") for e in spec.edges]})
    texts = [spec.task, spec.orchestrator.prompt or "", *prompts.values()]
    out: list[dict[str, Any]] = []
    for n, t in spec.inputs.items():
        word = re.escape(n)
        used = (re.search(rf"\binput\.{word}\b", graph)
                or any(re.search(rf"\{{{word}\}}", x) for x in texts)
                or any(o is not t and o.show_if and re.search(rf"\b{word}\b", o.show_if)
                       for o in spec.inputs.values()))
        if not used:
            m = msg("validate.input_unused",
                    "flow input `{name}` is not used: no agent has `inputs: {{{name}: {{from: "
                    "input.{name}}}}}` and the task does not name it, so no agent receives it",
                    name=n)
            out.append(_entry(f"inputs.{n}", m, []))
    return out


def _public(result: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in result.items() if k != "spec"}


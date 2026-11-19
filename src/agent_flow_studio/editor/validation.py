"""Validação partilhada pelo servidor do editor e pelo assistente (formato, grafo e prompts)."""
from __future__ import annotations

import os
import re
import tempfile
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from ..builder.compile import FlowError, compile_flow, flow_section
from ..builder.spec import FlowSpec


# --- erros estruturados (para o editor destacar nós) -------------------------------------------
def _nodes_in(text: str, nodes: set[str]) -> list[str]:
    return sorted(n for n in nodes if re.search(rf"(?<![A-Za-z0-9_]){re.escape(n)}(?![A-Za-z0-9_])",
                                                text))


def _structured(exc: Exception, raw: dict[str, Any]) -> list[dict[str, Any]]:
    nodes = set(raw.get("agents") or {}) | set(raw.get("gates") or {})
    out: list[dict[str, Any]] = []
    if isinstance(exc, ValidationError):
        for e in exc.errors(include_url=False):
            loc = ".".join(str(p) for p in e["loc"])
            msg = str(e["msg"]).removeprefix("Value error, ")
            for part in msg.split("; "):
                # "agentes sem ligação a partir de 'x': ['y']" → o culpado é o que vem após ": "
                tail = _nodes_in(part.rsplit(": ", 1)[-1], nodes) if ": " in part else []
                found = (tail or _nodes_in(part, nodes)
                         or [p for p in map(str, e["loc"]) if p in nodes])
                out.append({"loc": loc, "msg": part, "nodes": found})
    else:
        msg = str(exc)
        out.append({"loc": "", "msg": msg, "nodes": _nodes_in(msg, nodes)})
    return out


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8", newline="\n")
    os.replace(tmp, path)


def _validate(raw: dict[str, Any], prompts: dict[str, str]) -> dict[str, Any]:
    """Valida formato, grafo e prompts (compilação completa, sem resolver segredos)."""
    try:
        spec = FlowSpec.model_validate(raw)
    except ValidationError as e:
        return {"ok": False, "errors": _structured(e, raw)}
    try:
        with tempfile.TemporaryDirectory() as tmp:
            for k, v in prompts.items():
                _atomic_write(Path(tmp) / k, v)
            # sem efeitos globais: pode correr ao mesmo tempo que uma execução
            cf = compile_flow(spec, Path(tmp), resolve_secrets=False, configure_artifacts=False)
    except FlowError as e:
        return {"ok": False, "errors": _structured(e, raw)}
    return {"ok": True, "errors": [], "spec": spec, "fluxo": flow_section(spec),
            "allowed_tools": [t for t in cf.options.allowed_tools if t != "Agent"]}


def _public(result: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in result.items() if k != "spec"}


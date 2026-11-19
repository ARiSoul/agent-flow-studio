"""Guardar/ler resultados intermédios por execução (permite retomar e auditar)."""
from collections.abc import Iterable, Mapping
from typing import Any

from claude_agent_sdk import tool
from pydantic import ValidationError

from ..config import settings
from .common import err, ok
from .guards import safe_artifact_path

DRAFT_MARKER = "RASCUNHO — requer aprovação humana"
# Artefactos aceites no flow ativo: o compilador chama configure() com os declarados no flow.
_names: frozenset[str] = frozenset()
_contracts: dict[str, Any] = {}


def configure(names: Iterable[str], contracts: Mapping[str, Any]) -> None:
    global _names, _contracts
    _names, _contracts = frozenset(names), dict(contracts)


@tool("save_artifact", "Guarda um resultado intermédio da execução (ex.: 01_resumo.json).",
      {"run_id": str, "name": str, "content": str})
async def save_artifact(args):
    try:
        path = safe_artifact_path(settings.runs_dir, args["run_id"], args["name"])
    except ValueError as e:
        return err(str(e))
    if args["name"] not in _names:
        return err(f"Nome de artefacto não previsto: '{args['name']}'. "
                   f"Nomes válidos: {', '.join(sorted(_names))}.")
    contract = _contracts.get(args["name"])
    if contract is not None:
        try:
            contract.validate_json(args["content"])
        except ValidationError as e:
            return err(f"'{args['name']}' não cumpre o contrato; corrige e volta a guardar.\n"
                       f"{e.errors(include_url=False, include_input=False)}")
    content = args["content"]
    if path.suffix == ".md" and DRAFT_MARKER not in content:
        # Garantia determinística: todo o rascunho fica marcado, faça o modelo o que fizer.
        content = f"> **{DRAFT_MARKER}**\n\n{content}"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return ok(f"Guardado: {path}")


@tool("read_artifact", "Lê um resultado intermédio guardado da execução.", {"run_id": str, "name": str})
async def read_artifact(args):
    try:
        path = safe_artifact_path(settings.runs_dir, args["run_id"], args["name"])
    except ValueError as e:
        return err(str(e))
    if not path.exists():
        return err("Artefacto não existe.")
    return ok(path.read_text(encoding="utf-8"))

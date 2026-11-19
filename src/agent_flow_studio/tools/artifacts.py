"""Save/read intermediate results per run (allows resuming and auditing)."""
from collections.abc import Iterable, Mapping
from typing import Any

from claude_agent_sdk import tool
from pydantic import ValidationError

from ..config import settings
from .common import err, ok
from .guards import safe_artifact_path

DRAFT_MARKER = "DRAFT — requires human approval"
# Artifacts accepted in the active flow: the compiler calls configure() with those it declares.
_names: frozenset[str] = frozenset()
_contracts: dict[str, Any] = {}


def configure(names: Iterable[str], contracts: Mapping[str, Any]) -> None:
    global _names, _contracts
    _names, _contracts = frozenset(names), dict(contracts)


@tool("save_artifact", "Saves an intermediate result of the run (e.g. 01_summary.json).",
      {"run_id": str, "name": str, "content": str})
async def save_artifact(args):
    try:
        path = safe_artifact_path(settings.runs_dir, args["run_id"], args["name"])
    except ValueError as e:
        return err(str(e))
    if args["name"] not in _names:
        return err(f"Unexpected artifact name: '{args['name']}'. "
                   f"Valid names: {', '.join(sorted(_names))}.")
    contract = _contracts.get(args["name"])
    if contract is not None:
        try:
            contract.validate_json(args["content"])
        except ValidationError as e:
            return err(f"'{args['name']}' does not meet its contract; fix it and save again.\n"
                       f"{e.errors(include_url=False, include_input=False)}")
    content = args["content"]
    if path.suffix == ".md" and DRAFT_MARKER not in content:
        # Deterministic guarantee: every draft is marked, whatever the model does.
        content = f"> **{DRAFT_MARKER}**\n\n{content}"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return ok(f"Saved: {path}")


@tool("read_artifact", "Reads a saved intermediate result of the run.",
      {"run_id": str, "name": str})
async def read_artifact(args):
    try:
        path = safe_artifact_path(settings.runs_dir, args["run_id"], args["name"])
    except ValueError as e:
        return err(str(e))
    if not path.exists():
        return err("Artifact does not exist.")
    return ok(path.read_text(encoding="utf-8"))

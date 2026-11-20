"""Agent library: generic agents that are copied into flows and tuned there.

A library agent is a folder `<id>/` with `agent.yaml` (an agent as in a flow, plus an optional
`title`, without `prompt`/`prompt_file`) and `prompt.md`. Inputs are declared without `from`:
each flow links them itself. Copying an agent into a flow is a plain copy plus an `origin`
(`{agent, hash}`), so the editor can show whether the copy was changed in the flow and whether the
library has a newer version; nothing in a flow changes when the library changes.

Sources: core agents in `agent_flow_studio/agents/` (generic, public), `extensions/<name>/agents/`
(outside git) and the local library `agents/` (or `AFS_AGENTS_DIR`, outside git), the only one the
editor writes to. The library never keeps `autonomy: auto` (that choice is made per flow by a
person) nor flow-specific fields (`from`, `join`, `max_runs`, `origin`).
"""
from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError

from ..extensions import LOADED
from .spec import SLUG, AgentSpec

CORE_DIR = Path(__file__).resolve().parent.parent / "agents"
AGENT_ID = re.compile(SLUG)
MAX_PROMPT = 100_000
FLOW_ONLY = ("origin", "join", "max_runs", "prompt", "prompt_file")  # not part of the library
_TITLE = re.compile(r"^[^\x00-\x1f]{1,80}$")


class LibraryError(ValueError):
    pass


@dataclass(frozen=True)
class LibraryAgent:
    id: str
    title: str
    source: str  # "core", "local" or the extension name
    folder: Path
    spec: AgentSpec
    prompt: str
    hash: str


def local_dir() -> Path:
    return Path(os.getenv("AFS_AGENTS_DIR", "agents"))


def agent_dirs() -> list[tuple[str, Path]]:
    return [("core", CORE_DIR), *LOADED.agent_dirs, ("local", local_dir())]


def _norm_prompt(text: str) -> str:
    return text.replace("\r\n", "\n").strip()


def fingerprint(agent: AgentSpec, prompt: str) -> str:
    """What makes an agent "the same": its definition and prompt, without the flow-specific parts
    (input sources, join, loops, origin), the prompt location and the icon."""
    data = agent.model_dump(mode="json", by_alias=True, exclude={*FLOW_ONLY, "icon"})
    for i in data.get("inputs", {}).values():
        i.pop("from", None)
    data["prompt"] = _norm_prompt(prompt)
    blob = json.dumps(data, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


def _clean(raw: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    """The library form of an agent (raw dict): no flow-only fields, inputs without `from`, no
    `auto` autonomy. Returns it and notes about what was left out."""
    notes: list[str] = []
    out = {k: v for k, v in raw.items() if k not in FLOW_ONLY and k != "title"}
    inputs = out.get("inputs") or {}
    if isinstance(inputs, dict):
        out["inputs"] = {n: {k: v for k, v in (i or {}).items() if k != "from"}
                         for n, i in inputs.items()}
    if out.get("autonomy") == "auto":
        del out["autonomy"]
        notes.append("autonomy 'auto' is not kept in the library (each flow chooses it)")
    return out, notes


def _parse(agent_id: str, raw: Any, prompt: str, where: str) -> AgentSpec:
    if not isinstance(raw, dict):
        raise LibraryError(f"{where}: agent.yaml must be a mapping")
    if "prompt" in raw or "prompt_file" in raw:
        raise LibraryError(f"{where}: the prompt goes in prompt.md, not in agent.yaml")
    if any(isinstance(i, dict) and "from" in i for i in (raw.get("inputs") or {}).values()):
        raise LibraryError(f"{where}: library inputs have no 'from' (each flow links them)")
    if raw.get("autonomy") == "auto":
        raise LibraryError(f"{where}: autonomy 'auto' is not allowed in the library")
    bad = set(FLOW_ONLY) & set(raw)
    if bad:
        raise LibraryError(f"{where}: flow-only field(s) {sorted(bad)}")
    if not _norm_prompt(prompt):
        raise LibraryError(f"{where}: prompt.md is empty")
    try:
        return AgentSpec.model_validate({k: v for k, v in raw.items() if k != "title"}
                                        | {"prompt": prompt})
    except ValidationError as e:
        raise LibraryError(f"{where}: {e}") from e


def _load_one(source: str, d: Path) -> LibraryAgent:
    where = f"library agent {d.name!r} ({source})"
    if not AGENT_ID.fullmatch(d.name):
        raise LibraryError(f"{where}: invalid folder name (use {SLUG})")
    try:
        raw = yaml.safe_load((d / "agent.yaml").read_text(encoding="utf-8")) or {}
        prompt = (d / "prompt.md").read_text(encoding="utf-8") if (d / "prompt.md").is_file() else ""
    except (OSError, yaml.YAMLError) as e:
        raise LibraryError(f"{where}: {e}") from e
    spec = _parse(d.name, raw, prompt, where)
    title = str(raw.get("title") or d.name) if isinstance(raw, dict) else d.name
    return LibraryAgent(d.name, title, source, d, spec, prompt, fingerprint(spec, prompt))


def list_agents() -> dict[str, LibraryAgent]:
    out: dict[str, LibraryAgent] = {}
    for source, base in agent_dirs():
        if not base.is_dir():
            continue
        for d in sorted(p for p in base.iterdir() if (p / "agent.yaml").is_file()):
            a = _load_one(source, d)
            if a.id in out:
                raise LibraryError(f"library agent {a.id!r} exists twice ({out[a.id].source}, "
                                   f"{source})")
            out[a.id] = a
    return out


def agent_data(a: LibraryAgent) -> dict[str, Any]:
    """The agent as it goes into a flow (raw dict, prompt inline; the editor moves it to a file)."""
    data = a.spec.model_dump(mode="json", by_alias=True, exclude_defaults=True)
    data.pop("prompt_file", None)
    return data | {"prompt": a.prompt}


def agents_dict() -> list[dict[str, Any]]:
    """For the editor and the CLI."""
    return [{"id": a.id, "title": a.title, "source": a.source, "hash": a.hash,
             "description": " ".join(a.spec.description.split()), "icon": a.spec.icon,
             "agent": agent_data(a)}
            for a in list_agents().values()]


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8", newline="\n")
    os.replace(tmp, path)


def save_local(agent_id: str, title: str, raw: dict[str, Any], prompt: str,
               overwrite: bool = False) -> tuple[LibraryAgent, list[str]]:
    """Saves an agent (from a flow) to the local library. Core and extension agents cannot be
    replaced; an existing local agent only with `overwrite`."""
    if not AGENT_ID.fullmatch(agent_id):
        raise LibraryError(f"invalid library agent id {agent_id!r} (use {SLUG})")
    title = title.strip() or agent_id
    if not _TITLE.fullmatch(title):
        raise LibraryError("title: one line, up to 80 characters")
    if len(prompt) > MAX_PROMPT:
        raise LibraryError("prompt too long")
    existing = list_agents().get(agent_id)
    if existing and existing.source != "local":
        raise LibraryError(f"{agent_id!r} is a {existing.source} library agent; choose another id")
    if existing and not overwrite:
        raise LibraryError(f"library agent {agent_id!r} already exists")
    data, notes = _clean(raw)
    spec = _parse(agent_id, data, prompt, f"library agent {agent_id!r}")
    folder = local_dir() / agent_id
    body = {"title": title, **spec.model_dump(mode="json", by_alias=True, exclude_defaults=True,
                                              exclude={"prompt", "prompt_file"})}
    header = (f"# Library agent: {title.replace(chr(10), ' ')}\n"
              "# Copied into flows from the editor; the prompt is in prompt.md.\n")
    _atomic_write(folder / "agent.yaml",
                  header + yaml.safe_dump(body, allow_unicode=True, sort_keys=False, width=100))
    _atomic_write(folder / "prompt.md", _norm_prompt(prompt) + "\n")
    return _load_one("local", folder), notes


def origin_status(raw: dict[str, Any], prompts: dict[str, str]) -> dict[str, dict[str, Any]]:
    """For each agent of a flow copied from the library: is it still the same as when it was
    copied (`changed`), and does the library have another version now (`update`)."""
    agents = raw.get("agents") if isinstance(raw, dict) else None
    if not isinstance(agents, dict):
        return {}
    try:
        lib = list_agents()
    except LibraryError:
        lib = {}
    out: dict[str, dict[str, Any]] = {}
    for name, a in agents.items():
        o = a.get("origin") if isinstance(a, dict) else None
        if not isinstance(o, dict) or not isinstance(o.get("agent"), str):
            continue
        la = lib.get(o["agent"])
        changed: bool | None
        try:
            spec = AgentSpec.model_validate(a)
            text = spec.prompt if spec.prompt is not None else prompts.get(spec.prompt_file or "")
            changed = None if text is None else fingerprint(spec, text) != o.get("hash")
        except ValidationError:
            changed = None  # invalid for now: the flow errors already say why
        out[name] = {"agent": o["agent"], "title": la.title if la else o["agent"],
                     "source": la.source if la else None, "missing": la is None,
                     "changed": changed, "update": bool(la and la.hash != o.get("hash"))}
    return out

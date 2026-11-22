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
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError

from ..extensions import LOADED, extension_agent_dirs, extension_root
from ..publish import PUBLIC, check_public
from .catalog import BUILTIN_TOOLS
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
    from .. import workspaces
    ws = workspaces.current()
    own = [] if ws.studio else [("workspace", ws.agents_dir)]  # the workspace's own first
    return [*own, ("core", CORE_DIR), *extension_agent_dirs(), ("local", local_dir())]


def _source(location: str) -> str:
    """The "public" location is the built-in library (source "core")."""
    return "core" if location == PUBLIC else location


def save_dir(location: str) -> Path:
    """Where "Save to library" writes: the local library, an extension's agents/ folder, or the
    built-in library in the package (public: git publishes it)."""
    if location == "local":
        return local_dir()
    if location == "workspace":
        from .. import workspaces
        ws = workspaces.current()
        if ws.studio:
            raise LibraryError("no workspace is open")
        return ws.agents_dir
    if location == PUBLIC:
        return CORE_DIR
    if location not in LOADED.names:
        raise LibraryError(f"unknown location {location!r} (local, public or a loaded extension)")
    return extension_root(location) / "agents"


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
    unknown = [t for t in raw.get("tools") or [] if t not in BUILTIN_TOOLS]
    if unknown:  # external MCP tools belong to a flow (approved there), not to the library
        raise LibraryError(f"{where}: tool(s) not in the catalog: {', '.join(map(str, unknown))}")
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
            if a.id in out and out[a.id].source == "workspace":
                continue  # a workspace's agent wins over a studio one with the same id
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
             "location": PUBLIC if a.source == "core" else a.source,
             "description": " ".join(a.spec.description.split()), "icon": a.spec.icon,
             "agent": agent_data(a), "agent_yaml": agent_yaml(a), "prompt": a.prompt}
            for a in list_agents().values()]


def _unique(used: set[str], base: str) -> str:
    if base not in used:
        return base
    return next(f"{base}_{i}" for i in range(2, 1000) if f"{base}_{i}" not in used)


def copy_into(spec: dict[str, Any], prompts: dict[str, str], a: LibraryAgent,
              name: str | None = None) -> str:
    """Copies a library agent into a raw flow (prompt in prompts/<name>.md, with an origin).
    Returns the node name: `name`, or the library id (made unique) when not given."""
    used = set(spec.get("agents") or {}) | set(spec.get("gates") or {}) | set(
        spec.get("mcp_servers") or {})
    if name is not None:
        if not AGENT_ID.fullmatch(name):
            raise LibraryError(f"invalid node name {name!r} (use {SLUG})")
        if name in used:
            raise LibraryError(f"a node {name!r} already exists")
    node = name or _unique(used, a.id)
    files = {p.removeprefix("prompts/").removesuffix(".md") for p in prompts}
    path = f"prompts/{_unique(files, node)}.md"
    prompts[path] = a.prompt
    data = {k: v for k, v in agent_data(a).items() if k != "prompt"}
    spec.setdefault("agents", {})[node] = data | {"prompt_file": path,
                                                  "origin": {"agent": a.id, "hash": a.hash}}
    return node


def refresh_copy(spec: dict[str, Any], prompts: dict[str, str], name: str, a: LibraryAgent) -> None:
    """Replaces a copied agent with the current library version, keeping what belongs to the flow:
    where each input comes from, join, max_runs and the icon (and the prompt file's path)."""
    old = (spec.get("agents") or {}).get(name)
    if not isinstance(old, dict):
        raise LibraryError(f"agent {name!r} does not exist")
    new = {k: v for k, v in agent_data(a).items() if k != "prompt"}
    path = old.get("prompt_file") or f"prompts/{name}.md"
    prompts[path] = a.prompt
    old_inputs = old.get("inputs") or {}
    for k, i in (new.get("inputs") or {}).items():
        src = (old_inputs.get(k) or {}).get("from")
        if src:
            i["from"] = src
    for k in ("join", "max_runs", "icon"):
        if old.get(k) is not None:
            new[k] = old[k]
    spec["agents"][name] = new | {"prompt_file": path, "origin": {"agent": a.id, "hash": a.hash}}


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8", newline="\n")
    os.replace(tmp, path)


@dataclass(frozen=True)
class Checked:
    """A library agent checked but not saved (see `check`)."""
    id: str
    title: str
    spec: AgentSpec
    data: dict[str, Any]  # library form (raw), without the prompt
    prompt: str
    notes: list[str]
    exists: bool  # a local library agent with this id exists (saving replaces it)


def check(agent_id: str, title: str, raw: dict[str, Any], prompt: str,
          location: str = "local") -> Checked:
    """Everything `save_local` checks, without writing: id, title, prompt, the agent itself, and
    that the id is not a core or extension agent."""
    if not AGENT_ID.fullmatch(agent_id):
        raise LibraryError(f"invalid library agent id {agent_id!r} (use {SLUG})")
    title = title.strip() or agent_id
    if not _TITLE.fullmatch(title):
        raise LibraryError("title: one line, up to 80 characters")
    if len(prompt) > MAX_PROMPT:
        raise LibraryError("prompt too long")
    existing = list_agents().get(agent_id)
    save_dir(location)  # a known location
    if existing and existing.source != _source(location) and location == "workspace":
        existing = None  # a workspace's own agent may take the id of a studio one (it wins there)
    if existing and existing.source != _source(location):
        where = "public (built-in)" if existing.source == "core" else existing.source
        raise LibraryError(f"{agent_id!r} is a {where} library agent; choose another id")
    data, notes = _clean(raw)
    spec = _parse(agent_id, data, prompt, f"library agent {agent_id!r}")
    return Checked(agent_id, title, spec, data, prompt, notes, existing is not None)


def save_local(agent_id: str, title: str, raw: dict[str, Any], prompt: str,
               overwrite: bool = False, location: str = "local") -> tuple[LibraryAgent, list[str]]:
    """Saves an agent (from a flow) to the local library. Core and extension agents cannot be
    replaced; an existing local agent only with `overwrite`."""
    c = check(agent_id, title, raw, prompt, location)
    if c.exists and not overwrite:
        raise LibraryError(f"library agent {agent_id!r} already exists")
    title, spec, notes = c.title, c.spec, c.notes
    if location == PUBLIC:  # git publishes it: no private words in it
        check_public("\n".join([agent_id, title, json.dumps(c.data, ensure_ascii=False), prompt]))
    folder = save_dir(location) / agent_id
    body = {"title": title, **spec.model_dump(mode="json", by_alias=True, exclude_defaults=True,
                                              exclude={"prompt", "prompt_file"})}
    header = (f"# Library agent: {title.replace(chr(10), ' ')}\n"
              "# Copied into flows from the editor; the prompt is in prompt.md.\n")
    _atomic_write(folder / "agent.yaml",
                  header + yaml.safe_dump(body, allow_unicode=True, sort_keys=False, width=100))
    _atomic_write(folder / "prompt.md", _norm_prompt(prompt) + "\n")
    return _load_one(_source(location), folder), notes


def delete_agent(agent_id: str) -> str:
    """Deletes a library agent (its folder); returns where it was."""
    a = list_agents().get(agent_id)
    if a is None:
        raise LibraryError(f"library agent {agent_id!r} does not exist")
    shutil.rmtree(a.folder)
    return a.source


def move_agent(agent_id: str, location: str) -> LibraryAgent:
    """Moves a library agent between this machine, an extension and public (the built-in one)."""
    a = list_agents().get(agent_id)
    if a is None:
        raise LibraryError(f"library agent {agent_id!r} does not exist")
    if _source(location) == a.source:
        return a
    target = save_dir(location) / agent_id
    if target.exists():
        raise LibraryError(f"{target} already exists")
    if location == PUBLIC:
        check_public("\n".join(f.read_text(encoding="utf-8") for f in sorted(a.folder.glob("*"))
                               if f.is_file()))
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(a.folder), target)
    return _load_one(_source(location), target)


def agent_yaml(a: LibraryAgent) -> str:
    """The definition as the library window edits it (everything but the title and the prompt)."""
    data = a.spec.model_dump(mode="json", by_alias=True, exclude_defaults=True,
                             exclude={"prompt", "prompt_file"})
    return yaml.safe_dump(data, allow_unicode=True, sort_keys=False, width=100)


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

"""Workspaces: one project's flows, agents, connectors, variables and environments (like Postman).

A workspace is a folder with `workspace.yaml`, `flows/`, `agents/` and `connections.yaml`. It lives
either inside a repository (`<repo>/.agent-flow/`, it travels with the code) or in the studio
(`workspaces/<name>/`, outside git). The machine keeps a registry of the workspaces it knows
(`~/.agent-flow-studio/workspaces.json`, like VS Code's recent folders).

The default "studio" workspace is what the studio always had: `flows/`, the extensions, the public
flows, the agent library and `connections.yaml`. Without a workspace nothing changes.

Variables are plain values (never secrets: secret-like text is refused); an environment's variables
override the workspace's. Flows use them as `{vars.<name>}` and `{roots.<name>}` (a root is a
folder of the workspace). The current workspace and environment are a context variable: the editor
sets them per request, a run keeps them for its whole life.
"""
from __future__ import annotations

import json
import os
import re
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .messages import MsgError, msg

STUDIO = "studio"
WS_FILE = "workspace.yaml"
REPO_DIR = ".agent-flow"  # a workspace inside a repository
NAME = re.compile(r"^[a-z][a-z0-9_-]{0,39}$")
KEY = re.compile(r"^[a-z][a-z0-9_]{0,39}$")
EnvKind = Literal["dev", "staging", "prod"]
# a {vars.x} / {roots.x} reference in a text
# {vars.x} / {roots.x}; {{vars.x}} stays literal (as in message templates)
REF = re.compile(r"(?<!\{)\{(vars|roots)\.([a-z][a-z0-9_]{0,39})\}(?!\})")
MAX_VALUE = 2000


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


def _secret_like(v: str) -> bool:
    from .connections.model import _SECRET_LIKE  # the same rule as connector instructions

    return bool(_SECRET_LIKE.search(v))


def _check_vars(vars_: dict[str, str]) -> dict[str, str]:
    for k, v in vars_.items():
        if not KEY.fullmatch(k):
            raise MsgError(msg("ws.var_name", "variable {name}: lowercase letters, digits and _",
                               name=repr(k)))
        if len(v) > MAX_VALUE:
            raise MsgError(msg("ws.var_long", "variable {name}: more than {n} characters",
                               name=repr(k), n=MAX_VALUE))
        if _secret_like(v):
            raise MsgError(msg("ws.var_secret",
                               "variable {name} looks like a secret: secrets go in environment "
                               "variables or the keyring, never in a workspace", name=repr(k)))
    return vars_


class Root(_Strict):
    path: str = Field(min_length=1, max_length=500)  # relative to the workspace home, or absolute
    description: str = ""


class Environment(_Strict):
    kind: EnvKind = "dev"  # drives approvals (approve_prod) as a flow workspace environment does
    vars: dict[str, str] = Field(default_factory=dict)

    _v = field_validator("vars")(classmethod(lambda cls, v: _check_vars(v)))


class WorkspaceSpec(_Strict):
    version: Literal[1] = 1
    name: str = Field(pattern=NAME.pattern)
    title: str = ""
    description: str = ""
    roots: dict[str, Root] = Field(default_factory=dict)
    vars: dict[str, str] = Field(default_factory=dict)
    environments: dict[str, Environment] = Field(default_factory=dict)
    default_environment: str | None = None

    _v = field_validator("vars")(classmethod(lambda cls, v: _check_vars(v)))

    @model_validator(mode="after")
    def _check(self) -> WorkspaceSpec:
        for kind, names in (("roots", self.roots), ("environments", self.environments)):
            for n in names:
                if not KEY.fullmatch(n):
                    raise MsgError(msg("ws.key_name", "{name}: lowercase letters, digits and _",
                                       name=f"{kind}.{n}"))
        if self.name == STUDIO:
            raise MsgError(msg("ws.reserved", "the name 'studio' is the studio's own workspace"))
        if self.default_environment and self.default_environment not in self.environments:
            raise MsgError(msg("ws.default_env", "default_environment {name} is not an environment",
                               name=repr(self.default_environment)))
        return self


@dataclass
class Workspace:
    name: str
    folder: Path | None  # the folder with workspace.yaml (None: the studio)
    spec: WorkspaceSpec | None = None

    @property
    def studio(self) -> bool:
        return self.folder is None

    @property
    def where(self) -> Literal["studio", "repo", "local"]:
        """studio = the studio's own; repo = inside a repository; local = in the studio's
        workspaces/ folder."""
        if self.folder is None:
            return "studio"
        return "repo" if self.folder.name == REPO_DIR else "local"

    @property
    def home(self) -> Path:
        """What roots are relative to: the repository (in a repo) or the workspace folder."""
        assert self.folder is not None
        return self.folder.parent if self.folder.name == REPO_DIR else self.folder

    @property
    def flows_dir(self) -> Path:
        assert self.folder is not None
        return self.folder / "flows"

    @property
    def agents_dir(self) -> Path:
        assert self.folder is not None
        return self.folder / "agents"

    @property
    def connections_file(self) -> Path:
        assert self.folder is not None
        return self.folder / "connections.yaml"

    def env_names(self) -> list[str]:
        return list(self.spec.environments) if self.spec else []

    def default_env(self) -> str | None:
        if not self.spec or not self.spec.environments:
            return None
        return self.spec.default_environment or next(iter(self.spec.environments))

    def resolve(self, env: str | None) -> tuple[dict[str, str], dict[str, str], EnvKind | None]:
        """(variables, roots as absolute paths, environment kind) for an environment."""
        if self.spec is None:
            return {}, {}, None
        e = self.spec.environments.get(env or self.default_env() or "")
        values = {**self.spec.vars, **(e.vars if e else {})}
        roots = {n: str((self.home / r.path).expanduser().resolve())
                 for n, r in self.spec.roots.items()}
        return values, roots, (e.kind if e else None)

    def known_vars(self) -> set[str]:
        """Names a flow may use: the workspace's, or ones every environment defines."""
        if self.spec is None:
            return set()
        envs = [set(e.vars) for e in self.spec.environments.values()]
        return set(self.spec.vars) | (set.intersection(*envs) if envs else set())


STUDIO_WS = Workspace(STUDIO, None, None)
_ctx: ContextVar[tuple[Workspace, str | None]] = ContextVar("afs_workspace",
                                                             default=(STUDIO_WS, None))


def current() -> Workspace:
    return _ctx.get()[0]


def current_env() -> str | None:
    ws, env = _ctx.get()
    return env if env in ws.env_names() else ws.default_env()


def set_current(ws: Workspace, env: str | None = None) -> Any:
    """For a whole request (the server's middleware); give the token back to reset_current."""
    return _ctx.set((ws, env))


def reset_current(token: Any) -> None:
    _ctx.reset(token)


@contextmanager
def using(ws: Workspace, env: str | None = None) -> Iterator[Workspace]:
    """The workspace (and environment) everything in this block works in."""
    token = _ctx.set((ws, env))
    try:
        yield ws
    finally:
        _ctx.reset(token)


def scoped(d: Path) -> Path:
    """A folder of machine-local state (runs/chats, runs/.previous…) for the current workspace:
    the studio keeps `d`, a workspace gets `d/@<name>` (no clash with flow names)."""
    ws = current()
    return d if ws.studio else d / f"@{ws.name}"


def is_own(location: str) -> bool:
    """A location that belongs to the current workspace (not the studio-wide ones)."""
    return location == "workspace" and not current().studio


def resolved() -> tuple[dict[str, str], dict[str, str], EnvKind | None]:
    return current().resolve(current_env())


def env_kind() -> EnvKind | None:
    return resolved()[2]


# --- {vars.x} / {roots.x} in flows ---------------------------------------------------------------
def substitute_text(text: str) -> tuple[str, set[str]]:
    """The text with its {vars.x}/{roots.x} filled in, and the references it could not fill."""
    values, roots, _ = resolved()
    unknown: set[str] = set()

    def rep(m: re.Match[str]) -> str:
        table = values if m.group(1) == "vars" else roots
        if m.group(2) in table:
            return table[m.group(2)]
        unknown.add(f"{m.group(1)}.{m.group(2)}")
        return m.group(0)
    return REF.sub(rep, text), unknown


def substitute(data: Any, skip: tuple[str, ...] = ("if", "layout")) -> tuple[Any, set[str]]:
    """Every string of a flow (as data) with its references filled in; `if` expressions use
    `vars.x` as names and stay as they are; the layout has no text."""
    unknown: set[str] = set()

    def walk(node: Any) -> Any:
        if isinstance(node, str):
            text, u = substitute_text(node)
            unknown.update(u)
            return text
        if isinstance(node, list):
            return [walk(x) for x in node]
        if isinstance(node, dict):
            return {k: (v if k in skip else walk(v)) for k, v in node.items()}
        return node
    return walk(data), unknown


def unknown_error(unknown: set[str]) -> MsgError:
    ws = current()
    return MsgError(msg("spec.var_unknown",
                        "unknown variables in this workspace ({workspace}): {names}",
                        workspace=ws.name, names=", ".join(sorted(unknown))))


# --- storage and registry ----------------------------------------------------------------------
def studio_dir() -> Path:
    """Where the studio keeps its own workspaces (ignored by git)."""
    return Path(os.getenv("AFS_WORKSPACES_DIR", "workspaces"))


def registry_file() -> Path:
    return Path(os.getenv("AFS_WORKSPACES_FILE",
                          Path.home() / ".agent-flow-studio" / "workspaces.json"))


def _read_registry() -> list[dict[str, str]]:
    try:
        data = json.loads(registry_file().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    return [x for x in data if isinstance(x, dict) and "name" in x and "folder" in x] \
        if isinstance(data, list) else []


def _write_registry(items: list[dict[str, str]]) -> None:
    f = registry_file()
    f.parent.mkdir(parents=True, exist_ok=True)
    tmp = f.with_suffix(".tmp")
    tmp.write_text(json.dumps(items, indent=1, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, f)


def load(folder: Path) -> Workspace:
    """A workspace from its folder (the one with workspace.yaml)."""
    f = folder / WS_FILE
    try:
        data = yaml.safe_load(f.read_text(encoding="utf-8")) or {}
    except OSError as e:
        raise MsgError(msg("ws.missing", "no workspace at {path}", path=str(folder))) from e
    spec = WorkspaceSpec.model_validate(data)
    return Workspace(spec.name, folder.resolve(), spec)


def save(ws: Workspace) -> None:
    assert ws.folder is not None and ws.spec is not None
    ws.folder.mkdir(parents=True, exist_ok=True)
    data = ws.spec.model_dump(mode="json", exclude_defaults=True)
    data = {"version": 1, **data}
    head = ("# Workspace of Agent Flow Studio: flows/, agents/, connections.yaml (no secrets),\n"
            "# variables and environments. Variables are never secrets.\n")
    tmp = ws.folder / (WS_FILE + ".tmp")
    tmp.write_text(head + yaml.safe_dump(data, allow_unicode=True, sort_keys=False),
                   encoding="utf-8")
    os.replace(tmp, ws.folder / WS_FILE)


def register(ws: Workspace) -> None:
    assert ws.folder is not None
    items = [x for x in _read_registry() if x["name"] != ws.name]
    _write_registry([*items, {"name": ws.name, "folder": str(ws.folder)}])


def unregister(name: str) -> None:
    """Forgets a workspace on this machine (its files stay where they are)."""
    _write_registry([x for x in _read_registry() if x["name"] != name])


def folder_for(where: Literal["repo", "local"], name: str, repo: Path | None = None) -> Path:
    if where == "repo":
        if repo is None:
            raise MsgError(msg("ws.repo_needed", "a workspace in a repository needs its folder"))
        return repo.expanduser().resolve() / REPO_DIR
    return (studio_dir() / name).resolve()


def create(name: str, where: Literal["repo", "local"], repo: Path | None = None,
           title: str = "", description: str = "") -> Workspace:
    """A new workspace (in a repository or in the studio), registered on this machine."""
    if get_or_none(name) is not None or name == STUDIO:
        raise MsgError(msg("ws.exists", "a workspace {name} already exists", name=repr(name)))
    folder = folder_for(where, name, repo)
    if (folder / WS_FILE).exists():
        raise MsgError(msg("ws.folder_used", "there is already a workspace in {path}",
                           path=str(folder)))
    if where == "repo" and repo is not None and not repo.expanduser().is_dir():
        raise MsgError(msg("ws.repo_missing", "the folder {path} does not exist", path=str(repo)))
    roots = {"app": Root(path=".", description="This repository")} if where == "repo" else {}
    spec = WorkspaceSpec(name=name, title=title or name, description=description, roots=roots,
                         environments={"dev": Environment(kind="dev")}, default_environment="dev")
    ws = Workspace(name, folder, spec)
    save(ws)
    for d in (ws.flows_dir, ws.agents_dir):
        d.mkdir(parents=True, exist_ok=True)
    register(ws)
    return ws


def open_folder(path: Path) -> Workspace:
    """Adds an existing workspace (a repository with .agent-flow/, or a workspace folder)."""
    p = path.expanduser().resolve()
    folder = p / REPO_DIR if (p / REPO_DIR / WS_FILE).is_file() else p
    ws = load(folder)
    other = get_or_none(ws.name)
    if other is not None and other.folder != ws.folder:
        raise MsgError(msg("ws.exists", "a workspace {name} already exists", name=repr(ws.name)))
    register(ws)
    return ws


def get_or_none(name: str) -> Workspace | None:
    if name == STUDIO:
        return STUDIO_WS
    for x in _read_registry():
        if x["name"] == name:
            try:
                return load(Path(x["folder"]))
            except (MsgError, ValueError):
                return None
    return None


def get(name: str) -> Workspace:
    ws = get_or_none(name)
    if ws is None:
        raise MsgError(msg("ws.unknown", "unknown workspace {name}", name=repr(name)))
    return ws


def from_cli(value: str | None, env: str | None = None) -> Workspace:
    """--workspace (a registered name, or the folder of a workspace or of its repository) and
    --env (an environment of that workspace)."""
    ws = STUDIO_WS if not value or value == STUDIO else get_or_none(value)
    if ws is None:
        p = Path(value or "").expanduser()
        if not p.is_dir():
            raise MsgError(msg("ws.unknown", "unknown workspace {name}", name=repr(value)))
        ws = load(p / REPO_DIR if (p / REPO_DIR / WS_FILE).is_file() else p)
    if env and env not in ws.env_names():
        raise MsgError(msg("ws.env_unknown", "workspace {workspace} has no environment {env} "
                           "(it has: {names})", workspace=ws.name, env=repr(env),
                           names=", ".join(ws.env_names()) or "—"))
    return ws


def list_all() -> list[dict[str, Any]]:
    """The studio's own and the registered ones (a moved or deleted folder shows as missing)."""
    out: list[dict[str, Any]] = [{"name": STUDIO, "title": "Studio", "where": "studio",
                                  "folder": None, "ok": True, "environments": [],
                                  "default_environment": None}]
    for x in _read_registry():
        try:
            ws = load(Path(x["folder"]))
            assert ws.spec is not None
            out.append({"name": ws.name, "title": ws.spec.title or ws.name, "where": ws.where,
                        "folder": str(ws.folder), "ok": True, "environments": ws.env_names(),
                        "default_environment": ws.default_env()})
        except (MsgError, ValueError):
            out.append({"name": x["name"], "title": x["name"], "where": "missing",
                        "folder": x["folder"], "ok": False, "environments": [],
                        "default_environment": None})
    return out

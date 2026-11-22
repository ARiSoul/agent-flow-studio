"""Copy and duplicate across studio workspaces: flows, library agents, agents of a flow,
connector definitions, variables/environments and whole workspaces.

Before a copy, `copy_plan` lists what the item uses that the target lacks (variables, roots,
connectors, library agents, flows used as subflows or as input sources) with a choice per item:
copy it too, use the target's one with the same name, or leave it (the copy then shows validation
problems to fix). Secrets are never copied in files: a copied connector arrives without its secret,
unless the person chooses "use the same secret" (the keyring entry is copied on this machine,
never shown). Runs are not copied. Nothing is deleted: a replaced flow or agent goes to `.trash/`.
"""
from __future__ import annotations

import re
import shutil
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

import yaml

from .. import workspaces
from ..builder import library
from ..builder.compile import find_flow, flow_locations, flows_dir
from ..connections import store
from ..messages import MsgError, msg
from .yaml_io import dump_raw

Kind = Literal["flow", "agent", "flow_agent", "connector", "vars"]
DepKind = Literal["var", "root", "connector", "agent", "flow"]
Choice = Literal["copy", "target", "leave"]
NAME = re.compile(r"^[a-z0-9][a-z0-9_-]{0,39}$")  # flows (agents and connectors: no "-")
SLUG = re.compile(r"^[a-z][a-z0-9_]{0,39}$")
EXPR_REF = re.compile(r"\b(vars|roots)\.([a-z][a-z0-9_]{0,39})\b")
IGNORE = shutil.ignore_patterns("*.bak", "*.tmp", ".trash")


class CopyError(MsgError):
    pass



def _missing(name: str, ws: workspaces.Workspace) -> CopyError:
    return CopyError(msg("copy.missing", "{name} does not exist in {workspace}", name=name,
                workspace=ws.name))


def _clash(name: str, ws: workspaces.Workspace, suggestion: str) -> CopyError:
    return CopyError(msg("copy.clash", "{name} already exists in {workspace} (choose another "
                         "name, e.g. {suggestion}, or replace it)", name=repr(name),
                         workspace=ws.name, suggestion=suggestion))


@dataclass
class Item:
    """What is copied: `name` is a flow, a library agent, a connector, or (flow_agent) the agent
    `agent` of the flow `name`; vars: `vars` and `environments` by name."""
    kind: Kind
    name: str = ""
    agent: str | None = None  # flow_agent: the agent of the flow
    to_flow: str | None = None  # flow_agent: into this flow of the target (None: its library)
    vars: list[str] = field(default_factory=list)
    environments: list[str] = field(default_factory=list)


# --- reading things in a workspace --------------------------------------------------------------
def _studio_flows(studio_flows: Path | None) -> Path:
    return studio_flows or flows_dir()


def _flow_folder(ws: workspaces.Workspace, name: str, studio_flows: Path | None) -> Path | None:
    with workspaces.using(ws):
        found = find_flow(name, _studio_flows(studio_flows))
    return found[1] if found else None


def _flow_target_dir(ws: workspaces.Workspace, studio_flows: Path | None) -> Path:
    """Where a copied flow goes: the workspace's flows/, or the studio's own flows/ (this machine,
    never public: publishing stays a separate, checked step)."""
    with workspaces.using(ws):
        return flow_locations(_studio_flows(studio_flows))[0][1]


def _read_flow(d: Path) -> tuple[dict[str, Any], dict[str, str]]:
    raw = yaml.safe_load((d / "flow.yaml").read_text(encoding="utf-8")) or {}
    if not isinstance(raw, dict):
        raise CopyError(msg("copy.flow_invalid", "flow {name}: flow.yaml must be a mapping",
                            name=d.name))
    prompts = {str(p.relative_to(d)).replace("\\", "/"): p.read_text(encoding="utf-8")
               for p in sorted((d / "prompts").glob("*.md"))} if (d / "prompts").is_dir() else {}
    return raw, prompts


def _agents_of(ws: workspaces.Workspace) -> dict[str, library.LibraryAgent]:
    with workspaces.using(ws):
        return library.list_agents()


def _conns_of(ws: workspaces.Workspace) -> dict[str, Any]:
    with workspaces.using(ws):
        return store.load()


# --- dependencies -------------------------------------------------------------------------------
def _strings(node: Any) -> list[tuple[str | None, str]]:
    """Every string of a data tree with the key it is under."""
    out: list[tuple[str | None, str]] = []

    def walk(n: Any, key: str | None) -> None:
        if isinstance(n, str):
            out.append((key, n))
        elif isinstance(n, list):
            for x in n:
                walk(x, key)
        elif isinstance(n, dict):
            for k, v in n.items():
                if k != "layout":
                    walk(v, str(k))
    walk(node, None)
    return out


def _refs(data: Any, texts: list[str]) -> set[tuple[DepKind, str]]:
    out: set[tuple[DepKind, str]] = set()
    for key, s in [*_strings(data), *((None, t) for t in texts)]:
        pattern = EXPR_REF if key == "if" else workspaces.REF
        for m in pattern.finditer(s):
            out.add(("var" if m.group(1) == "vars" else "root", m.group(2)))
    return out


def _agent_deps(a: dict[str, Any]) -> set[tuple[DepKind, str]]:
    out: set[tuple[DepKind, str]] = set()
    for c in [*(a.get("connections") or []), *(a.get("write_connections") or []),
              (a.get("action") or {}).get("connector")]:
        if isinstance(c, str) and c and "{" not in c:
            out.add(("connector", c))
    origin = a.get("origin") or {}
    if isinstance(origin, dict) and origin.get("agent"):
        out.add(("agent", str(origin["agent"])))
    sub = a.get("subflow") or {}
    if isinstance(sub, dict) and sub.get("flow"):
        out.add(("flow", str(sub["flow"])))
    return out


def _flow_deps(raw: dict[str, Any], prompts: dict[str, str]) -> set[tuple[DepKind, str]]:
    out = _refs(raw, list(prompts.values()))
    root = (raw.get("workspace") or {}).get("root") if isinstance(raw.get("workspace"), dict) \
        else None
    if root:
        out.add(("root", str(root)))
    for a in (raw.get("agents") or {}).values():
        if isinstance(a, dict):
            out |= _agent_deps(a)
    for i in (raw.get("inputs") or {}).values():
        src = (i or {}).get("source") if isinstance(i, dict) else None
        if isinstance(src, dict) and src.get("flow") and src["flow"] != raw.get("name"):
            out.add(("flow", str(src["flow"])))
    return out


def _item_deps(src: workspaces.Workspace, item: Item,
               studio_flows: Path | None) -> set[tuple[DepKind, str]]:
    if item.kind == "flow":
        d = _flow_folder(src, item.name, studio_flows)
        if d is None:
            raise _missing(repr(item.name), src)
        deps = _flow_deps(*_read_flow(d))
        deps.discard(("flow", item.name))
        return deps
    if item.kind == "agent":
        a = _agents_of(src).get(item.name)
        if a is None:
            raise _missing(repr(item.name), src)
        raw = yaml.safe_load((a.folder / "agent.yaml").read_text(encoding="utf-8")) or {}
        return _refs(raw, [a.prompt]) | _agent_deps(raw)
    if item.kind == "flow_agent":
        data, prompt = _flow_agent(src, item, studio_flows)
        return _refs(data, [prompt]) | _agent_deps(data)
    return set()  # connectors and variables use nothing else


def _has(ws: workspaces.Workspace, kind: DepKind, name: str, studio_flows: Path | None) -> bool:
    if kind == "var":
        return name in ws.known_vars()
    if kind == "root":
        return bool(ws.spec and name in ws.spec.roots)
    if kind == "connector":
        try:
            return name in _conns_of(ws)
        except store.ConnectorConfigError:
            return False
    if kind == "agent":
        return name in _agents_of(ws)
    return _flow_folder(ws, name, studio_flows) is not None


def _exists(ws: workspaces.Workspace, item: Item, name: str, studio_flows: Path | None) -> bool:
    """Whether the target already has an item with this name (a name clash)."""
    if item.kind == "flow":
        return (_flow_target_dir(ws, studio_flows) / name).exists()
    if item.kind == "agent" or (item.kind == "flow_agent" and not item.to_flow):
        return name in _agents_of(ws)
    if item.kind == "flow_agent":
        d = _flow_folder(ws, item.to_flow or "", studio_flows)
        return bool(d and name in (_read_flow(d)[0].get("agents") or {}))
    if item.kind == "connector":
        return name in _conns_of(ws)
    return False


def _unique(taken: Callable[[str], bool] | set[str], base: str) -> str:
    """A free name: <base>_2, <base>_3..."""
    used = taken if callable(taken) else taken.__contains__
    n = 2
    while used(f"{base}_{n}"):
        n += 1
    return f"{base}_{n}"


def _main_name(item: Item) -> str:
    return (item.agent or "") if item.kind == "flow_agent" else item.name


def copy_plan(src: workspaces.Workspace, to: workspaces.Workspace, item: Item,
              new_name: str | None = None, studio_flows: Path | None = None) -> dict[str, Any]:
    """What a copy would do: the target name (and whether it clashes, with a free name to
    suggest) and each dependency with where it is and the default choice."""
    if item.kind == "vars":
        return _vars_plan(src, to, item)
    name = new_name or _main_name(item)
    deps = []
    for kind, dep in sorted(_item_deps(src, item, studio_flows)):
        in_source = _has(src, kind, dep, studio_flows)
        in_target = _has(to, kind, dep, studio_flows)
        copyable = in_source and (kind not in ("var", "root") or not to.studio)
        default: Choice = "target" if in_target else ("copy" if copyable else "leave")
        deps.append({"kind": kind, "name": dep, "in_source": in_source, "in_target": in_target,
                     "copyable": copyable, "choice": default})
    clash = _exists(to, item, name, studio_flows)
    out: dict[str, Any] = {"kind": item.kind, "name": name, "clash": clash, "deps": deps}
    if clash:
        out["suggestion"] = _unique(lambda n: _exists(to, item, n, studio_flows), name)
    if item.kind == "connector":
        conn = _conns_of(src).get(item.name)
        if conn is None:
            raise _missing(repr(item.name), src)
        out["secrets"] = sorted(f for f, ref in conn.secrets.items() if ref == "keyring")
    return out


def _vars_plan(src: workspaces.Workspace, to: workspaces.Workspace, item: Item) -> dict[str, Any]:
    if src.spec is None or to.spec is None:
        raise CopyError(msg("copy.vars_studio", "the studio's own workspace has no variables"))
    missing = [v for v in item.vars if v not in src.spec.vars
               and not any(v in e.vars for e in src.spec.environments.values())]
    missing += [e for e in item.environments if e not in src.spec.environments]
    if missing:
        raise _missing(", ".join(missing), src)
    return {"kind": "vars", "deps": [],
            "clash_vars": [v for v in item.vars if v in to.known_vars() or v in to.spec.vars],
            "clash_environments": [e for e in item.environments if e in to.spec.environments]}


# --- copying ------------------------------------------------------------------------------------
@dataclass
class Result:
    copied: list[dict[str, str]] = field(default_factory=list)  # {kind, name, as}
    secrets: list[str] = field(default_factory=list)  # connector/field copied in the keyring
    missing_secrets: list[str] = field(default_factory=list)  # connector/field to set by hand

    def add(self, kind: str, name: str, as_: str) -> None:
        self.copied.append({"kind": kind, "name": name, "as": as_})

    def to_json(self) -> dict[str, Any]:
        return {"copied": self.copied, "secrets": self.secrets,
                "missing_secrets": self.missing_secrets}


def _trash(path: Path) -> None:
    """A replaced flow or agent: moved to .trash/ next to it (recoverable)."""
    trash = path.parent / ".trash"
    trash.mkdir(exist_ok=True)
    shutil.move(str(path), trash / f"{path.name}-{time.strftime('%Y%m%d-%H%M%S')}")


def copy(src: workspaces.Workspace, to: workspaces.Workspace, item: Item, *,
         new_name: str | None = None, replace: bool = False,
         deps: dict[str, Choice] | None = None, same_secrets: bool = False,
         studio_flows: Path | None = None) -> Result:
    """Copies an item (and the dependencies chosen "copy") from `src` to `to` (may be the same
    workspace: a duplicate with another name). `deps` = {"<kind>:<name>": choice}."""
    res = Result()
    if item.kind == "vars":
        _vars_plan(src, to, item)  # they exist in the source
        _copy_vars(src, to, item.vars, item.environments, replace, res)
        return res
    plan = copy_plan(src, to, item, new_name, studio_flows)
    name = plan["name"]
    if src.name == to.name and name == _main_name(item) and item.kind != "flow_agent":
        raise CopyError(msg("copy.same_name", "a copy in the same workspace needs another name"))
    if plan["clash"] and not replace:
        raise _clash(name, to, plan["suggestion"])
    pattern = NAME if item.kind == "flow" else SLUG
    if not pattern.fullmatch(name):
        raise CopyError(msg("copy.bad_name", "invalid name {name}", name=repr(name)))
    chosen = {f"{d['kind']}:{d['name']}": d["choice"] for d in plan["deps"]}
    chosen.update(deps or {})
    # the dependencies first (a flow then opens with what it needs)
    for d in plan["deps"]:
        key = f"{d['kind']}:{d['name']}"
        if chosen.get(key) != "copy" or not d["copyable"]:
            continue  # "target" (use the target's one) or "leave"
        if d["kind"] in ("var", "root"):
            _copy_vars(src, to, [d["name"]] if d["kind"] == "var" else [], [], False, res,
                       roots=[d["name"]] if d["kind"] == "root" else [])
        elif d["kind"] == "connector":
            _copy_connector(src, to, d["name"], d["name"], d["in_target"], same_secrets, res)
        elif d["kind"] == "agent":
            _copy_agent(src, to, d["name"], d["name"], d["in_target"], res)
        elif d["kind"] == "flow" and not d["in_target"]:
            _copy_flow(src, to, d["name"], d["name"], False, studio_flows, res)
    if item.kind == "flow":
        _copy_flow(src, to, item.name, name, plan["clash"], studio_flows, res)
    elif item.kind == "agent":
        _copy_agent(src, to, item.name, name, plan["clash"], res)
    elif item.kind == "connector":
        _copy_connector(src, to, item.name, name, plan["clash"], same_secrets, res)
    elif item.kind == "flow_agent":
        _copy_flow_agent(src, to, item, name, plan["clash"], studio_flows, res)
    return res


# --- move: copy, then take the original out of the source ---------------------------------------
def _own_location(ws: workspaces.Workspace) -> str:
    return "local" if ws.studio else "workspace"


def _check_movable(src: workspaces.Workspace, item: Item, studio_flows: Path | None) -> None:
    """Before anything is copied: the original can leave the source."""
    if item.kind == "agent":
        a = _agents_of(src).get(item.name)
        if a is None:
            raise _missing(repr(item.name), src)
        if a.source != _own_location(src):
            raise CopyError(msg("copy.move_shared", "{name} belongs to {where}, not to this "
                                "workspace: copy it instead", name=repr(item.name),
                                where=a.source))
    elif item.kind == "connector":
        conns = _conns_of(src)
        if item.name not in conns:
            raise _missing(repr(item.name), src)
        with workspaces.using(src):
            where = store.location(item.name)
        if where != _own_location(src):
            raise CopyError(msg("copy.move_shared", "{name} belongs to {where}, not to this "
                                "workspace: copy it instead", name=repr(item.name), where=where))
    elif item.kind == "flow_agent":
        d = _flow_folder(src, item.name, studio_flows)
        if d is None:
            raise _missing(repr(item.name), src)
        raw, _ = _read_flow(d)
        if raw.get("start") == item.agent:
            raise CopyError(msg("copy.move_start", "{agent} is the start of the flow {flow}: "
                                "choose another start first, or copy it",
                                agent=repr(item.agent), flow=repr(item.name)))
    elif item.kind == "vars" and src.spec is not None:
        left = set(src.spec.environments) - set(item.environments)
        if item.environments and not left:
            raise CopyError(msg("copy.move_last_env", "a workspace keeps at least one "
                                "environment: copy the last one instead"))


def move(src: workspaces.Workspace, to: workspaces.Workspace, item: Item, *,
         new_name: str | None = None, replace: bool = False,
         deps: dict[str, Choice] | None = None, same_secrets: bool = False,
         studio_flows: Path | None = None) -> Result:
    """Copies the item (as `copy`, with the dependency check), then takes the original out of
    the source: a flow or library agent goes to `.trash`, a connector leaves the source file (its
    keyring secret is copied first, then removed there), variables/environments leave the
    source workspace.yaml, an agent leaves its flow (with its links). The dependencies stay in
    the source. Nothing leaves the source when the copy fails."""
    if src.name == to.name:
        raise CopyError(msg("copy.move_same", "moving needs another workspace (to duplicate in "
                            "this one, copy it)"))
    _check_movable(src, item, studio_flows)
    res = copy(src, to, item, new_name=new_name, replace=replace, deps=deps,
               same_secrets=same_secrets or item.kind == "connector", studio_flows=studio_flows)
    if item.kind == "flow":
        d = _flow_folder(src, item.name, studio_flows)
        if d is not None:
            _trash(d)
    elif item.kind == "agent":
        a = _agents_of(src).get(item.name)
        if a is not None:
            _trash(a.folder)
    elif item.kind == "connector":
        with workspaces.using(src):
            conns = store.load()
            conn = conns.pop(item.name, None)
            if conn is not None:
                store.save(conns)
                store.delete_secrets(item.name, conn)
    elif item.kind == "vars":
        _remove_vars(src, item.vars, item.environments)
    elif item.kind == "flow_agent":
        _remove_flow_agent(src, item, studio_flows)
    return res


def _remove_vars(ws: workspaces.Workspace, names: list[str], envs: list[str]) -> None:
    assert ws.spec is not None
    data = ws.spec.model_dump(mode="json")
    for v in names:
        data["vars"].pop(v, None)
        for e in data["environments"].values():
            (e.get("vars") or {}).pop(v, None)
    for e in envs:
        data["environments"].pop(e, None)
        if data.get("default_environment") == e:
            data["default_environment"] = None
    ws.spec = workspaces.WorkspaceSpec.model_validate(data)
    workspaces.save(ws)


def _remove_flow_agent(ws: workspaces.Workspace, item: Item, studio_flows: Path | None) -> None:
    """The agent leaves its flow: its node, the links to and from it, the inputs that took its
    outputs (they become free), its place on the canvas; its prompt file goes to .trash."""
    d = _flow_folder(ws, item.name, studio_flows)
    if d is None:
        return
    raw, _ = _read_flow(d)
    node = item.agent or ""
    a = (raw.get("agents") or {}).pop(node, None)
    if a is None:
        return
    raw["edges"] = [e for e in raw.get("edges") or []
                    if isinstance(e, dict) and node not in (e.get("from"), e.get("to"))]
    for other in (raw.get("agents") or {}).values():
        for i in (other.get("inputs") or {}).values() if isinstance(other, dict) else []:
            if isinstance(i, dict) and str(i.get("from") or "").startswith(f"{node}."):
                i.pop("from", None)
    if isinstance(raw.get("layout"), dict):
        raw["layout"].pop(node, None)
    pf = a.get("prompt_file") if isinstance(a, dict) else None
    users = [x for x in (raw.get("agents") or {}).values()
             if isinstance(x, dict) and x.get("prompt_file") == pf]
    if pf and not users and (d / pf).is_file():
        _trash(d / pf)
    (d / "flow.yaml").write_text(dump_raw(raw, str(raw.get("name") or d.name)), encoding="utf-8")


def _copy_flow(src: workspaces.Workspace, to: workspaces.Workspace, name: str, new: str,
               replace: bool, studio_flows: Path | None, res: Result) -> None:
    d = _flow_folder(src, name, studio_flows)
    assert d is not None
    dest = _flow_target_dir(to, studio_flows) / new
    if dest.exists():
        if not replace:
            return
        _trash(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(d, dest, ignore=IGNORE)
    if new != name:
        raw, _ = _read_flow(dest)
        raw["name"] = new
        (dest / "flow.yaml").write_text(dump_raw(raw, new), encoding="utf-8")
    res.add("flow", name, new)


def _copy_agent(src: workspaces.Workspace, to: workspaces.Workspace, agent_id: str, new: str,
                replace: bool, res: Result) -> None:
    a = _agents_of(src)[agent_id]
    with workspaces.using(to):
        existing = library.list_agents().get(new)
        location = "local" if to.studio else "workspace"
        if existing is not None:
            if not replace or existing.source != location:
                return  # the target's own (or a built-in) agent stays
            _trash(existing.folder)
        dest = library.save_dir(location) / new
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(a.folder, dest, ignore=IGNORE)
    res.add("agent", agent_id, new)


def _copy_connector(src: workspaces.Workspace, to: workspaces.Workspace, name: str, new: str,
                    replace: bool, same_secrets: bool, res: Result) -> None:
    conn = _conns_of(src)[name]
    with workspaces.using(to):
        conns = store.load()
        if new in conns and not replace:
            return
        if new in conns and store.location(new) != ("local" if to.studio else "workspace"):
            return  # an extension's or the public connector: not replaced from here
        # the definition only; `${VAR}` references are names, keyring secrets stay behind
        conns[new] = conn.model_copy(deep=True)
        store.save(conns, moves={new: "local" if to.studio else "workspace"})
    res.add("connector", name, new)
    for f, ref in conn.secrets.items():
        if ref != "keyring":
            continue
        if not same_secrets:
            res.missing_secrets.append(f"{new}/{f}")
            continue
        with workspaces.using(src):
            value = store.get_secret(name, conn, f)
        if value is None:
            res.missing_secrets.append(f"{new}/{f}")
            continue
        with workspaces.using(to):
            store.set_secret(new, f, value)  # on this machine only; never in a file
        res.secrets.append(f"{new}/{f}")
        del value


def _flow_agent(src: workspaces.Workspace, item: Item,
                studio_flows: Path | None) -> tuple[dict[str, Any], str]:
    d = _flow_folder(src, item.name, studio_flows)
    if d is None:
        raise _missing(repr(item.name), src)
    raw, prompts = _read_flow(d)
    a = (raw.get("agents") or {}).get(item.agent or "")
    if not isinstance(a, dict):
        raise _missing(repr(f"{item.name}/{item.agent}"), src)
    prompt = str(a.get("prompt") or prompts.get(str(a.get("prompt_file") or ""), ""))
    return dict(a), prompt


def _copy_flow_agent(src: workspaces.Workspace, to: workspaces.Workspace, item: Item, new: str,
                     replace: bool, studio_flows: Path | None, res: Result) -> None:
    data, prompt = _flow_agent(src, item, studio_flows)
    if not item.to_flow:  # into the target's agent library
        with workspaces.using(to):
            raw = {k: v for k, v in data.items() if k not in ("prompt", "prompt_file", "origin")}
            location = "local" if to.studio else "workspace"
            existing = library.list_agents().get(new)
            if existing is not None and replace and existing.source == location:
                _trash(existing.folder)
            library.save_local(new, str(data.get("title") or new), raw, prompt,
                               location=location)
        res.add("agent", f"{item.name}/{item.agent}", new)
        return
    d = _flow_folder(to, item.to_flow, studio_flows)
    if d is None:
        raise _missing(repr(item.to_flow), to)
    raw, _ = _read_flow(d)
    agents = raw.setdefault("agents", {})
    if new in agents and not replace:
        raise _clash(new, to, _unique(set(agents), new))
    # its links to the other agents of its flow do not exist here: its inputs become free
    inputs = data.get("inputs") or {}
    if isinstance(inputs, dict):
        data["inputs"] = {n: {k: v for k, v in (i or {}).items() if k != "from"}
                          for n, i in inputs.items()}
    if data.get("prompt_file"):
        rel = f"prompts/{new}.md"
        (d / "prompts").mkdir(exist_ok=True)
        (d / rel).write_text(prompt, encoding="utf-8")
        data["prompt_file"] = rel
    agents[new] = data
    (d / "flow.yaml").write_text(dump_raw(raw, str(raw.get("name") or d.name)), encoding="utf-8")
    res.add("flow_agent", f"{item.name}/{item.agent}", f"{item.to_flow}/{new}")


def _copy_vars(src: workspaces.Workspace, to: workspaces.Workspace, names: list[str],
               envs: list[str], replace: bool, res: Result, roots: list[str] | None = None) -> None:
    """Variables (base value and the value of each environment both workspaces have), whole
    environments, and roots (as absolute folders when the two workspaces live apart)."""
    if src.spec is None or to.spec is None:
        raise CopyError(msg("copy.vars_studio", "the studio's own workspace has no variables"))
    data = to.spec.model_dump(mode="json")
    s = src.spec
    for e in envs:
        mine = s.environments[e].model_dump(mode="json")
        if e in data["environments"] and not replace:
            # the target's environment stays; it gets the variables it does not have yet
            have = data["environments"][e].setdefault("vars", {})
            for k, v in mine.get("vars", {}).items():
                have.setdefault(k, v)
        else:
            data["environments"][e] = mine
        res.add("environment", e, e)
    for v in names:
        if (v in data["vars"] or v in to.known_vars()) and not replace:
            continue
        if v in s.vars:
            data["vars"][v] = s.vars[v]
        for en, e in s.environments.items():
            if v in e.vars and en in data["environments"]:
                data["environments"][en].setdefault("vars", {})[v] = e.vars[v]
        if v not in s.vars:  # only per environment there: the default one's value is the base
            data["vars"].setdefault(v, src.resolve(None)[0].get(v, ""))
        res.add("var", v, v)
    for r in roots or []:
        if r in data["roots"] and not replace:
            continue
        root = s.roots[r].model_dump(mode="json")
        absolute = (src.home / root["path"]).expanduser().resolve()
        root["path"] = absolute.as_posix() if src.home != to.home else root["path"]
        data["roots"][r] = root
        res.add("root", r, r)
    to.spec = workspaces.WorkspaceSpec.model_validate(data)
    workspaces.save(to)


# --- whole workspaces ---------------------------------------------------------------------------
def duplicate(src: workspaces.Workspace, name: str, where: Literal["repo", "local"],
              repo: Path | None = None, title: str = "", same_secrets: bool = False) -> tuple[
                  workspaces.Workspace, Result]:
    """A new workspace with the flows, agents, connectors (definitions), variables, environments
    and roots of `src` (no runs). Roots that were relative to the old home point to the same
    folders (absolute) when the new one lives elsewhere."""
    if src.studio or src.spec is None or src.folder is None:
        raise CopyError(msg("copy.dup_studio", "the studio's own workspace cannot be "
                            "duplicated (copy its flows into a new workspace instead)"))
    new = workspaces.create(name, where, repo, title=title or name,
                            description=src.spec.description)
    assert new.folder is not None
    res = Result()
    for part in ("flows", "agents"):
        if (src.folder / part).is_dir():
            shutil.copytree(src.folder / part, new.folder / part, ignore=IGNORE,
                            dirs_exist_ok=True)
    if src.connections_file.is_file():
        shutil.copy2(src.connections_file, new.connections_file)
    data = src.spec.model_dump(mode="json")
    data |= {"name": name, "title": title or name}
    if src.home != new.home:
        for r in data["roots"].values():
            r["path"] = (src.home / r["path"]).expanduser().resolve().as_posix()
    if where == "repo" and "app" not in data["roots"]:
        data["roots"]["app"] = {"path": ".", "description": "This repository"}
    new.spec = workspaces.WorkspaceSpec.model_validate(data)
    workspaces.save(new)
    res.add("workspace", src.name, name)
    # secrets of the workspace's own connectors: copied in the keyring only if asked
    try:
        conns = _conns_of(new)
    except store.ConnectorConfigError:
        conns = {}
    with workspaces.using(new):
        own = {n: c for n, c in conns.items() if store.location(n) == "workspace"}
    for n, c in own.items():
        for f, ref in c.secrets.items():
            if ref != "keyring":
                continue
            value = None
            if same_secrets:
                with workspaces.using(src):
                    value = store.get_secret(n, c, f)
            if value is None:
                res.missing_secrets.append(f"{n}/{f}")
                continue
            with workspaces.using(new):
                store.set_secret(n, f, value)
            res.secrets.append(f"{n}/{f}")
    return new, res

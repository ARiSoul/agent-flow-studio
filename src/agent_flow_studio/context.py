"""The context of a studio workspace: what each of its folders/repositories is (structure,
technologies, how to build and test, conventions, key files), shared by every agent of its flows.

Files in the workspace folder (a repository workspace commits them with the repository):
- `context.md`: one section per root (written by the context keeper, or corrected by the person)
  and a `notes` section the person writes. Sections are marked with HTML comments, so a section
  can hold any Markdown.
- `context.json`: per root, the git commit and the time of the last explore/update, and the run.

The studio writes these files; a model never does: the context keeper (`keeper_flow`, read-only
tools on one root) returns its section as a typed output and the runner stores it.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any

from . import workspaces
from .messages import MsgError, msg

CONTEXT_FILE = "context.md"
STATE_FILE = "context.json"
NOTES = "notes"
MAX_SECTION = 20_000
MAX_PROMPT = 12_000  # what each agent gets (the rest stays in the file)
KEEPER_FLOW = "workspace_context"
KEEPER_AGENT = "context_keeper"
_BEGIN = re.compile(r"^<!-- afs:begin ([a-z][a-z0-9_]{0,39}) -->$", re.MULTILINE)
_HEADING = re.compile(r"^(#{1,2})(\s)", re.MULTILINE)


def _folder(ws: workspaces.Workspace) -> Path:
    if ws.folder is None:
        raise MsgError(msg("ctx.studio", "the studio's own workspace has no context (open or "
                           "create a workspace)"))
    return ws.folder


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8", newline="\n")
    os.replace(tmp, path)


# --- reading and writing -----------------------------------------------------------------------
def sections(ws: workspaces.Workspace) -> dict[str, str]:
    """{root or "notes": Markdown body} from context.md (empty when there is none)."""
    f = _folder(ws) / CONTEXT_FILE
    if not f.is_file():
        return {}
    text = f.read_text(encoding="utf-8")
    out: dict[str, str] = {}
    for m in _BEGIN.finditer(text):
        name = m.group(1)
        end = text.find(f"<!-- afs:end {name} -->", m.end())
        if end < 0:
            continue
        body = text[m.end():end].strip("\n")
        # the visible heading the studio writes is not part of the body
        lines = body.split("\n")
        if lines and lines[0].startswith("## "):
            lines = lines[1:]
        out[name] = "\n".join(lines).strip()
    return out


def state(ws: workspaces.Workspace) -> dict[str, dict[str, Any]]:
    f = _folder(ws) / STATE_FILE
    try:
        data = json.loads(f.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    roots = data.get("roots") if isinstance(data, dict) else None
    return roots if isinstance(roots, dict) else {}


def _check(body: str) -> str:
    from .connections.model import _SECRET_LIKE
    body = body.strip()
    if len(body) > MAX_SECTION:
        raise MsgError(msg("ctx.too_long", "a context section has at most {n} characters",
                           n=MAX_SECTION))
    if _SECRET_LIKE.search(body):
        raise MsgError(msg("ctx.secret", "the context looks like it has a secret; secrets never "
                           "go in the workspace context"))
    # a section's own headings start at ###: ## is the section itself
    return _HEADING.sub(lambda m: "###" + m.group(2), body)


def _render(ws: workspaces.Workspace, secs: dict[str, str]) -> str:
    assert ws.spec is not None
    roots = ws.resolve(None)[1]
    out = [f"# Workspace context — {ws.spec.title or ws.name}", "",
           ("<!-- Written by Agent Flow Studio. The agents of this workspace's flows read it as "
           "background. One section per root (folder or repository), kept by the context keeper; "
           "the notes section is yours. -->"), ""]
    order = [NOTES, *[r for r in ws.spec.roots if r in secs],
             *sorted(k for k in secs if k != NOTES and k not in ws.spec.roots)]
    for name in order:
        if name not in secs:
            continue
        title = "Notes" if name == NOTES else f"{name} — `{roots.get(name, '(not a root any more)')}`"
        out += [f"<!-- afs:begin {name} -->", f"## {title}", "", secs[name].strip(), "",
                f"<!-- afs:end {name} -->", ""]
    return "\n".join(out)


def set_section(ws: workspaces.Workspace, name: str, body: str, *, commit: str | None = None,
                by: str | None = None) -> None:
    """Writes one section (a root's, or the notes) and, for a root, its state."""
    if name != NOTES and (ws.spec is None or name not in ws.spec.roots):
        raise MsgError(msg("ctx.unknown_root", "{root} is not a root of the workspace {workspace}",
                           root=repr(name), workspace=ws.name))
    secs = sections(ws)
    clean = _check(body)
    if clean:
        secs[name] = clean
    else:
        secs.pop(name, None)
    folder = _folder(ws)
    _write(folder / CONTEXT_FILE, _render(ws, secs))
    if name != NOTES:
        st = state(ws)
        st[name] = {"commit": commit, "updated": time.time(), "by": by}
        _write(folder / STATE_FILE, json.dumps({"version": 1, "roots": st}, indent=1) + "\n")


def follow_renames(ws: workspaces.Workspace, before: dict[str, str]) -> dict[str, str]:
    """After the roots changed: a root that only changed its name (same folder, before → now)
    keeps its explored section and state under the new name. Returns {old: new}."""
    if ws.spec is None or ws.folder is None:
        return {}
    now = {k: r.path for k, r in ws.spec.roots.items()}
    gone = {k: p for k, p in before.items() if k not in now}
    new = {k: p for k, p in now.items() if k not in before}
    renames = {}
    for old, path in gone.items():
        same = [k for k, p in new.items() if p == path]
        if len(same) == 1 and same[0] not in renames.values():
            renames[old] = same[0]
    if not renames:
        return {}
    secs, st = sections(ws), state(ws)
    moved = False
    for old, to in renames.items():
        if old in secs and to not in secs:
            secs[to] = secs.pop(old)
            moved = True
        if old in st and to not in st:
            st[to] = st.pop(old)
            moved = True
    if moved:
        _write(ws.folder / CONTEXT_FILE, _render(ws, secs))
        _write(ws.folder / STATE_FILE, json.dumps({"version": 1, "roots": st}, indent=1) + "\n")
    return renames


def context_text(ws: workspaces.Workspace, limit: int = MAX_PROMPT) -> str:
    """The context as the agents get it (current roots and the notes), cut at `limit`."""
    if ws.studio or ws.spec is None:
        return ""
    secs = sections(ws)
    roots = ws.resolve(None)[1]
    parts = []
    if secs.get(NOTES):
        parts.append(f"### Notes\n{secs[NOTES]}")
    for r in ws.spec.roots:
        if secs.get(r):
            parts.append(f"### Root `{r}` — {roots.get(r, '')}\n{secs[r]}")
    text = "\n\n".join(parts)
    if len(text) > limit:
        text = (text[:limit].rsplit("\n", 1)[0]
                + f"\n\n(cut here: the whole context is in {_folder(ws) / CONTEXT_FILE})")
    return text


# --- git state of the roots --------------------------------------------------------------------
def _git(root: Path, *args: str) -> str | None:
    exe = shutil.which("git")
    if not exe or not (root / ".git").exists():
        return None
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env |= {"GIT_TERMINAL_PROMPT": "0", "GIT_OPTIONAL_LOCKS": "0"}
    try:
        r = subprocess.run([exe, "-c", "core.fsmonitor=false", *args], cwd=root, env=env,
                           capture_output=True, text=True, timeout=10, check=False,
                           stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError):
        return None
    return r.stdout.strip() if r.returncode == 0 else None


def head(root: Path) -> str | None:
    """The HEAD commit of a repository root (None: not a git repository)."""
    return _git(root, "rev-parse", "HEAD")


def roots_state(ws: workspaces.Workspace) -> dict[str, dict[str, Any]]:
    """Per root: explored or not, when, and how many commits came since (git roots)."""
    if ws.spec is None:
        return {}
    st, secs, roots = state(ws), sections(ws), ws.resolve(None)[1]
    out = {}
    for r in ws.spec.roots:
        s = st.get(r) or {}
        path = Path(roots[r])
        since = None
        if s.get("commit") and path.is_dir():
            n = _git(path, "rev-list", "--count", f"{s['commit']}..HEAD")
            since = int(n) if n and n.isdigit() else None
        out[r] = {"path": str(path), "exists": path.is_dir(), "git": (path / ".git").exists(),
                  "explored": bool(secs.get(r)), "updated": s.get("updated"),
                  "commit": s.get("commit"), "commits_since": since}
    return out


# --- the context keeper (a system flow, never on disk) -----------------------------------------
def keeper_prompt() -> str:
    return (Path(__file__).parent / "context_keeper.md").read_text(encoding="utf-8")


def keeper_flow(root: str) -> dict[str, Any]:
    """The flow that explores or updates the context of ONE root (read-only tools on it). Its
    `section` output is stored by the studio; it has no way to write anything."""
    def txt(kind: str, label: str) -> dict[str, Any]:
        return {"type": kind, "label": label, "required": False}
    return {
        "version": 1, "name": KEEPER_FLOW, "title": "Workspace context",
        "description": "Explores a root of the workspace, or updates what the context says "
                       "about it.",
        "execution": "graph", "task": f"Keep the context of the workspace root `{root}`.",
        "start": KEEPER_AGENT, "workspace": {"root": root}, "use_context": False,
        "context_update": "never",
        "inputs": {"mode": {"type": "choice", "options": ["explore", "update"]},
                   "root": {"type": "text"},
                   "current": txt("markdown", "What the context says now"),
                   "since": txt("text", "Commit of the last update"),
                   "changes": txt("markdown", "What the last run did")},
        "agents": {KEEPER_AGENT: {
            "description": "Explores a folder or repository (read only) and writes what the "
                           "other agents need to know about it.",
            "prompt": keeper_prompt(),
            "tools": ["workspace.read", "workspace.glob", "workspace.grep", "workspace.git"],
            "max_turns": 60,
            "inputs": {k: {"from": f"input.{k}", **({} if k in ("mode", "root")
                                                     else {"required": False})}
                       for k in ("mode", "root", "current", "since", "changes")},
            "outputs": {"section": {"type": "markdown", "label": "Context of the root"},
                        "summary": {"type": "text", "label": "What changed in the context"}},
        }},
    }


def run_changes(events: list[dict[str, Any]], outputs: dict[str, dict[str, Any]],
                limit: int = 6000) -> str:
    """What a run did, for the update: the flow, its stop message, the outputs (cut)."""
    start = next((e for e in events if e.get("type") == "start"), {})
    end = next((e for e in reversed(events) if e.get("type") == "end"), {})
    lines = [(f"Flow `{start.get('flow')}`, run `{start.get('input')}`, "
             f"ended {end.get('status', '?')}.")]
    gate = (end.get("gate") or {}) if isinstance(end.get("gate"), dict) else {}
    if gate.get("message"):
        lines.append(f"Result: {gate['message']}")
    for agent, values in outputs.items():
        for k, v in values.items():
            text = v if isinstance(v, str) else json.dumps(v, ensure_ascii=False, default=str)
            lines.append(f"- {agent}.{k}: {text[:800]}")
    out = "\n".join(lines)
    return out[:limit]

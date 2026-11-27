"""Read-only git in the flow workspace (`workspace.git`): status, diff, log, show, branch, ls-files,
blame.

Not a shell: fixed subcommands, options from a closed list, no `-c`/`--output`/external diff or
textconv tools, fsmonitor and hooks off, no pager, no prompts. The compiler sets the workspace
(`configure`); without one the tool refuses. Writing with git (commit, push) stays in
`workspace.bash`, which follows the flow autonomy.

Which repository: the workspace folder when it is one; otherwise the repositories one level down
in it and the roots of the studio workspace (e.g. apps, src…) that are repositories. With one,
it is used; with several, `repo` chooses (a root name or a sub-folder of the workspace); the
error lists them.
"""
from __future__ import annotations

import asyncio
import os
import re
import shutil
from pathlib import Path
from typing import Any

from claude_agent_sdk import tool

from .common import err, ok

MAX_OUTPUT = 100_000
TIMEOUT = 30.0
COMMANDS = ("status", "diff", "log", "show", "branch", "ls-files", "blame")

# options per command: exact flags, or "prefix=" for flags with a value (--max-count=20)
_COMMON = {"--stat", "--name-only", "--name-status", "--oneline", "--patch", "-p", "--no-color"}
_OPTIONS: dict[str, set[str]] = {
    "status": {"--short", "-s", "--branch", "-b", "--porcelain", "--untracked-files=no"},
    "diff": _COMMON | {"--cached", "--staged", "--merge-base", "-U", "--unified=", "--stat=",
                       "--diff-filter=", "-w", "--ignore-all-space", "--find-renames", "-M"},
    "log": _COMMON | {"-n", "--max-count=", "--since=", "--until=", "--author=", "--grep=",
                      "--format=", "--pretty=", "--first-parent", "--no-merges", "--merges",
                      "--graph", "--decorate", "--all", "--reverse", "--follow", "-U", "--unified="},
    "show": _COMMON | {"--format=", "--pretty=", "-U", "--unified=", "--no-patch", "-s"},
    "branch": {"--list", "-a", "--all", "-r", "--remotes", "-v", "-vv", "--merged", "--no-merged",
               "--contains", "--show-current", "--sort="},
    "ls-files": {"--cached", "-c", "--modified", "-m", "--others", "-o", "--exclude-standard",
                 "--deleted", "-d"},
    "blame": {"-L", "-w", "-M", "-C", "--date=", "--porcelain", "-s"},
}
# git runs code from config for these; keep them off (a repo's own .git/config is trusted no more
# than the rest of the repo)
_SAFE_PREFIX = ["-c", "core.fsmonitor=false", "-c", "core.pager=cat", "--no-pager"]
_NO_EXTERNAL = {"diff": ["--no-ext-diff", "--no-textconv"], "log": ["--no-ext-diff", "--no-textconv"],
                "show": ["--no-ext-diff", "--no-textconv"]}
_VALUE = re.compile(r"^[^\x00-\x1f]{1,300}$")  # one line, printable

_workspace: Path | None = None


def configure(workspace: Path | None) -> None:
    global _workspace
    _workspace = workspace


def check_args(command: str, args: list[str]) -> list[str]:
    """Returns the arguments when they are allowed; raises ValueError otherwise."""
    if command not in COMMANDS:
        raise ValueError(f"command {command!r} not allowed; use {', '.join(COMMANDS)}")
    if len(args) > 40:
        raise ValueError("too many arguments")
    allowed = _OPTIONS[command]
    after_dashdash = False
    for a in args:
        if not isinstance(a, str) or not _VALUE.fullmatch(a):
            raise ValueError("arguments must be short single-line strings")
        if after_dashdash:
            if a.startswith("/") or re.match(r"^[A-Za-z]:", a) or ".." in Path(a).parts:
                raise ValueError(f"path {a!r} must stay inside the workspace")
            continue
        if a == "--":
            after_dashdash = True
            continue
        if a.startswith("-"):
            flag = a.split("=", 1)[0] + "=" if "=" in a else a
            short_num = re.fullmatch(r"-(n|U|L)\S*", a) or re.fullmatch(r"-\d+", a)
            if not (a in allowed or flag in allowed
                    or (short_num and (a[:2] in allowed or command == "log"))):
                raise ValueError(f"option {a!r} not allowed for git {command}")
    return args


def _is_repo(p: Path) -> bool:
    return (p / ".git").exists()


def repos() -> dict[str, Path]:
    """The repositories the tool can read: name → folder."""
    if _workspace is None:
        return {}
    found: dict[str, Path] = {}
    if _is_repo(_workspace):
        found["."] = _workspace
    else:
        for sub in sorted(_workspace.iterdir()) if _workspace.is_dir() else []:
            if sub.is_dir() and not sub.name.startswith(".") and _is_repo(sub):
                found[sub.name] = sub
    from .. import workspaces  # the roots of the studio workspace (folders the person declared)
    for name, path in workspaces.resolved()[1].items():
        root = Path(path)
        if _is_repo(root) and root.resolve() not in {f.resolve() for f in found.values()}:
            found[name] = root
    return found


def pick_repo(repo: str | None) -> Path:
    """The repository to run git in (see the module note)."""
    if _workspace is None:
        raise ValueError("this flow has no workspace")
    found = repos()
    if repo:
        if repo in found:
            return found[repo]
        if ".." in Path(repo).parts or Path(repo).is_absolute():
            raise ValueError("repo must be a root name or a folder inside the workspace")
        p = (_workspace / repo).resolve()
        if p.is_relative_to(_workspace.resolve()) and _is_repo(p):
            return p
        raise ValueError(f"{repo!r} is not a git repository here; repos: {', '.join(found) or 'none'}")
    if "." in found:
        return found["."]
    if len(found) == 1:
        return next(iter(found.values()))
    if not found:
        raise ValueError("no git repository in the workspace or its roots (or one level down)")
    raise ValueError("several git repositories: pass repo, one of " + ", ".join(found))


async def run_git(command: str, args: list[str], repo: str | None = None) -> dict[str, Any]:
    cwd = pick_repo(repo)
    exe = shutil.which("git")
    if not exe:
        raise ValueError("git is not installed or not on PATH")
    argv = [*_SAFE_PREFIX, command, *_NO_EXTERNAL.get(command, []), *check_args(command, args)]
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env |= {"GIT_TERMINAL_PROMPT": "0", "GIT_PAGER": "cat", "GIT_OPTIONAL_LOCKS": "0"}
    proc = await asyncio.create_subprocess_exec(
        exe, *argv, cwd=str(cwd), env=env, stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    try:
        out, errb = await asyncio.wait_for(proc.communicate(), timeout=TIMEOUT)
    except asyncio.TimeoutError:  # TimeoutError on Python 3.11+
        proc.kill()
        raise ValueError(f"git {command} took longer than {TIMEOUT:.0f}s") from None
    text = out.decode("utf-8", "replace")
    return {"exit_code": proc.returncode, "truncated": len(text) > MAX_OUTPUT,
            "output": text[:MAX_OUTPUT],
            "error": errb.decode("utf-8", "replace").strip()[-2000:] if proc.returncode else ""}


@tool(
    "git",
    "Read-only git in the flow workspace. 'command' is one of: status, diff, log, show, branch, "
    "ls-files, blame. 'args' are options and revisions/paths, e.g. ['main...HEAD', '--stat'] or "
    "['-n', '20', '--oneline']. Put paths after '--'. It cannot commit, push or change files. "
    "When the workspace folder is not a repository, it uses the repositories one level down and "
    "the workspace roots: pass 'repo' (a root name or a sub-folder) when there are several; the "
    "error lists them.",
    {"type": "object", "properties": {
        "command": {"type": "string", "enum": list(COMMANDS)},
        "args": {"type": "array", "items": {"type": "string"}},
        "repo": {"type": "string"},
    }, "required": ["command"]},
)
async def git(args):
    try:
        r = await run_git(args["command"], list(args.get("args") or []), args.get("repo") or None)
    except ValueError as e:
        return err(str(e))
    return ok(r)

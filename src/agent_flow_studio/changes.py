"""What a run changed in the git roots of its workspace, without writing to them.

At the start of a run the studio takes a `snapshot` of each root: its HEAD, its branch, the files
already changed or untracked then, with a hash of their content (`git hash-object`, never `-w`:
nothing is written to the repository). `changes` later lists the files that differ from that
start: modified, added (new untracked files), deleted, with their diff against HEAD. A file that
was already changed before the run is shown only when the run changed it again (`before: true`:
its diff also holds the person's earlier edits). Reading only; git runs with the same safe
environment as the read-only git tool (no pager, no fsmonitor, no external diff).
"""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

MAX_FILES = 300  # files listed per root
MAX_DIFF = 200_000  # characters of diff per file
MAX_UNTRACKED = 2000  # untracked files remembered at the start
_SAFE = ["-c", "core.fsmonitor=false", "-c", "core.pager=cat", "--no-pager"]


def _git(root: Path, *args: str, ok: tuple[int, ...] = (0,), timeout: float = 20,
         stdin: str | None = None) -> str | None:
    exe = shutil.which("git")
    if not exe or not (root / ".git").exists():
        return None
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env |= {"GIT_TERMINAL_PROMPT": "0", "GIT_OPTIONAL_LOCKS": "0", "GIT_PAGER": "cat"}
    try:
        r = subprocess.run([exe, *_SAFE, *args], cwd=root, env=env, capture_output=True,
                           timeout=timeout, check=False,
                           input=stdin.encode("utf-8") if stdin is not None else None,
                           stdin=None if stdin is not None else subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError):
        return None
    if r.returncode not in ok:
        return None
    return r.stdout.decode("utf-8", "replace")


def _status(root: Path) -> dict[str, str]:
    """path → porcelain status code ("M", "A", "D", "??", "R"…) of the working tree."""
    out = _git(root, "status", "--porcelain=v1", "-z", "--untracked-files=all") or ""
    items, parts, i = {}, out.split("\0"), 0
    while i < len(parts):
        entry = parts[i]
        i += 1
        if len(entry) < 4:
            continue
        code, path = entry[:2], entry[3:]
        if code[0] in "RC":  # renamed/copied: the next item is the old path
            i += 1
        items[path] = "??" if code == "??" else (code.strip() or "M")[0]
    return items


def _hashes(root: Path, paths: list[str]) -> dict[str, str | None]:
    """The content hash of each file (one git call; None: not a file now)."""
    files = [p for p in paths if (root / p).is_file()]
    out = _git(root, "hash-object", "--stdin-paths", stdin="".join(f"{p}\n" for p in files)) \
        if files else ""
    got = dict(zip(files, (out or "").split(), strict=False))
    return {p: got.get(p) for p in paths}


def snapshot(root: Path) -> dict[str, Any] | None:
    """The state of a git root before a run (None: not a git repository)."""
    head = _git(root, "rev-parse", "HEAD")
    if head is None:
        return None
    branch = (_git(root, "rev-parse", "--abbrev-ref", "HEAD") or "").strip()
    dirty = _hashes(root, list(_status(root))[:MAX_UNTRACKED])
    return {"head": head.strip(), "branch": branch, "dirty": dirty}


def _numstat(root: Path, head: str) -> dict[str, tuple[int | None, int | None]]:
    out = _git(root, "diff", "--no-ext-diff", "--no-textconv", "--numstat", "-z", head) or ""
    stats: dict[str, tuple[int | None, int | None]] = {}
    for line in out.split("\0"):
        bits = line.split("\t")
        if len(bits) == 3 and bits[2]:
            a, d = bits[0], bits[1]
            stats[bits[2]] = (None, None) if a == "-" else (int(a), int(d))
    return stats


def _new_file_diff(root: Path, path: str) -> tuple[str, int | None, bool]:
    f = root / path
    try:
        data = f.read_bytes()
    except OSError:
        return "", None, False
    if b"\0" in data[:8000]:
        return "", None, True
    text = data.decode("utf-8", "replace")
    lines = text.splitlines()
    body = "".join(f"+{x}\n" for x in lines)
    return f"--- /dev/null\n+++ b/{path}\n@@ -0,0 +1,{len(lines)} @@\n{body}", len(lines), False


def changes(root: Path, snap: dict[str, Any]) -> list[dict[str, Any]]:
    """The files of a root that the run changed since its snapshot."""
    head = snap.get("head")
    if not head:
        return []
    before: dict[str, str | None] = snap.get("dirty") or {}
    stats = _numstat(root, head)
    now = _status(root)
    hashes = _hashes(root, [p for p in now if p in before])
    out: list[dict[str, Any]] = []
    for path, code in sorted(now.items()):
        if path in before and hashes.get(path) == before[path]:
            continue  # changed before the run and not touched since
        status = {"??": "added", "A": "added", "D": "deleted", "R": "renamed"}.get(code, "modified")
        item: dict[str, Any] = {"path": path, "status": status, "before": path in before}
        if code == "??":
            diff, added, binary = _new_file_diff(root, path)
            item |= {"added": added, "removed": 0, "binary": binary}
        else:
            a, d = stats.get(path, (None, None))
            diff = "" if a is None else _git(
                root, "diff", "--no-ext-diff", "--no-textconv", head, "--", path) or ""
            item |= {"added": a, "removed": d, "binary": a is None and status != "deleted"}
        item["diff"] = diff[:MAX_DIFF]
        item["cut"] = len(diff) > MAX_DIFF
        out.append(item)
        if len(out) >= MAX_FILES:
            break
    return out

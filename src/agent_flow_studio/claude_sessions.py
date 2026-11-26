"""Claude Code sessions and memory notes about a folder, as sources for the workspace context.

Claude Code keeps, per folder, its sessions (`<config>/projects/<folder key>/<id>.jsonl`) and its
memory notes (`<same folder>/memory/*.md`); `<config>` is `CLAUDE_CONFIG_DIR` or `~/.claude`.
They stay on each machine. The SDK lists and reads the sessions (`list_sessions`,
`get_session_messages`: the main chain, which after a compaction starts with its summary).

Only the conversation text is used: the person's messages and Claude's text replies. Tool calls
and results, thinking, images, subagent messages and injected reminders are left out, and
secret-like text is masked. Nothing here writes into a repository: the digests only go into the
inputs of a context run (machine-local `runs/`).

The studio's own sessions (its flow runs and assistants) are not sources: they are tagged
`agent-flow-studio` when they end (`mark_ours`), and older ones are known by their first prompt
or by the program that started them.
"""
from __future__ import annotations

import contextlib
import os
import re
from collections import OrderedDict
from pathlib import Path
from typing import Any

STUDIO_TAG = "agent-flow-studio"
STUDIO_PREFIXES = ("You are the agent `", "Run the flow for ", "[Flow open in the editor",
                   "[Workspace open in the Board")
MAX_DIGEST = 60_000
MAX_MEMORY = 60_000
_SANITIZED_MAX = 200
_INJECTED = re.compile(
    r"<(system-reminder|local-command-stdout|local-command-stderr|command-message|command-name"
    r"|command-args|task-notification|user-prompt-submit-hook)\b[^>]*>.*?</\1>", re.DOTALL)
_PASTED = re.compile(r"<pasted_content\b[^>]*>(.*?)</pasted_content>", re.DOTALL)
_ENTRYPOINT = re.compile(r'"entrypoint"\s*:\s*"([^"]{1,40})"')
_COMPACT = "This session is being continued from a previous conversation"
_cache: OrderedDict[tuple[str, float], str] = OrderedDict()


def config_dir() -> Path:
    return Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude")


SESSION_ID = re.compile(r"^[A-Za-z0-9-]{8,64}$")


def session_cwd(session_id: str) -> str | None:
    """The folder a Claude Code session ran in (its file's `cwd`), to resume it there; None when
    this machine has no such session."""
    if not SESSION_ID.fullmatch(session_id or ""):
        return None
    for f in (config_dir() / "projects").glob(f"*/{session_id}.jsonl"):
        try:
            with f.open(encoding="utf-8", errors="replace") as fh:
                for _ in range(50):
                    line = fh.readline()
                    if not line:
                        break
                    m = re.search(r'"cwd"\s*:\s*("(?:[^"\\]|\\.)*")', line)
                    if m:
                        import json
                        return str(json.loads(m.group(1)))
        except OSError:
            continue
    return None


def resume_command(session_id: str, cwd: str, windows: bool) -> str:
    """The command that goes on with a session in Claude Code (PowerShell on Windows)."""
    if windows:
        return f"Set-Location -LiteralPath '{cwd.replace(chr(39), chr(39) * 2)}'; claude --resume {session_id}"
    import shlex
    return f"cd {shlex.quote(cwd)} && claude --resume {session_id}"


def open_terminal(session_id: str, cwd: str) -> None:
    """Opens the system terminal in `cwd`, going on with the session in Claude Code."""
    import shutil
    import subprocess
    import sys
    if not SESSION_ID.fullmatch(session_id) or not Path(cwd).is_dir():
        raise ValueError("no such session or folder on this machine")
    resume = f"claude --resume {session_id}"
    if sys.platform == "win32":
        wt = shutil.which("wt")
        if wt:
            subprocess.Popen([wt, "-d", cwd, "powershell", "-NoExit", "-Command", resume])
        else:
            subprocess.Popen(["powershell", "-NoExit", "-Command", resume], cwd=cwd,
                             creationflags=getattr(subprocess, "CREATE_NEW_CONSOLE", 0))
    elif sys.platform == "darwin":
        script = resume_command(session_id, cwd, False).replace("\\", "\\\\").replace('"', '\\"')
        subprocess.Popen(["osascript", "-e", f'tell application "Terminal" to do script "{script}"'])
    else:
        term = shutil.which("x-terminal-emulator") or shutil.which("gnome-terminal") or shutil.which("xterm")
        if not term:
            raise ValueError("no terminal found: run the command yourself")
        subprocess.Popen([term, "-e", "bash", "-lc", f"{resume_command(session_id, cwd, False)}; exec bash"])


def _key(folder: str) -> str:
    return re.sub(r"[^a-zA-Z0-9]", "-", folder)


def project_dir(folder: str | Path) -> Path | None:
    """The Claude Code folder of a project folder (None when it has none)."""
    base = config_dir() / "projects"
    for name in {str(folder), os.path.realpath(folder)}:
        key = _key(name)
        if len(key) <= _SANITIZED_MAX:
            if (base / key).is_dir():
                return base / key
            continue
        # long paths: cut and followed by a hash (whose function differs between runtimes)
        try:
            found = next((d for d in base.iterdir()
                          if d.is_dir() and d.name.startswith(key[:_SANITIZED_MAX] + "-")), None)
        except OSError:
            found = None
        if found:
            return found
    return None


def mask(text: str) -> str:
    from .connections.model import _SECRET_LIKE
    return _SECRET_LIKE.sub("[secret removed]", text)


def _entrypoint(path: Path) -> str | None:
    """The program that started a session (from its first lines; best effort)."""
    try:
        with path.open(encoding="utf-8", errors="replace") as f:
            for i, line in enumerate(f):
                if '"entrypoint"' in line[:20_000]:
                    m = _ENTRYPOINT.search(line)
                    if m:
                        return m.group(1)
                if i >= 40:
                    break
    except OSError:
        return None
    return None


def kind_of(tag: str | None, first_prompt: str | None, entrypoint: str | None) -> str:
    """`studio` (this studio's runs and assistants), `program` (started by another program
    through the SDK) or `person` (a conversation of the person)."""
    if tag == STUDIO_TAG or (first_prompt or "").lstrip().startswith(STUDIO_PREFIXES):
        return "studio"
    if entrypoint and entrypoint.startswith("sdk"):
        return "program"
    return "person"


def _session_file(info: Any, folder: str) -> Path | None:
    for where in (getattr(info, "cwd", None), folder):
        d = project_dir(where) if where else None
        if d and (d / f"{info.session_id}.jsonl").is_file():
            return d / f"{info.session_id}.jsonl"
    return None


def _ms(v: Any) -> float | None:
    return v / 1000 if isinstance(v, (int, float)) else None


def list_sessions(folder: str, learned: dict[str, float] | None = None) -> list[dict[str, Any]]:
    """The sessions of a folder (and its git worktrees), newest first."""
    from claude_agent_sdk import list_sessions as sdk_list
    learned = learned or {}
    try:
        infos = sdk_list(directory=folder, include_worktrees=True)
    except (OSError, ValueError):
        return []
    out = []
    for s in infos:
        path = _session_file(s, folder)
        first = _clean(s.first_prompt or "")[:200]
        modified = _ms(s.last_modified)
        seen = learned.get(s.session_id)
        out.append({
            "id": s.session_id, "title": _clean(s.custom_title or s.summary or "")[:120],
            "first_prompt": first, "branch": s.git_branch, "cwd": s.cwd,
            "created": _ms(s.created_at), "modified": modified, "size": s.file_size,
            "kind": kind_of(s.tag, s.first_prompt, _entrypoint(path) if path else None),
            "state": "new" if seen is None else
                     "changed" if modified and modified > seen + 1 else "learned"})
    out.sort(key=lambda x: x["modified"] or 0, reverse=True)
    return out


def _frontmatter(text: str) -> dict[str, str]:
    if not text.startswith("---"):
        return {}
    end = text.find("\n---", 3)
    out = {}
    for line in text[3:max(0, end)].splitlines():
        k, sep, v = line.partition(":")
        if sep and not line.startswith((" ", "\t")):
            out[k.strip()] = v.strip()
    return out


def list_memory(folder: str, learned: dict[str, float] | None = None) -> list[dict[str, Any]]:
    """The Claude Code memory notes of a folder (the MEMORY.md index left out)."""
    learned = learned or {}
    d = project_dir(folder)
    if d is None or not (d / "memory").is_dir():
        return []
    out = []
    for f in sorted((d / "memory").glob("*.md")):
        if f.name.upper() == "MEMORY.MD":
            continue
        try:
            text, st = f.read_text(encoding="utf-8", errors="replace"), f.stat()
        except OSError:
            continue
        meta = _frontmatter(text)
        seen = learned.get(f.name)
        out.append({"file": f.name, "name": mask(meta.get("name") or f.stem)[:120],
                    "description": mask(meta.get("description") or "")[:300],
                    "size": st.st_size, "modified": st.st_mtime,
                    "state": "new" if seen is None else
                             "changed" if st.st_mtime > seen + 1 else "learned"})
    return out


# --- the text that goes to the readers ---------------------------------------------------------
def _clean(text: str) -> str:
    text = _INJECTED.sub("", text)
    text = _PASTED.sub(lambda m: "[pasted text]\n" + m.group(1).strip()[:1000]
                       + ("…" if len(m.group(1).strip()) > 1000 else ""), text)
    return mask(text).strip()


def _texts(content: Any) -> list[str]:
    if isinstance(content, str):
        return [content]
    if not isinstance(content, list):
        return []
    return [str(b.get("text") or "") for b in content
            if isinstance(b, dict) and b.get("type") == "text"]


def cut(text: str, limit: int) -> str:
    """Keeps the start (a compaction summary) and the end (the latest work)."""
    if len(text) <= limit:
        return text
    head = int(limit * 0.4)
    tail = limit - head
    return (text[:head].rsplit("\n", 1)[0] + "\n\n[… the middle of this session was cut …]\n\n"
            + text[-tail:].split("\n", 1)[-1])


def digest(session_id: str, folder: str, info: dict[str, Any] | None = None,
           limit: int = MAX_DIGEST) -> str:
    """The conversation of one session as Markdown: the person's messages and Claude's text."""
    from claude_agent_sdk import get_session_messages
    key = (session_id, float((info or {}).get("modified") or 0))
    if key[1] and key in _cache:
        _cache.move_to_end(key)
        return _cache[key]
    lines = []
    for m in get_session_messages(session_id, directory=folder):
        if m.parent_tool_use_id or m.type not in ("user", "assistant"):
            continue
        msg = m.message if isinstance(m.message, dict) else {}
        for t in _texts(msg.get("content")):
            if m.type == "user" and t.lstrip().startswith(_COMPACT):
                lines.append(f"**Summary of earlier work in this session:**\n{mask(t).strip()}")
                continue
            t = _clean(t)
            if t:
                lines.append(f"**{'Person' if m.type == 'user' else 'Claude'}:** {t}")
    head = []
    if info and lines:
        head.append(f"Session `{session_id}`" + (f" — {info['title']}" if info.get("title") else ""))
        if info.get("branch"):
            head.append(f"Branch: {info['branch']}")
    text = cut("\n\n".join([*head, *lines]), limit)
    if key[1]:
        _cache[key] = text
        while len(_cache) > 64:
            _cache.popitem(last=False)
    return text


def memory_digest(folder: str, files: list[str], limit: int = MAX_MEMORY) -> str:
    """The chosen memory notes, one block each."""
    d = project_dir(folder)
    if d is None:
        return ""
    blocks = []
    for name in files:
        f = d / "memory" / name
        if Path(name).name != name or not f.is_file():
            continue
        text = f.read_text(encoding="utf-8", errors="replace")
        blocks.append(f"#### Memory note `{name}`\n{mask(text).strip()}")
    return cut("\n\n".join(blocks), limit)


def estimate(folder: str, sessions: list[dict[str, Any]], memory: list[str]) -> dict[str, int]:
    """The size of what the readers would get: characters and about how many tokens."""
    chars = sum(len(digest(s["id"], folder, s)) for s in sessions)
    if memory:
        chars += len(memory_digest(folder, memory))
    return {"chars": chars, "tokens": chars // 4}


def mark_ours(session_id: str | None, cwd: str | Path | None = None) -> None:
    """Tags a session of this studio, so it is never offered as a source (best effort)."""
    if not session_id:
        return
    from claude_agent_sdk import tag_session
    # the tag is only a hint (older sessions are known otherwise): no error stops a run here
    with contextlib.suppress(Exception):
        tag_session(session_id, STUDIO_TAG, directory=str(cwd) if cwd else None)

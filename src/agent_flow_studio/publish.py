"""The "public" place: what git commits to the public repository, chosen explicitly by a person.

- Flows: `flows/<name>/` with an exception line `!flows/<name>/` in `.gitignore` (flows/* is
  ignored by default).
- Library agents: the built-in library in the package (`src/agent_flow_studio/agents/`).
- Connectors: `connections.public.yaml` at the root (definitions only; secrets never, as anywhere).

Anything that goes public is checked against the private terms (`private_terms.txt` outside git
and `extensions/<name>/private_terms.txt`): a match blocks it.
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

from .extensions import LOADED, extension_root

PUBLIC = "public"


class PrivateTermsError(ValueError):
    def __init__(self, terms: list[str]):
        super().__init__("it names private words: " + ", ".join(terms))
        self.terms = terms


def private_terms() -> list[str]:
    files = [Path("private_terms.txt"), *(extension_root(n) / "private_terms.txt"
                                          for n in LOADED.names)]
    out: list[str] = []
    for f in files:
        if f.is_file():
            out += [ln.strip() for ln in f.read_text(encoding="utf-8").splitlines()
                    if ln.strip() and not ln.lstrip().startswith("#")]
    return out


def terms_in(text: str) -> list[str]:
    low = text.lower()
    return sorted({t for t in private_terms() if t.lower() in low})


def check_public(text: str) -> None:
    found = terms_in(text)
    if found:
        raise PrivateTermsError(found)


def public_connections_file() -> Path:
    return Path(os.getenv("AFS_PUBLIC_CONNECTIONS_FILE", "connections.public.yaml"))


# --- flows: the .gitignore exception -------------------------------------------------------------
def _repo_root(start: Path) -> Path | None:
    try:
        r = subprocess.run(["git", "rev-parse", "--show-toplevel"], cwd=str(start),
                           capture_output=True, text=True, timeout=5, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    return Path(r.stdout.strip()) if r.returncode == 0 and r.stdout.strip() else None


def _exception_line(base: Path, name: str) -> tuple[Path, str] | None:
    """(.gitignore path, "!flows/<name>/") for a flow folder in base, or None outside git."""
    base = base.resolve()
    root = _repo_root(base if base.is_dir() else base.parent)
    if root is None:
        return None
    rel = (base / name).relative_to(root.resolve()).as_posix()
    return root / ".gitignore", f"!{rel}/"


def set_flow_public(base: Path, name: str, public: bool) -> bool:
    """Adds or removes the flow's exception in .gitignore. False when there is no git here."""
    got = _exception_line(base, name)
    if got is None:
        return False
    path, line = got
    lines = path.read_text(encoding="utf-8").splitlines() if path.is_file() else []
    has = line in (x.strip() for x in lines)
    if public and not has:
        # right after the flows/* rule when there is one, so the exception applies
        rule = f"{line[1:].rsplit('/', 2)[0]}/*"
        at = next((i + 1 for i, x in enumerate(lines) if x.strip() == rule), len(lines))
        while at < len(lines) and lines[at].strip().startswith("!"):
            at += 1
        lines.insert(at, line)
    elif not public and has:
        lines = [x for x in lines if x.strip() != line]
    else:
        return True
    path.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")
    return True

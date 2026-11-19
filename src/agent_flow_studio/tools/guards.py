"""Pure validations (no SDK dependencies)."""
import re
from pathlib import Path

_NAME_RE = re.compile(r"[A-Za-z0-9_.-]{1,80}")
_RUN_ID_RE = re.compile(r"[A-Za-z0-9_-]{1,40}")


def safe_artifact_path(runs_dir: Path, run_id: str, name: str) -> Path:
    """Path of an artifact in runs/<id>/, never outside that folder."""
    if not _RUN_ID_RE.fullmatch(run_id):
        raise ValueError("Invalid run id.")
    if not _NAME_RE.fullmatch(name) or ".." in name:
        raise ValueError("Invalid artifact name.")
    base = (runs_dir / run_id).resolve()
    path = (base / name).resolve()
    if base not in path.parents:
        raise ValueError("Path outside the run folder.")
    return path

"""Validações puras (sem dependências do SDK)."""
import re
from pathlib import Path

_NAME_RE = re.compile(r"[A-Za-z0-9_.-]{1,80}")
_RUN_ID_RE = re.compile(r"[A-Za-z0-9_-]{1,40}")


def safe_artifact_path(runs_dir: Path, run_id: str, name: str) -> Path:
    """Caminho de um artefacto em runs/<id>/, sem sair dessa pasta."""
    if not _RUN_ID_RE.fullmatch(run_id):
        raise ValueError("Id de execução inválido.")
    if not _NAME_RE.fullmatch(name) or ".." in name:
        raise ValueError("Nome de artefacto inválido.")
    base = (runs_dir / run_id).resolve()
    path = (base / name).resolve()
    if base not in path.parents:
        raise ValueError("Caminho fora da pasta da execução.")
    return path

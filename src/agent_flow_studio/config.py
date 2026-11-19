import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Settings:
    mock: bool = os.getenv("AFS_MOCK", "1") == "1"  # as extensões podem usar dados simulados
    runs_dir: Path = Path(os.getenv("RUNS_DIR", "runs"))
    model: str | None = os.getenv("AFS_MODEL") or None
    max_turns: int = int(os.getenv("MAX_TURNS", "60"))


settings = Settings()

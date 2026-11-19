"""Os testes do núcleo usam o flow genérico tests/fixtures/flows/pipeline (sem extensões)."""
from pathlib import Path

import pytest

PIPELINE = Path(__file__).parent / "fixtures" / "flows" / "pipeline"


@pytest.fixture(autouse=True)
def _flows_dir(monkeypatch):
    monkeypatch.setenv("FLOWS_DIR", str(PIPELINE.parent))

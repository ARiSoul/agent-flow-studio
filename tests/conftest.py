"""Core tests use the generic flow tests/fixtures/flows/pipeline (no extensions)."""
from pathlib import Path

import pytest

PIPELINE = Path(__file__).parent / "fixtures" / "flows" / "pipeline"


@pytest.fixture(autouse=True)
def _flows_dir(monkeypatch):
    monkeypatch.setenv("FLOWS_DIR", str(PIPELINE.parent))

"""Core tests use the generic flow tests/fixtures/flows/pipeline (no extensions)."""
from dataclasses import replace
from pathlib import Path

import pytest

from agent_flow_studio import hooks
from agent_flow_studio.config import settings
from agent_flow_studio.editor import runner as runner_mod
from agent_flow_studio.tools import artifacts
from agent_flow_studio.tools import io as io_tool

PIPELINE = Path(__file__).parent / "fixtures" / "flows" / "pipeline"


@pytest.fixture(autouse=True)
def _flows_dir(monkeypatch):
    monkeypatch.setenv("FLOWS_DIR", str(PIPELINE.parent))


@pytest.fixture(autouse=True)
def _runs_dir(tmp_path, monkeypatch):
    """Safety net: no test writes in the real runs/ folder. Fixtures that need the folder use
    the same path (tmp_path / "runs")."""
    s = replace(settings, runs_dir=tmp_path / "runs")
    for mod in (hooks, runner_mod, artifacts, io_tool):
        monkeypatch.setattr(mod, "settings", s)

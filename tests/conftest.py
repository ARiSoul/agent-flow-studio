"""Core tests use the generic flow tests/fixtures/flows/pipeline (no extensions)."""
from dataclasses import replace
from pathlib import Path

import pytest

from agent_flow_studio import engine, hooks
from agent_flow_studio.config import settings
from agent_flow_studio.editor import chats
from agent_flow_studio.editor import runner as runner_mod
from agent_flow_studio.editor import server as server_mod
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
    for mod in (hooks, engine, runner_mod, artifacts, io_tool, chats, server_mod):
        monkeypatch.setattr(mod, "settings", s)


@pytest.fixture(autouse=True)
def _agents_dir(tmp_path, monkeypatch):
    """Safety net: the local agent library of the tests is a temporary folder."""
    monkeypatch.setenv("AFS_AGENTS_DIR", str(tmp_path / "agents"))


@pytest.fixture(autouse=True)
def _commands_file(tmp_path, monkeypatch):
    """Safety net: the saved assistant commands of the tests are a temporary file."""
    monkeypatch.setenv("AFS_COMMANDS_FILE", str(tmp_path / "assistant_commands.yaml"))

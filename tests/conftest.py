"""Core tests use the generic flow tests/fixtures/flows/pipeline (no extensions)."""
from dataclasses import replace
from pathlib import Path

import pytest

from agent_flow_studio import engine, hooks
from agent_flow_studio.config import settings
from agent_flow_studio.editor import chats, uploads
from agent_flow_studio.editor import runner as runner_mod
from agent_flow_studio.editor import server as server_mod
from agent_flow_studio.editor.api import common as api_common
from agent_flow_studio.editor.runs import control, events, history, queue, workspace_context
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
    for mod in (hooks, engine, runner_mod, artifacts, io_tool, chats, server_mod, uploads,
                control, events, history, queue, workspace_context,  # the runner's parts
                api_common):  # the API helpers that use it
        monkeypatch.setattr(mod, "settings", s)


@pytest.fixture(autouse=True)
def _agents_dir(tmp_path, monkeypatch):
    """Safety net: the local agent library of the tests is a temporary folder."""
    monkeypatch.setenv("AFS_AGENTS_DIR", str(tmp_path / "agents"))


@pytest.fixture(autouse=True)
def _commands_file(tmp_path, monkeypatch):
    """Safety net: the saved assistant commands of the tests are a temporary file."""
    monkeypatch.setenv("AFS_COMMANDS_FILE", str(tmp_path / "assistant_commands.yaml"))


@pytest.fixture(autouse=True)
def _workspaces(tmp_path, monkeypatch):
    """Safety net: the workspace registry, the studio's workspaces/ folder and the browser
    profiles of the tests are temporary."""
    monkeypatch.setenv("AFS_WORKSPACES_FILE", str(tmp_path / "workspaces.json"))
    monkeypatch.setenv("AFS_WORKSPACES_DIR", str(tmp_path / "workspaces"))
    monkeypatch.setenv("AFS_BROWSER_DIR", str(tmp_path / "browser"))


@pytest.fixture(autouse=True)
def _connection_files(tmp_path, monkeypatch):
    """Safety net: the local and public connectors and the model providers of the tests are
    temporary files (a test that saves a connector never writes the machine's connections.yaml
    or the repository's connections.public.yaml)."""
    monkeypatch.setenv("CONNECTIONS_FILE", str(tmp_path / "connections.yaml"))
    monkeypatch.setenv("PROVIDERS_FILE", str(tmp_path / "providers.yaml"))
    monkeypatch.setenv("AFS_PUBLIC_CONNECTIONS_FILE", str(tmp_path / "connections.public.yaml"))


@pytest.fixture(autouse=True)
def _memory_keyring(monkeypatch):
    """Safety net: secrets and sign-in tokens saved by a test go to a dictionary, never to the
    machine's keyring (a test may still put its own fake in place)."""
    from agent_flow_studio import providers
    from agent_flow_studio.connections import store
    vault: dict = {}

    class Memory:
        set_password = staticmethod(lambda s, u, v: vault.__setitem__((s, u), v))
        get_password = staticmethod(lambda s, u: vault.get((s, u)))

        @staticmethod
        def delete_password(s, u):
            if (s, u) not in vault:
                import keyring.errors
                raise keyring.errors.PasswordDeleteError(u)
            vault.pop((s, u))

    monkeypatch.setattr(store, "_keyring", lambda: Memory)
    if hasattr(providers, "_keyring"):
        monkeypatch.setattr(providers, "_keyring", lambda: Memory)


@pytest.fixture(autouse=True)
def _claude_config(tmp_path, monkeypatch):
    """Safety net: Claude Code sessions and memory notes of the tests are a temporary folder."""
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude"))

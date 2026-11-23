"""Builds the in-process MCP servers (core + extensions) and the list of tools."""
from claude_agent_sdk import create_sdk_mcp_server

from .extensions import LOADED
from .tools import artifacts, connections, git, io

_GROUPS = {
    "artifacts": [artifacts.save_artifact, artifacts.read_artifact],
    "connections": [connections.http_get, connections.http_write, connections.run_action],
    # typed inputs/outputs of the agents, and the files of the run
    "io": [io.get_inputs, io.set_output, io.read_file, io.view_image, io.view_video],
    "workspace": [git.git],  # read-only git; the other workspace.* tools are native (catalog.py)
    **LOADED.tool_groups,
}

MCP_SERVERS = {
    name: create_sdk_mcp_server(name=name, version="0.1.0", tools=tools)
    for name, tools in _GROUPS.items()
}


def t(server: str, tool_name: str) -> str:
    """Full tool name, as the SDK exposes it."""
    return f"mcp__{server}__{tool_name}"


ALL_TOOLS = [t(s, x.name) for s, tools in _GROUPS.items() for x in tools]

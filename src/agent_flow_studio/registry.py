"""Monta os servidores MCP em processo (núcleo + extensões) e a lista de ferramentas."""
from claude_agent_sdk import create_sdk_mcp_server

from .extensions import LOADED
from .tools import artifacts, connections

_GROUPS = {
    "artifacts": [artifacts.save_artifact, artifacts.read_artifact],
    "connections": [connections.http_get, connections.http_write],
    **LOADED.tool_groups,
}

MCP_SERVERS = {
    name: create_sdk_mcp_server(name=name, version="0.1.0", tools=tools)
    for name, tools in _GROUPS.items()
}


def t(server: str, tool_name: str) -> str:
    """Nome completo da ferramenta, como o SDK a expõe."""
    return f"mcp__{server}__{tool_name}"


ALL_TOOLS = [t(s, x.name) for s, tools in _GROUPS.items() for x in tools]

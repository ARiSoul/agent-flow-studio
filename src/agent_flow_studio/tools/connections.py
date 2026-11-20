"""Generic tools that call the configured connectors (connections.yaml).

The guard hook makes sure each subagent uses only the connectors the flow assigned to it.
"""
import httpx
from claude_agent_sdk import tool

from ..connections import actions
from ..connections.client import WRITE_METHODS, ConnectorError, get, write
from ..connections.store import ConnectorConfigError
from .common import err, ok


@tool(
    "http_get",
    "GET (read-only) on a configured connector. 'path' is relative to the connector's base URL "
    "and starts with / (e.g. /_apis/projects?api-version=7.1). You can only use the connectors "
    "assigned to you.",
    {"type": "object", "properties": {
        "connection": {"type": "string"},
        "path": {"type": "string"},
        "query": {"type": "object", "additionalProperties": {"type": "string"}},
    }, "required": ["connection", "path"]},
)
async def http_get(args):
    try:
        r = await get(args["connection"], args["path"], args.get("query"))
    except (ConnectorError, ConnectorConfigError, httpx.HTTPError) as e:
        return err(f"{type(e).__name__}: {e}")
    return ok(r)


@tool(
    "http_write",
    "Write (POST/PUT/PATCH/DELETE) on a connector with write access: create a PR, comment, "
    "update a work item, etc. 'path' is relative to the base URL; 'body' is JSON. Depending on "
    "the flow autonomy, the call may wait for human approval or be denied.",
    {"type": "object", "properties": {
        "connection": {"type": "string"},
        "method": {"type": "string", "enum": list(WRITE_METHODS)},
        "path": {"type": "string"},
        "body": {},
        "query": {"type": "object", "additionalProperties": {"type": "string"}},
    }, "required": ["connection", "method", "path"]},
)
async def http_write(args):
    try:
        r = await write(args["connection"], args["method"], args["path"], args.get("body"),
                        args.get("query"))
    except (ConnectorError, ConnectorConfigError, httpx.HTTPError) as e:
        return err(f"{type(e).__name__}: {e}")
    return ok(r)


@tool(
    "run_action",
    "Runs a named action of a connector (the actions listed in your instructions, with their "
    "parameters). The studio checks the values and builds the request; write actions follow the "
    "flow autonomy (they may wait for approval or be denied).",
    {"type": "object", "properties": {
        "connection": {"type": "string"},
        "action": {"type": "string"},
        "params": {"type": "object"},
    }, "required": ["connection", "action"]},
)
async def run_action(args):
    try:
        r = await actions.run(args["connection"], args["action"], args.get("params"))
    except (actions.ActionError, ConnectorError, ConnectorConfigError, httpx.HTTPError) as e:
        return err(f"{type(e).__name__}: {e}")
    return ok(r)

"""Ferramenta genérica de leitura através dos conectores configurados (connections.yaml).

O hook de guarda garante que cada subagente só usa os conectores que o flow lhe atribuiu.
"""
import httpx
from claude_agent_sdk import tool

from ..connections.client import WRITE_METHODS, ConnectorError, get, write
from ..connections.store import ConnectorConfigError
from .common import err, ok


@tool(
    "http_get",
    "GET (só leitura) num conector configurado. 'path' é relativo à URL base do conector e começa "
    "por / (ex.: /_apis/projects?api-version=7.1). Só podes usar os conectores que te foram "
    "atribuídos.",
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
    "Escrita (POST/PUT/PATCH/DELETE) num conector com acesso de escrita: criar PR, comentar, "
    "atualizar um work item, etc. 'path' relativo à URL base; 'body' em JSON. Conforme a "
    "autonomia do flow, a chamada pode esperar aprovação humana ou ser recusada.",
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

"""Authentication per type and GET requests (read-only) through a connector.

Secrets and tokens exist in memory only during the request (OAuth tokens stay cached until they
expire). Error messages never include headers or secrets.
"""
from __future__ import annotations

import asyncio
import base64
import json
import os
import shutil
import time
from typing import Any

import httpx

from . import oauth, store
from .model import Connection

MAX_BODY = 100_000
TIMEOUT = 30.0
USER_AGENT = "agent-flow-studio"
IMDS_URL = "http://169.254.169.254/metadata/identity/oauth2/token"  # Azure VM / AKS
_tokens: dict[str, tuple[str, float]] = {}  # key → (token, expires at)
_transport: httpx.AsyncBaseTransport | None = None  # tests: httpx.MockTransport


class ConnectorError(Exception):
    pass


async def _cli(*args: str) -> str:
    exe = shutil.which(args[0])
    if not exe:
        raise ConnectorError(f"'{args[0]}' is not installed or not on PATH")
    proc = await asyncio.create_subprocess_exec(exe, *args[1:], stdout=asyncio.subprocess.PIPE,
                                                stderr=asyncio.subprocess.PIPE)
    out, errb = await asyncio.wait_for(proc.communicate(), timeout=60)
    if proc.returncode != 0:
        msg = errb.decode("utf-8", "replace").strip().splitlines()[-1:] or ["error"]
        raise ConnectorError(f"{args[0]} failed: {msg[0][:200]}")
    return out.decode("utf-8").strip()


def _secret(name: str, conn: Connection) -> str:
    field = conn.secret_field
    value = store.get_secret(name, conn, field) if field else None
    if not value:
        raise ConnectorError(f"secret '{field}' is not set for connector {name!r}")
    return value


async def _client_credentials(name: str, conn: Connection) -> str:
    key = f"{name}:cc"
    cached = _tokens.get(key)
    if cached and cached[1] > time.time() + 60:
        return cached[0]
    url = conn.auth.token_url or (
        f"https://login.microsoftonline.com/{conn.auth.tenant_id}/oauth2/v2.0/token")
    data = {"grant_type": "client_credentials", "client_id": conn.auth.client_id or "",
            "client_secret": _secret(name, conn)}
    if conn.resolved_scope():
        data["scope"] = conn.resolved_scope() or ""
    async with httpx.AsyncClient(timeout=TIMEOUT, follow_redirects=False, transport=_transport) as c:
        r = await c.post(url, data=data)
    if r.status_code != 200:
        raise ConnectorError(f"getting the token failed (HTTP {r.status_code})")
    body = r.json()
    _tokens[key] = (body["access_token"], time.time() + float(body.get("expires_in", 300)))
    return body["access_token"]


async def _azure_cli(conn: Connection) -> str:
    scope = conn.resolved_scope() or ""
    resource = scope.removesuffix("/.default")
    out = await _cli("az", "account", "get-access-token", "--resource", resource, "--output", "json")
    return json.loads(out)["accessToken"]


async def _managed_identity(name: str, conn: Connection) -> str:
    """Azure managed identity: App Service/Functions/Container Apps (IDENTITY_ENDPOINT) or IMDS."""
    key = f"{name}:mi"
    cached = _tokens.get(key)
    if cached and cached[1] > time.time() + 60:
        return cached[0]
    resource = (conn.resolved_scope() or "").removesuffix("/.default")
    endpoint, secret = os.getenv("IDENTITY_ENDPOINT"), os.getenv("IDENTITY_HEADER")
    if endpoint and secret:
        url, headers = endpoint, {"X-IDENTITY-HEADER": secret}
        params = {"resource": resource, "api-version": "2019-08-01"}
    else:
        url, headers = IMDS_URL, {"Metadata": "true"}
        params = {"resource": resource, "api-version": "2018-02-01"}
    if conn.auth.client_id:  # user-assigned identity
        params["client_id"] = conn.auth.client_id
    try:
        async with httpx.AsyncClient(timeout=5.0, follow_redirects=False,
                                     transport=_transport) as c:
            r = await c.get(url, params=params, headers=headers)
    except httpx.HTTPError:
        raise ConnectorError("managed identity is not available here (not running on Azure, "
                             "or no identity assigned)") from None
    if r.status_code != 200:
        raise ConnectorError(f"managed identity token failed (HTTP {r.status_code})")
    body = r.json()
    if body.get("expires_on"):
        expires = float(body["expires_on"])
    else:
        expires = time.time() + float(body.get("expires_in", 300))
    _tokens[key] = (body["access_token"], expires)
    return body["access_token"]


async def _oauth(name: str, conn: Connection) -> str:
    try:
        return await oauth.access_token(name, conn)
    except oauth.OAuthError as e:
        raise ConnectorError(str(e)) from None
    except httpx.HTTPError as e:
        raise ConnectorError(f"renewing the sign-in failed: {type(e).__name__}") from None


async def auth_params(name: str, conn: Connection) -> tuple[dict[str, str], dict[str, str]]:
    """(headers, query) that authenticate a request. Only sign-in with token_in: query uses query."""
    if conn.auth.method == "oauth_browser":
        token = await _oauth(name, conn)
        if conn.auth.token_in == "query":
            return {}, {"access_token": token}
        return {"Authorization": f"Bearer {token}"}, {}
    return await auth_headers(name, conn), {}


async def auth_headers(name: str, conn: Connection) -> dict[str, str]:
    m = conn.auth.method
    if m == "none":
        return {}
    if m == "api_key":
        return {conn.auth.header: _secret(name, conn)}
    if m == "bearer":
        return {"Authorization": f"Bearer {_secret(name, conn)}"}
    if m == "basic":
        raw = f"{conn.auth.username}:{_secret(name, conn)}".encode()
        return {"Authorization": "Basic " + base64.b64encode(raw).decode()}
    if m == "pat":
        if conn.type == "azure_devops":
            raw = f":{_secret(name, conn)}".encode()
            return {"Authorization": "Basic " + base64.b64encode(raw).decode()}
        return {"Authorization": f"Bearer {_secret(name, conn)}"}
    if m == "client_credentials":
        return {"Authorization": f"Bearer {await _client_credentials(name, conn)}"}
    if m == "azure_cli":
        return {"Authorization": f"Bearer {await _azure_cli(conn)}"}
    if m == "gh_cli":
        return {"Authorization": f"Bearer {await _cli('gh', 'auth', 'token')}"}
    if m == "managed_identity":
        return {"Authorization": f"Bearer {await _managed_identity(name, conn)}"}
    if m == "oauth_browser":
        return {"Authorization": f"Bearer {await _oauth(name, conn)}"}
    raise ConnectorError(f"unknown method {m!r}")


def _scrub(text: str, hidden: list[str]) -> str:
    for h in hidden:
        if h:
            text = text.replace(h, "***")
    return text


async def _send(conn: Connection, method: str, path: str, headers: dict[str, str],
                params: dict[str, str], auth_query: dict[str, str], body: Any = None
                ) -> httpx.Response:
    """Sends the request; transport errors never show the token (it may be in the query)."""
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT, follow_redirects=False,
                                     transport=_transport) as c:
            return await c.request(method, conn.resolved_base_url() + path, headers=headers,
                                   params={**params, **auth_query},
                                   json=body if body is not None else None)
    except httpx.HTTPError as e:
        hidden = [*auth_query.values(), *headers.values()]
        raise ConnectorError(_scrub(f"{type(e).__name__}: {e}", hidden)[:300]) from None


def check_path(conn: Connection, path: str) -> None:
    if not path.startswith("/") or ".." in path or "://" in path or "\\" in path or "@" in path:
        raise ConnectorError("invalid path: must be relative to the base URL and start with /")
    bare = path.split("?", 1)[0]
    if conn.allowed_paths and not any(bare.startswith(p.split("?", 1)[0])
                                      for p in conn.allowed_paths):
        raise ConnectorError(f"path not allowed; allowed: {', '.join(conn.allowed_paths)}")


async def get(name: str, path: str, query: dict[str, Any] | None = None,
              conns: dict[str, Connection] | None = None) -> dict[str, Any]:
    conns = store.load() if conns is None else conns
    conn = conns.get(name)
    if conn is None:
        raise ConnectorError(f"connector {name!r} is not configured")
    check_path(conn, path)
    auth_h, auth_q = await auth_params(name, conn)
    headers = {"Accept": "application/json", "User-Agent": USER_AGENT, **auth_h}
    if conn.type == "github":
        headers["X-GitHub-Api-Version"] = "2022-11-28"
    t0 = time.perf_counter()
    r = await _send(conn, "GET", path, headers, {k: str(v) for k, v in (query or {}).items()},
                    auth_q)
    text = r.text
    try:
        body: Any = r.json()
        text = json.dumps(body, ensure_ascii=False, indent=1)
    except ValueError:
        pass
    return {"status": r.status_code, "elapsed_ms": round((time.perf_counter() - t0) * 1000),
            "content_type": r.headers.get("content-type", ""),
            "truncated": len(text) > MAX_BODY, "body": text[:MAX_BODY]}


WRITE_METHODS = ("POST", "PUT", "PATCH", "DELETE")


async def write(name: str, method: str, path: str, body: Any = None,
                query: dict[str, Any] | None = None,
                conns: dict[str, Connection] | None = None) -> dict[str, Any]:
    """Write request. Only on connectors with access: write; the hook handles approval."""
    conns = store.load() if conns is None else conns
    conn = conns.get(name)
    if conn is None:
        raise ConnectorError(f"connector {name!r} is not configured")
    if conn.access != "write":
        raise ConnectorError(f"connector {name!r} is read-only (access: read)")
    method = method.upper()
    if method not in WRITE_METHODS:
        raise ConnectorError(f"invalid method {method!r}; use {', '.join(WRITE_METHODS)}")
    check_path(conn, path)
    auth_h, auth_q = await auth_params(name, conn)
    headers = {"Accept": "application/json", "User-Agent": USER_AGENT, **auth_h}
    if conn.type == "github":
        headers["X-GitHub-Api-Version"] = "2022-11-28"
    if conn.type == "azure_devops" and method == "PATCH" and "/wit/" in path:
        headers["Content-Type"] = "application/json-patch+json"
    t0 = time.perf_counter()
    r = await _send(conn, method, path, headers, {k: str(v) for k, v in (query or {}).items()},
                    auth_q, body)
    text = r.text
    try:
        text = json.dumps(r.json(), ensure_ascii=False, indent=1)
    except ValueError:
        pass
    return {"status": r.status_code, "elapsed_ms": round((time.perf_counter() - t0) * 1000),
            "truncated": len(text) > MAX_BODY, "body": text[:MAX_BODY]}


async def test(name: str, conns: dict[str, Connection] | None = None) -> dict[str, Any]:
    conns = store.load() if conns is None else conns
    conn = conns.get(name)
    if conn is None:
        return {"ok": False, "detail": "connector does not exist"}
    try:
        r = await get(name, conn.resolved_test_path(), conns=conns)
    except (ConnectorError, httpx.HTTPError) as e:
        return {"ok": False, "detail": f"{type(e).__name__}: {e}"[:300]}
    ok = 200 <= r["status"] < 300
    return {"ok": ok, "status": r["status"], "elapsed_ms": r["elapsed_ms"],
            "detail": "connection OK" if ok else r["body"][:300]}

"""Sign-in on a page for connectors: OAuth 2 authorization code with PKCE (S256).

The editor starts the sign-in (`start`), the provider sends the browser back to the editor's
callback with a one-time `state`, and `finish` swaps the code for tokens. Tokens live only in the
keyring (`store.set_oauth_tokens`); `access_token` renews them with the refresh token when there is
one. Error messages never include tokens, codes or secrets.
"""
from __future__ import annotations

import base64
import hashlib
import secrets
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import parse_qsl, urlencode

import httpx

from . import store
from .model import Connection

PENDING_TTL = 600  # seconds a started sign-in stays valid
TIMEOUT = 30.0


class OAuthError(Exception):
    pass


@dataclass(frozen=True)
class _Pending:
    name: str
    verifier: str
    redirect_uri: str
    created: float


_pending: dict[str, _Pending] = {}  # state → started sign-in (single use)


def _transport() -> httpx.AsyncBaseTransport | None:
    from . import client  # late import: client imports this module

    return client._transport


def _prune() -> None:
    now = time.time()
    for k in [k for k, p in _pending.items() if now - p.created > PENDING_TTL]:
        _pending.pop(k, None)


def start(name: str, conn: Connection, redirect_uri: str) -> str:
    """Returns the provider's sign-in URL for this connector."""
    if conn.auth.method != "oauth_browser":
        raise OAuthError("this connector does not use sign-in on a page")
    _prune()
    state = secrets.token_urlsafe(32)
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=")
    authorize, _, scope = conn.resolved_oauth()
    params = {**conn.auth.authorize_params, "client_id": conn.auth.client_id or "",
              "response_type": "code", "redirect_uri": redirect_uri, "state": state,
              "code_challenge": challenge.decode(), "code_challenge_method": "S256"}
    if scope:
        params["scope"] = scope
    _pending[state] = _Pending(name, verifier, redirect_uri, time.time())
    return authorize + ("&" if "?" in authorize else "?") + urlencode(params)


def cancel(state: str) -> str | None:
    """Drops a started sign-in (the provider refused it). Returns its connector name."""
    p = _pending.pop(state, None)
    return p.name if p else None


def _normalize(body: dict[str, Any], old: dict[str, Any] | None = None) -> dict[str, Any]:
    expires_in = body.get("expires_in")
    return {
        "access_token": body["access_token"],
        "refresh_token": body.get("refresh_token") or (old or {}).get("refresh_token"),
        "expires_at": time.time() + float(expires_in) if expires_in else None,
    }


def _parse(r: httpx.Response) -> dict[str, Any]:
    try:
        body = r.json()
    except ValueError:
        body = dict(parse_qsl(r.text))  # some providers answer form-encoded
    return body if isinstance(body, dict) else {}


def _refused(body: dict[str, Any], status: int) -> OAuthError:
    code = str(body.get("error") or f"HTTP {status}")[:80]
    desc = str(body.get("error_description") or "")[:200]
    return OAuthError(f"the provider refused the sign-in: {code}" + (f" — {desc}" if desc else ""))


async def _token_request(name: str, conn: Connection, data: dict[str, str]) -> dict[str, Any]:
    _, token_url, _ = conn.resolved_oauth()
    client_id = conn.auth.client_id or ""
    secret = store.get_secret(name, conn, "client_secret")
    headers = {"Accept": "application/json", "User-Agent": "agent-flow-studio"}
    data = {**data, "client_id": client_id}
    if conn.auth.token_auth == "basic":
        raw = f"{client_id}:{secret or ''}".encode()
        headers["Authorization"] = "Basic " + base64.b64encode(raw).decode()
    elif secret:
        data["client_secret"] = secret
    async with httpx.AsyncClient(timeout=TIMEOUT, follow_redirects=False,
                                 transport=_transport()) as c:
        r = await c.post(token_url, data=data, headers=headers)
    body = _parse(r)
    if r.status_code != 200 or "access_token" not in body:
        raise _refused(body, r.status_code)
    return body


async def _exchange(name: str, conn: Connection, tokens: dict[str, Any]) -> dict[str, Any]:
    """Long-lived token (Meta). If the exchange fails, the short-lived token is kept."""
    ex = conn.auth.exchange
    if ex is None:
        return tokens
    params = {"grant_type": ex.grant_type, ex.token_param: tokens["access_token"],
              "client_id": conn.auth.client_id or ""}
    secret = store.get_secret(name, conn, "client_secret")
    if secret:
        params["client_secret"] = secret
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT, follow_redirects=False,
                                     transport=_transport()) as c:
            r = await c.get(ex.url, params=params, headers={"Accept": "application/json"})
    except httpx.HTTPError:
        return tokens
    body = _parse(r)
    return _normalize(body, tokens) if r.status_code == 200 and "access_token" in body else tokens


async def finish(state: str, code: str, conns: dict[str, Connection]) -> str:
    """Swaps the code for tokens and keeps them in the keyring. Returns the connector name."""
    _prune()
    p = _pending.pop(state, None)
    if p is None:
        raise OAuthError("unknown or expired sign-in; start it again in Connectors")
    conn = conns.get(p.name)
    if conn is None or conn.auth.method != "oauth_browser":
        raise OAuthError(f"connector {p.name!r} no longer uses sign-in on a page")
    body = await _token_request(p.name, conn, {
        "grant_type": "authorization_code", "code": code, "redirect_uri": p.redirect_uri,
        "code_verifier": p.verifier})
    tokens = _normalize(body)
    if not tokens["refresh_token"]:
        tokens = await _exchange(p.name, conn, tokens)
    store.set_oauth_tokens(p.name, tokens)
    return p.name


async def access_token(name: str, conn: Connection) -> str:
    """A valid access token: the saved one, or a new one from the refresh token."""
    t = store.get_oauth_tokens(name)
    if not t or not t.get("access_token"):
        raise OAuthError(f"connector {name!r} is not signed in; sign in in Connectors")
    exp = t.get("expires_at")
    if exp is None or exp > time.time() + 60:
        return str(t["access_token"])
    if not t.get("refresh_token"):
        raise OAuthError(f"the sign-in of connector {name!r} expired; sign in again in Connectors")
    data = {"grant_type": "refresh_token", "refresh_token": str(t["refresh_token"])}
    _, _, scope = conn.resolved_oauth()
    if scope:
        data["scope"] = scope
    new = _normalize(await _token_request(name, conn, data), t)
    store.set_oauth_tokens(name, new)
    return str(new["access_token"])

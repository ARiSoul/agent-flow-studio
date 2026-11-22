"""Read/write connections.yaml and the secrets (keyring or environment variables)."""
from __future__ import annotations

import contextlib
import json
import os
from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError

from .model import OAUTH_TOKEN_FIELD, OPTIONAL_SECRET, SLUG, Connection

SERVICE = "agent-flow-studio"  # service name in the Credential Manager


class ConnectorConfigError(ValueError):
    pass


def connections_file() -> Path:
    return Path(os.getenv("CONNECTIONS_FILE", "connections.yaml"))


_locations: dict[str, str] = {}  # connector → "local" or the extension it was read from


def connection_files() -> list[tuple[str, Path]]:
    """Local connections.yaml (this machine) and extensions/<name>/connections.yaml (shared
    through the extension; never secrets there either)."""
    from .. import workspaces
    from ..extensions import extension_connection_files
    from ..publish import PUBLIC, public_connections_file
    ws = workspaces.current()
    own = [] if ws.studio else [("workspace", ws.connections_file)]  # the workspace's own first
    return [*own, ("local", connections_file()), *extension_connection_files(),
            (PUBLIC, public_connections_file())]


def _read(path: Path) -> dict[str, Connection]:
    if not path.is_file():
        return {}
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    out: dict[str, Connection] = {}
    for name, data in (raw.get("connections") or {}).items():
        if not SLUG.fullmatch(str(name)):
            raise ConnectorConfigError(f"connector {name!r}: invalid name")
        try:
            out[name] = Connection.model_validate(data)
        except ValidationError as e:
            raise ConnectorConfigError(f"connector {name!r}: {e}") from e
    return out


def load(path: Path | None = None) -> dict[str, Connection]:
    """One file, or (no path) all of them: local + extensions; a name exists in only one."""
    if path is not None:
        return _read(path)
    out: dict[str, Connection] = {}
    where: dict[str, str] = {}
    for loc, f in connection_files():
        for n, c in _read(f).items():
            if n in out and where[n] == "workspace":
                continue  # a workspace's connector wins over the studio one with the same name
            if n in out:
                raise ConnectorConfigError(f"connector {n!r} exists twice ({where[n]}, {loc})")
            out[n], where[n] = c, loc
    _locations.clear()
    _locations.update(where)
    return out


def location(name: str) -> str:
    """Where a connector is kept; a new one: the open workspace (or this machine's file)."""
    from .. import workspaces
    return _locations.get(name, "local" if workspaces.current().studio else "workspace")


def save(conns: dict[str, Connection], path: Path | None = None,
         moves: dict[str, str] | None = None) -> None:
    """Writes each connector to the file of its location (moves: connector → new location)."""
    if path is not None:
        _write(path, conns, local=True)
        return
    files = dict(connection_files())
    where = {n: (moves or {}).get(n) or location(n) for n in conns}
    unknown = set(where.values()) - set(files)
    if unknown:
        raise ConnectorConfigError(f"unknown location(s): {', '.join(sorted(unknown))}")
    for loc, f in files.items():
        mine = {n: c for n, c in conns.items() if where[n] == loc}
        if mine or f.is_file():
            _write(f, mine, local=loc == "local", workspace=loc == "workspace")
    _locations.clear()
    _locations.update(where)


def _write(path: Path, conns: dict[str, Connection], local: bool,
           workspace: bool = False) -> None:
    data = {"version": 1, "connections": {
        n: {"type": c.type, **c.model_dump(mode="json", exclude_defaults=True)}
        for n, c in sorted(conns.items())}}
    head = ("# Connectors of a studio workspace. No secrets: only ${VAR} or 'keyring' references "
            "(each machine sets its own).\n" if workspace else
            "# Connectors on this machine. No secrets: only ${VAR} or 'keyring' references.\n"
            if local else "# Shared connectors (an extension, or public in git). No secrets: only "
                          "${VAR} or 'keyring' references (each machine sets its own).\n")
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(head + yaml.safe_dump(data, allow_unicode=True, sort_keys=False),
                   encoding="utf-8")
    os.replace(tmp, path)


def _key(name: str, field: str) -> str:
    """The keyring entry of a connector's secret: a workspace's connectors have their own (two
    workspaces can use different credentials under the same name)."""
    from .. import workspaces
    ws = workspaces.current()
    if not ws.studio and location(name) == "workspace":
        return f"ws:{ws.name}/{name}/{field}"
    return f"{name}/{field}"


def _keyring():  # type: ignore[no-untyped-def]
    import keyring  # late import: only when there are secrets in the keyring

    return keyring


def get_secret(name: str, conn: Connection, field: str) -> str | None:
    ref = conn.secrets.get(field)
    if ref is None:
        return None
    if ref == "keyring":
        return _keyring().get_password(SERVICE, _key(name, field))
    return os.getenv(ref[2:-1])


def set_secret(name: str, field: str, value: str) -> None:
    _keyring().set_password(SERVICE, _key(name, field), value)


def delete_secrets(name: str, conn: Connection) -> None:
    for field, ref in conn.secrets.items():
        if ref == "keyring":
            with contextlib.suppress(Exception):  # already gone
                _keyring().delete_password(SERVICE, _key(name, field))
    delete_oauth_tokens(name)


def secret_status(name: str, conn: Connection) -> dict[str, Any]:
    """Says whether the required secret is set, without ever returning it."""
    field = conn.secret_field
    optional = conn.auth.method in OPTIONAL_SECRET
    if field is None:
        return {"field": None, "source": None, "set": True, "optional": False}
    ref = conn.secrets.get(field)
    present = bool(get_secret(name, conn, field)) if ref else False
    return {"field": field, "source": ref, "set": present, "optional": optional}


# --- sign-in on a page: tokens (JSON) only in the keyring, never in connections.yaml ------------
def get_oauth_tokens(name: str) -> dict[str, Any] | None:
    raw = _keyring().get_password(SERVICE, _key(name, OAUTH_TOKEN_FIELD))
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def set_oauth_tokens(name: str, tokens: dict[str, Any]) -> None:
    _keyring().set_password(SERVICE, _key(name, OAUTH_TOKEN_FIELD), json.dumps(tokens))


def delete_oauth_tokens(name: str) -> None:
    with contextlib.suppress(Exception):  # not signed in, or no keyring backend
        _keyring().delete_password(SERVICE, _key(name, OAUTH_TOKEN_FIELD))


def oauth_status(name: str, conn: Connection) -> dict[str, Any] | None:
    """Sign-in state for the editor, without the tokens. None when the method is not OAuth."""
    if conn.auth.method != "oauth_browser":
        return None
    t = get_oauth_tokens(name) or {}
    return {"signed_in": bool(t.get("access_token")), "expires_at": t.get("expires_at"),
            "refreshable": bool(t.get("refresh_token"))}

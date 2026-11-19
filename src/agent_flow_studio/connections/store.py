"""Leitura/escrita de connections.yaml e dos segredos (keyring ou variáveis de ambiente)."""
from __future__ import annotations

import contextlib
import os
from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError

from .model import SLUG, Connection

SERVICE = "agent-flow-studio"  # nome do serviço no Gestor de Credenciais


class ConnectorConfigError(ValueError):
    pass


def connections_file() -> Path:
    return Path(os.getenv("CONNECTIONS_FILE", "connections.yaml"))


def load(path: Path | None = None) -> dict[str, Connection]:
    path = path or connections_file()
    if not path.is_file():
        return {}
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    out: dict[str, Connection] = {}
    for name, data in (raw.get("connections") or {}).items():
        if not SLUG.fullmatch(str(name)):
            raise ConnectorConfigError(f"conector {name!r}: nome inválido")
        try:
            out[name] = Connection.model_validate(data)
        except ValidationError as e:
            raise ConnectorConfigError(f"conector {name!r}: {e}") from e
    return out


def save(conns: dict[str, Connection], path: Path | None = None) -> None:
    path = path or connections_file()
    data = {"version": 1, "connections": {
        n: {"type": c.type, **c.model_dump(mode="json", exclude_defaults=True)}
        for n, c in sorted(conns.items())}}
    text = ("# Conectores desta máquina. Sem segredos: só referências ${VAR} ou 'keyring'.\n"
            + yaml.safe_dump(data, allow_unicode=True, sort_keys=False))
    tmp = path.with_suffix(".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def _keyring():  # type: ignore[no-untyped-def]
    import keyring  # import tardio: só quando há segredos no keyring

    return keyring


def get_secret(name: str, conn: Connection, field: str) -> str | None:
    ref = conn.secrets.get(field)
    if ref is None:
        return None
    if ref == "keyring":
        return _keyring().get_password(SERVICE, f"{name}/{field}")
    return os.getenv(ref[2:-1])


def set_secret(name: str, field: str, value: str) -> None:
    _keyring().set_password(SERVICE, f"{name}/{field}", value)


def delete_secrets(name: str, conn: Connection) -> None:
    for field, ref in conn.secrets.items():
        if ref == "keyring":
            with contextlib.suppress(Exception):  # já não existe
                _keyring().delete_password(SERVICE, f"{name}/{field}")


def secret_status(name: str, conn: Connection) -> dict[str, Any]:
    """Diz se o segredo necessário está definido, sem nunca o devolver."""
    field = conn.secret_field
    if field is None:
        return {"field": None, "source": None, "set": True}
    ref = conn.secrets.get(field)
    present = bool(get_secret(name, conn, field)) if ref else False
    return {"field": field, "source": ref, "set": present}

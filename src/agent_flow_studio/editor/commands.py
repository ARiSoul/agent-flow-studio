"""Saved assistant commands: a message the user reuses, with instructions sent along with it.

Stored in `assistant_commands.yaml` (or `AFS_COMMANDS_FILE`), outside git: instructions may hold
internal details. Only the user creates, edits and deletes them (from the editor); the assistant
only receives the instructions of the command used with a message.
"""
from __future__ import annotations

import os
import re
import secrets
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

COMMAND_ID = re.compile(r"^[a-z0-9]{6,32}$")
MAX_COMMANDS = 200


class CommandError(ValueError):
    pass


class Command(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str = Field(pattern=COMMAND_ID.pattern)
    name: str = Field(min_length=1, max_length=60)
    text: str = Field(min_length=1, max_length=4_000)  # the message put in the chat
    instructions: str = Field(default="", max_length=20_000)  # sent to the assistant with it
    favorite: bool = False

    @field_validator("name")
    @classmethod
    def _one_line(cls, v: str) -> str:
        v = v.strip()
        if not v or any(ord(c) < 32 for c in v):
            raise ValueError("name: one line of text")
        return v


def commands_file() -> Path:
    return Path(os.getenv("AFS_COMMANDS_FILE", "assistant_commands.yaml"))


def new_id() -> str:
    return secrets.token_hex(6)


def load() -> list[Command]:
    f = commands_file()
    if not f.is_file():
        return []
    try:
        raw = yaml.safe_load(f.read_text(encoding="utf-8")) or {}
        return [Command.model_validate(c) for c in (raw.get("commands") or [])]
    except (OSError, yaml.YAMLError, ValidationError, AttributeError) as e:
        raise CommandError(f"{f}: invalid commands file: {e}") from e


def _write(cmds: list[Command]) -> None:
    f = commands_file()
    f.parent.mkdir(parents=True, exist_ok=True)
    body = {"commands": [c.model_dump(exclude_defaults=False) for c in cmds]}
    text = ("# Saved assistant commands (editor). Local file, outside git.\n"
            + yaml.safe_dump(body, allow_unicode=True, sort_keys=False, width=100))
    tmp = f.with_suffix(f.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8", newline="\n")
    os.replace(tmp, f)


def save(data: dict[str, Any]) -> Command:
    """Creates (no id, or an unknown one) or replaces a command; returns it."""
    try:
        c = Command.model_validate({"id": data.get("id") or new_id(), **{
            k: v for k, v in data.items() if k != "id"}})
    except ValidationError as e:
        raise CommandError(str(e)) from e
    cmds = load()
    i = next((k for k, x in enumerate(cmds) if x.id == c.id), None)
    if i is None:
        if len(cmds) >= MAX_COMMANDS:
            raise CommandError(f"at most {MAX_COMMANDS} commands")
        cmds.append(c)
    else:
        cmds[i] = c
    _write(cmds)
    return c


def delete(command_id: str) -> None:
    cmds = load()
    left = [c for c in cmds if c.id != command_id]
    if len(left) == len(cmds):
        raise CommandError(f"command {command_id!r} does not exist")
    _write(left)


def get(command_id: str) -> Command | None:
    return next((c for c in load() if c.id == command_id), None)

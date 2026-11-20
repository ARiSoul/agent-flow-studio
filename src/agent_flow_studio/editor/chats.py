"""History of the assistant conversations, per flow: `runs/chats/<flow>/<id>.json` (outside git).

The editor owns the content (the items it shows: messages, tools used, proposals) and saves the
whole conversation after each reply, with the model session id so it can be resumed. The server
only checks names, shape and size. Conversations hold what the user wrote: they stay local.
"""
from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from ..config import settings

CHAT_ID = re.compile(r"^[a-z0-9]{8,32}$")
FLOW = re.compile(r"^[a-z0-9][a-z0-9_-]{0,39}$")
MAX_BYTES = 2_000_000
MAX_ITEMS = 2_000


class ChatError(ValueError):
    pass


class Item(BaseModel):
    """One line of the conversation as the editor shows it (other fields are kept as they are)."""
    model_config = ConfigDict(extra="allow")
    kind: Literal["user", "assistant", "tool", "info", "error", "library"]


class Chat(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str = Field(pattern=CHAT_ID.pattern)
    title: str = Field(default="", max_length=120)
    session_id: str | None = Field(default=None, pattern=r"^[A-Za-z0-9-]{1,80}$")
    cost: float = Field(default=0, ge=0)
    created: float = 0
    updated: float = 0
    items: list[Item] = Field(default_factory=list, max_length=MAX_ITEMS)


def chats_dir() -> Path:
    return settings.runs_dir / "chats"


def _path(flow: str, chat_id: str) -> Path:
    if not FLOW.fullmatch(flow):
        raise ChatError("invalid flow name")
    if not CHAT_ID.fullmatch(chat_id):
        raise ChatError("invalid conversation id")
    return chats_dir() / flow / f"{chat_id}.json"


def _summary(c: dict[str, Any]) -> dict[str, Any]:
    items = c.get("items") or []
    return {"id": c["id"], "title": c.get("title") or "", "updated": c.get("updated", 0),
            "created": c.get("created", 0), "messages": sum(1 for i in items if i.get("kind") == "user")}


def list_chats(flow: str) -> list[dict[str, Any]]:
    """Summaries of the flow's conversations, the most recent first."""
    if not FLOW.fullmatch(flow):
        raise ChatError("invalid flow name")
    d = chats_dir() / flow
    out = []
    for f in d.glob("*.json") if d.is_dir() else []:
        if not CHAT_ID.fullmatch(f.stem):
            continue
        try:
            out.append(_summary(json.loads(f.read_text(encoding="utf-8"))))
        except (OSError, ValueError, KeyError):
            continue  # a damaged file does not hide the others
    return sorted(out, key=lambda c: -c["updated"])


def get(flow: str, chat_id: str) -> dict[str, Any]:
    p = _path(flow, chat_id)
    if not p.is_file():
        raise FileNotFoundError(chat_id)
    try:
        return Chat.model_validate_json(p.read_text(encoding="utf-8")).model_dump()
    except ValidationError as e:
        raise ChatError(f"damaged conversation file: {e}") from e


def save(flow: str, chat_id: str, data: dict[str, Any]) -> dict[str, Any]:
    p = _path(flow, chat_id)
    now = time.time()
    old: dict[str, Any] = {}
    if p.is_file():
        try:
            old = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            old = {}
    # a rename alone does not move the conversation to the top of the history
    same = bool(old) and old.get("items") == data.get("items")
    try:
        c = Chat.model_validate(data | {
            "id": chat_id, "updated": old.get("updated", now) if same else now,
            "created": old.get("created") or data.get("created") or now})
    except ValidationError as e:
        raise ChatError(str(e)) from e
    text = c.model_dump_json()
    if len(text.encode("utf-8")) > MAX_BYTES:
        raise ChatError("conversation too long to save (start a new one)")
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, p)
    return _summary(c.model_dump())


def delete(flow: str, chat_id: str) -> None:
    p = _path(flow, chat_id)
    if not p.is_file():
        raise FileNotFoundError(chat_id)
    p.unlink()

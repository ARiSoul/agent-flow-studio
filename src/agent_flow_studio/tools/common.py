import json
from typing import Any


def ok(data: Any) -> dict:
    text = data if isinstance(data, str) else json.dumps(data, ensure_ascii=False, indent=2)
    return {"content": [{"type": "text", "text": text}]}


def err(message: str) -> dict:
    return {"content": [{"type": "text", "text": f"ERROR: {message}"}], "is_error": True}


def not_implemented(what: str) -> dict:
    return err(f"{what} is not implemented yet. See the TODO in CLAUDE.md (or set AFS_MOCK=1).")

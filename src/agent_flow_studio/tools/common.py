import json
from typing import Any


def ok(data: Any) -> dict:
    text = data if isinstance(data, str) else json.dumps(data, ensure_ascii=False, indent=2)
    return {"content": [{"type": "text", "text": text}]}


def err(message: str) -> dict:
    return {"content": [{"type": "text", "text": f"ERRO: {message}"}], "is_error": True}


def not_implemented(what: str) -> dict:
    return err(f"{what} ainda não implementado. Ver TODO em CLAUDE.md (ou ative AFS_MOCK=1).")

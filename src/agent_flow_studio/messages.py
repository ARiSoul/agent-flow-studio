"""Messages that reach the editor (validation, run form…): English text plus a code and parameters.

A `Msg` IS the English string (so logs, tests and the model keep seeing plain text), and it also
carries `code` + `params`, so the editor can show it in the UI language with its own tables
(`editor/src/i18n/server.<lang>.ts`; English needs no table: the editor shows the server text).
`tests/test_messages.py` checks that every `msg("code", "template", …)` in the code has a
translation with the same placeholders.

Rules: the template is ONE string literal right after the code; parameters are passed already
formatted (`repr()` for names), and a parameter can itself be a `Msg` (a nested reason).
"""
from __future__ import annotations

from typing import Any


class Msg(str):
    code: str
    template: str
    params: dict[str, Any]

    def __new__(cls, code: str, template: str, **params: Any) -> Msg:  # noqa: PYI034 — Python 3.10
        obj = super().__new__(cls, template.format(**{k: str(v) for k, v in params.items()}))
        obj.code, obj.template, obj.params = code, template, params
        return obj

    def to_json(self) -> dict[str, Any]:
        return {"code": self.code,
                "params": {k: v.to_json() if isinstance(v, Msg) else str(v)
                           for k, v in self.params.items()}}


def msg(code: str, template: str, **params: Any) -> Msg:
    return Msg(code, template, **params)


class MsgError(ValueError):
    """A ValueError that carries one or more `Msg` (pydantic keeps it in the error's context)."""

    def __init__(self, *msgs: str):
        self.msgs = list(msgs)
        super().__init__("; ".join(str(m) for m in msgs))


def reason(e: BaseException) -> str:
    """The message of an error as a Msg when it has one (to nest it in another message)."""
    if isinstance(e, MsgError) and len(e.msgs) == 1:
        return e.msgs[0]
    if e.args and isinstance(e.args[0], Msg) and len(e.args) == 1:
        return e.args[0]
    return str(e)


def to_json(m: Any) -> dict[str, Any] | None:
    """{code, params} of a message, or None for plain text."""
    return m.to_json() if isinstance(m, Msg) else None

"""Named connector actions: the agent gives an action and its parameters; the studio checks the
values and builds the request from the action's templates (it never takes a path from the model).

`{param}` in the path is URL-encoded; in query values and body strings it is replaced as text; a
body string that is exactly "{param}" takes the typed value (number, boolean). The connector's
allowed paths still apply to the final path. Website connectors run their steps in `web.py`.
"""
from __future__ import annotations

import datetime as dt
import re
from typing import Any
from urllib.parse import quote

from . import client, store
from .model import _PLACEHOLDER, Action, ActionParam, Connection


class ActionError(ValueError):
    pass


def _value(name: str, p: ActionParam, v: Any) -> Any:
    if p.type == "boolean":
        if isinstance(v, bool):
            return v
        if str(v).lower() in ("true", "false"):
            return str(v).lower() == "true"
        raise ActionError(f"{name}: expected true or false")
    if p.type in ("integer", "number"):
        kind = "an integer" if p.type == "integer" else "a number"
        if isinstance(v, bool):
            raise ActionError(f"{name}: expected {kind}")
        try:
            f = float(v)
        except (TypeError, ValueError):
            raise ActionError(f"{name}: expected {kind}") from None
        if p.type == "number":
            return f
        if f != int(f):
            raise ActionError(f"{name}: expected {kind}")
        return int(f)
    s = str(v)
    if len(s) > p.max_length:
        raise ActionError(f"{name}: at most {p.max_length} characters")
    if any(ord(c) < 32 for c in s):
        raise ActionError(f"{name}: control characters are not allowed")
    if p.type == "choice" and s not in p.options:
        raise ActionError(f"{name}: one of {', '.join(p.options)}")
    if p.type == "date":
        try:
            dt.date.fromisoformat(s)
        except ValueError:
            raise ActionError(f"{name}: expected a date YYYY-MM-DD") from None
    if p.pattern and not re.fullmatch(p.pattern, s):
        raise ActionError(f"{name}: does not match the expected format ({p.pattern})")
    return s


def check_params(action: Action, given: dict[str, Any] | None) -> dict[str, Any]:
    given = dict(given or {})
    unknown = set(given) - set(action.params)
    if unknown:
        raise ActionError(f"unknown parameter(s): {', '.join(sorted(unknown))}")
    out: dict[str, Any] = {}
    for n, p in action.params.items():
        v = given.get(n)
        if v is None or v == "":
            if p.required:
                raise ActionError(f"{n}: required")
            out[n] = ""
            continue
        out[n] = _value(n, p, v)
    return out


def fill(text: str, values: dict[str, Any], encode: bool = False) -> str:
    def rep(m: re.Match[str]) -> str:
        v = values[m.group(1)]
        s = str(v).lower() if isinstance(v, bool) else str(v)
        return quote(s, safe="") if encode else s
    return _PLACEHOLDER.sub(rep, text)


def _fill_body(node: Any, values: dict[str, Any]) -> Any:
    if isinstance(node, str):
        m = _PLACEHOLDER.fullmatch(node)
        return values[m.group(1)] if m else fill(node, values)
    if isinstance(node, list):
        return [_fill_body(x, values) for x in node]
    if isinstance(node, dict):
        return {k: _fill_body(v, values) for k, v in node.items()}
    return node


# phone numbers: 9–15 digits, optionally +country, groups split by a space or a dot (not by "-"
# or ":", so dates and times are left alone); emails: the usual address shape
_PHONE = re.compile(r"(?<![\w+])(\+\d{1,3}[ .]?)?\d(?:[ .]?\d){8,14}(?![\w])")
_EMAIL = re.compile(r"[\w.+-]+@[\w-]+(\.[\w-]+)+")


def redact(value: Any, kinds: list[str]) -> Any:
    """Replaces phone numbers / emails in every string of a returned value."""
    if not kinds:
        return value
    if isinstance(value, str):
        if "email" in kinds:
            value = _EMAIL.sub("[email]", value)
        if "phone" in kinds:
            value = _PHONE.sub("[phone]", value)
        return value
    if isinstance(value, list):
        return [redact(x, kinds) for x in value]
    if isinstance(value, dict):
        return {k: redact(v, kinds) for k, v in value.items()}
    return value


def get_action(conn_name: str, conn: Connection, action: str) -> Action:
    a = conn.actions.get(action)
    if a is None:
        names = ", ".join(conn.actions) or "none"
        raise ActionError(f"connector {conn_name!r} has no action {action!r}; actions: {names}")
    return a


async def run(name: str, action: str, params: dict[str, Any] | None = None,
              conns: dict[str, Connection] | None = None, *, visible: bool = False
              ) -> dict[str, Any]:
    """Runs a named action; returns the response (HTTP) or the extracted values (website).
    `visible`: a website action in a window the person can watch (a test)."""
    conns = store.load() if conns is None else conns
    conn = conns.get(name)
    if conn is None:
        raise ActionError(f"connector {name!r} is not configured")
    a = get_action(name, conn, action)
    values = check_params(a, params)
    if conn.type == "website":
        from . import web  # optional dependency (Playwright), only for website connectors
        r = await web.run_action(name, conn, action, a, values, visible=visible)
        return {**r, "values": redact(r.get("values"), a.redact)}
    path = fill(a.path, values, encode=True)
    query = {k: fill(v, values) for k, v in a.query.items()}
    query = {k: v for k, v in query.items() if v != ""}  # optional params left empty
    if a.access == "read":
        r = await client.get(name, path, query, conns=conns)
    else:
        r = await client.write(name, a.method, path, _fill_body(a.body, values), query, conns=conns)
    return {"action": action, "request": f"{a.method} {path}", **r,
            "body": redact(r.get("body"), a.redact)}


def describe_action(name: str, a: Action) -> str:
    """One line for the agent's prompt: name(params) — description [write]."""
    ps = []
    for n, p in a.params.items():
        kind = f"one of {'/'.join(p.options)}" if p.type == "choice" else p.type
        ps.append(f"{n}: {kind}{'' if p.required else ', optional'}"
                  + (f" — {p.description}" if p.description else ""))
    tag = " **[write: follows the autonomy/approval]**" if a.access == "write" else ""
    line = f"- `{name}`({'; '.join(ps)}) — {a.description}{tag}"
    if a.instructions.strip():
        line += "\n  " + a.instructions.strip().replace("\n", "\n  ")
    return line

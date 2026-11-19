"""Typed values for flow inputs and agent inputs/outputs.

`IOType` describes a value (text, number, date, url, path, file, choice, json…); `check_value`
validates and normalizes one value against it. The studio applies it to the flow inputs before a
run and to every output an agent saves (`io.set_output`), so the next agent gets clean data.
"""
from __future__ import annotations

import json
import re
from datetime import date, datetime
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

IOKind = Literal["text", "markdown", "number", "integer", "boolean", "date", "datetime", "url",
                 "path", "file", "choice", "json"]
KINDS: tuple[str, ...] = IOKind.__args__  # type: ignore[attr-defined]
MAX_TEXT = 200_000
MAX_ITEMS = 200


class IOType(BaseModel):
    """A typed value. min/max: value (number, integer) or length (text, markdown)."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)
    type: IOKind = "text"
    label: str = ""
    description: str = ""
    required: bool = True
    default: Any = None
    multiple: bool = False  # a list of values of this type
    min: float | None = None
    max: float | None = None
    pattern: str | None = None  # text: full match
    options: list[str] = Field(default_factory=list)  # choice
    accept: list[str] = Field(default_factory=list)  # file: extensions such as ".md"
    contract: str | None = None  # json: a contract from the catalog
    draft: bool = False  # markdown: a draft for human approval

    @model_validator(mode="after")
    def _check(self) -> IOType:
        check_type(self)
        return self


def check_type(t: IOType, contracts: dict[str, Any] | None = None) -> None:
    """Rules of the type itself (the editor and the flow validation show these errors)."""
    if t.type == "choice" and not t.options:
        raise ValueError("choice needs 'options'")
    if t.options and t.type != "choice":
        raise ValueError("'options' is only for choice")
    if t.accept and t.type != "file":
        raise ValueError("'accept' is only for file")
    for ext in t.accept:
        if not re.fullmatch(r"\.[A-Za-z0-9]{1,10}", ext):
            raise ValueError(f"accept: {ext!r} must look like .md")
    if t.contract and t.type != "json":
        raise ValueError("'contract' is only for json")
    if t.draft and t.type != "markdown":
        raise ValueError("'draft' is only for markdown")
    if t.pattern is not None:
        if t.type not in ("text", "markdown"):
            raise ValueError("'pattern' is only for text")
        try:
            re.compile(t.pattern)
        except re.error as e:
            raise ValueError(f"invalid pattern: {e}") from e
    if (t.min is not None or t.max is not None) and t.type not in ("number", "integer", "text",
                                                                   "markdown"):
        raise ValueError("'min'/'max' are only for number, integer and text")
    if t.min is not None and t.max is not None and t.min > t.max:
        raise ValueError("'min' is greater than 'max'")
    if contracts is not None and t.contract and t.contract not in contracts:
        raise ValueError(f"unknown contract {t.contract!r}")
    if t.default is not None:
        try:
            check_value(t, t.default, contracts=contracts)
        except ValueError as e:
            raise ValueError(f"invalid default: {e}") from e


def compatible(source: str, target: str) -> bool:
    """Can a value of type `source` feed an input of type `target`?"""
    if source == target or target in ("text", "markdown", "json"):
        return True
    return (source, target) in {("integer", "number"), ("choice", "text"), ("url", "text"),
                                ("markdown", "text")}


def _one(t: IOType, v: Any, roots: list[Path] | None, run_dir: Path | None,
         contracts: dict[str, Any] | None) -> Any:
    k = t.type
    if k in ("text", "markdown"):
        if not isinstance(v, str):
            raise ValueError("must be text")
        if len(v) > MAX_TEXT:
            raise ValueError(f"longer than {MAX_TEXT} characters")
        if t.min is not None and len(v) < t.min:
            raise ValueError(f"shorter than {int(t.min)} characters")
        if t.max is not None and len(v) > t.max:
            raise ValueError(f"longer than {int(t.max)} characters")
        if t.pattern and not re.fullmatch(t.pattern, v):
            raise ValueError(f"does not match the pattern {t.pattern}")
        return v
    if k in ("number", "integer"):
        if isinstance(v, bool):
            raise ValueError("must be a number")
        if isinstance(v, str):
            try:
                v = float(v.strip().replace(",", "."))
            except ValueError:
                raise ValueError("must be a number") from None
        if not isinstance(v, (int, float)):
            raise ValueError("must be a number")
        if k == "integer":
            if float(v) != int(v):
                raise ValueError("must be a whole number")
            v = int(v)
        if t.min is not None and v < t.min:
            raise ValueError(f"less than {t.min:g}")
        if t.max is not None and v > t.max:
            raise ValueError(f"greater than {t.max:g}")
        return v
    if k == "boolean":
        if isinstance(v, bool):
            return v
        if isinstance(v, str) and v.strip().lower() in ("true", "false", "yes", "no", "1", "0"):
            return v.strip().lower() in ("true", "yes", "1")
        raise ValueError("must be true or false")
    if k == "date":
        try:
            return date.fromisoformat(str(v).strip()[:10]).isoformat()
        except ValueError:
            raise ValueError("must be a date (YYYY-MM-DD)") from None
    if k == "datetime":
        try:
            return datetime.fromisoformat(str(v).strip().replace("Z", "+00:00")).isoformat()
        except ValueError:
            raise ValueError("must be a date and time (ISO 8601)") from None
    if k == "url":
        s = str(v).strip()
        parts = urlsplit(s)
        if parts.scheme not in ("http", "https") or not parts.netloc or len(s) > 2000:
            raise ValueError("must be an http(s) URL")
        return s
    if k == "path":
        s = str(v).strip()
        if not s or "\x00" in s or len(s) > 1000:
            raise ValueError("must be a path")
        if roots is not None:
            p = Path(s)
            if not p.is_absolute():
                p = roots[0] / p
            p = p.resolve()
            if not any(p == r.resolve() or r.resolve() in p.parents for r in roots):
                raise ValueError("path outside the workspace and the run folder")
            return str(p)
        return s
    if k == "file":
        if not isinstance(v, dict) or not isinstance(v.get("path"), str):
            raise ValueError("must be a file reference {name, path}")
        name = str(v.get("name") or Path(v["path"]).name)
        if t.accept and Path(name).suffix.lower() not in {a.lower() for a in t.accept}:
            raise ValueError(f"file type not accepted ({', '.join(t.accept)})")
        if run_dir is not None:
            p = (run_dir / v["path"]).resolve()
            if run_dir.resolve() not in p.parents or not p.is_file():
                raise ValueError(f"file not found in the run folder: {v['path']}")
            return {"name": name, "path": v["path"], "size": p.stat().st_size}
        return {"name": name, "path": v["path"]}
    if k == "choice":
        if str(v) not in t.options:
            raise ValueError(f"must be one of: {', '.join(t.options)}")
        return str(v)
    if k == "json":
        if isinstance(v, str):
            try:
                v = json.loads(v)
            except ValueError:
                raise ValueError("must be valid JSON") from None
        if t.contract and contracts is not None:  # None: only the type rules (no catalog)
            adapter = contracts.get(t.contract)
            if adapter is None:
                raise ValueError(f"unknown contract {t.contract!r}")
            try:
                return adapter.dump_python(adapter.validate_python(v), mode="json")
            except ValidationError as e:
                raise ValueError(f"does not meet the contract {t.contract}: "
                                 f"{e.errors()[0]['msg']}") from None
        if len(json.dumps(v)) > MAX_TEXT:
            raise ValueError("JSON too large")
        return v
    raise ValueError(f"unknown type {k!r}")


def check_value(t: IOType, value: Any, *, roots: list[Path] | None = None,
                run_dir: Path | None = None, contracts: dict[str, Any] | None = None) -> Any:
    """The normalized value, or ValueError with a short reason. None when empty and optional.
    roots: folders a path must stay in (None = any). run_dir: where file references live."""
    empty = value is None or value == "" or value == []
    if empty:
        if t.default is not None and value is None:
            value = t.default
        elif t.required:
            raise ValueError("required")
        else:
            return [] if t.multiple else None
    if t.multiple:
        if not isinstance(value, list):
            value = [value]
        if len(value) > MAX_ITEMS:
            raise ValueError(f"more than {MAX_ITEMS} items")
        out = []
        for i, item in enumerate(value):
            try:
                out.append(_one(t, item, roots, run_dir, contracts))
            except ValueError as e:
                raise ValueError(f"item {i + 1}: {e}") from None
        return out
    if isinstance(value, list):
        raise ValueError("one value expected, not a list")  # noqa: TRY004 — callers catch ValueError
    return _one(t, value, roots, run_dir, contracts)

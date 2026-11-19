"""Flow templates: ready-made flows with parameters (workspace, branch, connector…).

A template is a folder with `template.yaml` (title, description, parameters), `flow.yaml` and
`prompts/*.md`. `{{param}}` placeholders are filled in when a flow is created: in `flow.yaml` only
inside string values (the YAML is parsed first, so a value can never change its structure) and in
the prompts as plain text. Core templates live in `agent_flow_studio/templates/`; an extension can
add its own in `extensions/<name>/templates/` (outside git).
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from ..extensions import LOADED
from .spec import FlowSpec

CORE_DIR = Path(__file__).resolve().parent.parent / "templates"
TEMPLATE_ID = re.compile(r"^[a-z][a-z0-9-]{0,39}$")
PLACEHOLDER = re.compile(r"\{\{\s*([a-z_][a-z0-9_]*)\s*\}\}")
_BRANCH = re.compile(r"^(?!-)(?!.*\.\.)[A-Za-z0-9._/-]{1,100}$")
_SLUG = re.compile(r"^[a-z][a-z0-9_]{0,39}$")
_LINE = re.compile(r"^[^\x00-\x1f]{1,300}$")


class TemplateError(ValueError):
    pass


class Param(BaseModel):
    model_config = ConfigDict(extra="forbid")
    label: str
    kind: Literal["path", "branch", "connector", "text"] = "text"
    default: str | None = None
    required: bool = True
    hint: str = ""
    connector_types: list[str] = Field(default_factory=list)  # kind connector: accepted types
    connector_access: Literal["read", "write"] | None = None  # kind connector: needed access


class TemplateMeta(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: str
    description: str = ""
    params: dict[str, Param] = Field(default_factory=dict)


@dataclass(frozen=True)
class Template:
    id: str
    meta: TemplateMeta
    folder: Path
    source: str  # "core" or the extension name


def template_dirs() -> list[tuple[str, Path]]:
    return [("core", CORE_DIR), *LOADED.template_dirs]


def list_templates() -> dict[str, Template]:
    out: dict[str, Template] = {}
    for source, base in template_dirs():
        if not base.is_dir():
            continue
        for d in sorted(p for p in base.iterdir() if (p / "template.yaml").is_file()):
            if not TEMPLATE_ID.fullmatch(d.name):
                raise TemplateError(f"template folder {d.name!r}: invalid name")
            if d.name in out:
                raise TemplateError(f"template {d.name!r} exists twice ({out[d.name].source}, "
                                    f"{source})")
            try:
                meta = TemplateMeta.model_validate(
                    yaml.safe_load((d / "template.yaml").read_text(encoding="utf-8")))
            except (yaml.YAMLError, ValidationError) as e:
                raise TemplateError(f"template {d.name!r}: {e}") from e
            out[d.name] = Template(d.name, meta, d, source)
    return out


def get_template(template_id: str) -> Template:
    t = list_templates().get(template_id)
    if t is None:
        raise TemplateError(f"template {template_id!r} does not exist")
    return t


def check_params(t: Template, given: dict[str, str]) -> dict[str, str]:
    """Defaults + checks per kind. Returns the values to fill in."""
    unknown = set(given) - set(t.meta.params)
    if unknown:
        raise TemplateError(f"unknown parameter(s): {', '.join(sorted(unknown))}")
    out: dict[str, str] = {}
    for key, p in t.meta.params.items():
        v = (given.get(key) or p.default or "").strip()
        if not v:
            if p.required:
                raise TemplateError(f"{p.label}: required")
            out[key] = ""
            continue
        if not _LINE.fullmatch(v):
            raise TemplateError(f"{p.label}: one line, up to 300 characters")
        if p.kind == "path" and not Path(v).expanduser().is_dir():
            raise TemplateError(f"{p.label}: folder not found: {v}")
        if p.kind == "branch" and not _BRANCH.fullmatch(v):
            raise TemplateError(f"{p.label}: invalid branch name {v!r}")
        if p.kind == "connector" and not _SLUG.fullmatch(v):
            raise TemplateError(f"{p.label}: invalid connector name {v!r}")
        out[key] = v
    return out


def _fill(text: str, values: dict[str, str], where: str) -> str:
    def sub(m: re.Match[str]) -> str:
        if m.group(1) not in values:
            raise TemplateError(f"{where}: unknown placeholder {{{{{m.group(1)}}}}}")
        return values[m.group(1)]

    return PLACEHOLDER.sub(sub, text)


def _fill_tree(node: Any, values: dict[str, str], where: str) -> Any:
    if isinstance(node, str):
        return _fill(node, values, where)
    if isinstance(node, list):
        return [_fill_tree(x, values, where) for x in node]
    if isinstance(node, dict):
        return {k: _fill_tree(v, values, where) for k, v in node.items()}
    return node


def render(template_id: str, name: str, title: str, params: dict[str, str]
           ) -> tuple[FlowSpec, dict[str, str]]:
    """The new flow (validated) and its prompt files (`prompts/<x>.md` → text)."""
    t = get_template(template_id)
    values = check_params(t, params)
    raw = yaml.safe_load((t.folder / "flow.yaml").read_text(encoding="utf-8"))
    raw = _fill_tree(raw, values, "flow.yaml") | {"name": name, "title": title}
    prompts = {f"prompts/{f.name}": _fill(f.read_text(encoding="utf-8"), values, f"prompts/{f.name}")
               for f in sorted((t.folder / "prompts").glob("*.md"))}
    try:
        spec = FlowSpec.model_validate(raw)
    except ValidationError as e:
        raise TemplateError(f"template {template_id!r} gives an invalid flow: {e}") from e
    return spec, prompts


def templates_dict() -> list[dict[str, Any]]:
    """For the editor and the CLI."""
    return [{"id": t.id, "source": t.source, **t.meta.model_dump(),
             "description": " ".join(t.meta.description.split())}  # one paragraph
            for t in list_templates().values()]

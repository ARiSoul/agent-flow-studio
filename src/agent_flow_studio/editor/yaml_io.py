"""Readable flow.yaml output: keys in format order, short lists/dicts inline."""
from __future__ import annotations

from typing import Any

import yaml

from ..builder.spec import FlowSpec

_HEADER = (
    "# Flow: {title}\n"
    "# Edit by hand or in the visual editor. Check with: agent-flow-studio-flows validate {name}\n"
)


class _Dumper(yaml.SafeDumper):
    pass


def _scalar(v: Any) -> bool:
    return v is None or isinstance(v, (str, int, float, bool))


def _short(v: Any) -> bool:
    return not isinstance(v, str) or (len(v) <= 70 and "\n" not in v)


def _repr_dict(d: _Dumper, data: dict) -> yaml.Node:
    flow = (0 < len(data) <= 4 and not {"description", "prompt", "prompt_file"} & set(data)
            and all(_scalar(v) and _short(v) for v in data.values()))
    return d.represent_mapping("tag:yaml.org,2002:map", data.items(), flow_style=flow)


def _repr_list(d: _Dumper, data: list) -> yaml.Node:
    flow = all(_scalar(v) and _short(v) for v in data)
    return d.represent_sequence("tag:yaml.org,2002:seq", data, flow_style=flow)


def _repr_str(d: _Dumper, s: str) -> yaml.Node:
    if "\n" in s:
        return d.represent_scalar("tag:yaml.org,2002:str", s, style="|")
    return d.represent_scalar("tag:yaml.org,2002:str", s)


class _Block(dict):
    """Top-level mapping: always in block style."""


_Dumper.add_representer(_Block, lambda d, data: d.represent_mapping(
    "tag:yaml.org,2002:map", data.items(), flow_style=False))
_Dumper.add_representer(dict, _repr_dict)
_Dumper.add_representer(list, _repr_list)
_Dumper.add_representer(str, _repr_str)


def spec_to_data(spec: FlowSpec) -> dict[str, Any]:
    """Minimal form (no default values), with `version` always present."""
    data = spec.model_dump(mode="json", by_alias=True, exclude_defaults=True)
    data = {"version": spec.version, **data}
    if "layout" in data:
        data["layout"] = {k: {"x": round(p["x"]), "y": round(p["y"])}
                          for k, p in data["layout"].items()}
    return data


def dump_flow(spec: FlowSpec) -> str:
    return _dump(spec_to_data(spec), spec.title, spec.name)


def dump_raw(raw: dict[str, Any], name: str) -> str:
    """A flow that does not validate yet (saved "with problems"): written as it is, in the same
    readable layout, so the editor can open it again and finish it."""
    data = {"version": raw.get("version", 1), **{k: v for k, v in raw.items() if k != "version"}}
    layout = data.get("layout")

    def _pos(p: Any) -> Any:
        ok = isinstance(p, dict) and all(isinstance(p.get(c), (int, float)) for c in ("x", "y"))
        return {"x": round(p["x"]), "y": round(p["y"])} if ok else p

    if isinstance(layout, dict):
        data["layout"] = {k: _pos(p) for k, p in layout.items()}
    return _dump(data, str(raw.get("title") or name), name)


def _dump(data: dict[str, Any], title: str, name: str) -> str:
    parts = [_HEADER.format(title=title.replace("\n", " "), name=name)]
    # top-level blocks separated by a blank line, for reading and diffs
    for key, value in data.items():
        chunk = yaml.dump(_Block({key: value}), Dumper=_Dumper, allow_unicode=True, sort_keys=False,
                          width=100)
        if key in ("start", "agents", "gates", "edges", "mcp_servers", "layout"):
            parts.append("\n")
        parts.append(chunk)
    return "".join(parts)

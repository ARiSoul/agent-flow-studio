"""Escrita de flow.yaml legível: chaves pela ordem do formato, listas/dicts curtos em linha."""
from __future__ import annotations

from typing import Any

import yaml

from ..builder.spec import FlowSpec

_HEADER = (
    "# Flow: {title}\n"
    "# Editável à mão ou no editor visual. Validar com:  agent-flow-studio-flows validate {name}\n"
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
    """Mapa de topo: sempre em bloco."""


_Dumper.add_representer(_Block, lambda d, data: d.represent_mapping(
    "tag:yaml.org,2002:map", data.items(), flow_style=False))
_Dumper.add_representer(dict, _repr_dict)
_Dumper.add_representer(list, _repr_list)
_Dumper.add_representer(str, _repr_str)


def spec_to_data(spec: FlowSpec) -> dict[str, Any]:
    """Forma mínima (sem valores por defeito), com `version` sempre presente."""
    data = spec.model_dump(mode="json", by_alias=True, exclude_defaults=True)
    data = {"version": spec.version, **data}
    if "layout" in data:
        data["layout"] = {k: {"x": round(p["x"]), "y": round(p["y"])}
                          for k, p in data["layout"].items()}
    return data


def dump_flow(spec: FlowSpec) -> str:
    data = spec_to_data(spec)
    parts = [_HEADER.format(title=spec.title.replace("\n", " "), name=spec.name)]
    # blocos de topo separados por uma linha em branco, para leitura e diffs
    for key, value in data.items():
        chunk = yaml.dump(_Block({key: value}), Dumper=_Dumper, allow_unicode=True, sort_keys=False,
                          width=100)
        if key in ("start", "agents", "gates", "edges", "mcp_servers", "layout"):
            parts.append("\n")
        parts.append(chunk)
    return "".join(parts)

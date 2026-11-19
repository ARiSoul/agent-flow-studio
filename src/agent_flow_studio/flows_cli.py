"""agent-flow-studio-flows: list, validate and inspect flows; create flows from templates."""
import argparse
import json
import sys

from .builder import FlowError, list_flows, load_spec
from .builder.catalog import catalog_dict
from .builder.compile import compile_flow, flow_section, flows_dir
from .builder.templates import TemplateError, render, templates_dict
from .editor.validation import _atomic_write
from .editor.yaml_io import dump_flow


def main() -> None:
    p = argparse.ArgumentParser(prog="agent-flow-studio-flows")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list", help="list the flows in flows/")
    v = sub.add_parser("validate", help="validate a flow (format, graph, tools, prompts)")
    v.add_argument("flow")
    s = sub.add_parser("show", help="show the generated orchestrator prompt")
    s.add_argument("flow")
    sub.add_parser("catalog", help="available tools and contracts (JSON)")
    sub.add_parser("templates", help="list the flow templates and their parameters")
    n = sub.add_parser("new", help="create a flow from a template")
    n.add_argument("flow", help="name of the new flow (folder in flows/)")
    n.add_argument("--template", required=True)
    n.add_argument("--title")
    n.add_argument("--set", action="append", default=[], metavar="PARAM=VALUE",
                   help="template parameter (repeat for each one)")
    a = p.parse_args()

    if a.cmd == "list":
        for name in list_flows():
            try:
                spec, _ = load_spec(name)
                print(f"{name:24} {spec.title}  ({len(spec.agents)} agents, {len(spec.gates)} gates)")
            except FlowError as e:
                print(f"{name:24} INVALID: {e}")
    elif a.cmd == "catalog":
        json.dump(catalog_dict(), sys.stdout, ensure_ascii=False, indent=2)
    elif a.cmd == "templates":
        for t in templates_dict():
            print(f"{t['id']:16} {t['title']}  [{t['source']}]")
            for k, prm in t["params"].items():
                extra = f" (default {prm['default']})" if prm["default"] else ""
                need = "" if prm["required"] else " (optional)"
                print(f"    --set {k}=…  {prm['label']}{extra}{need}")
    elif a.cmd == "new":
        _new(a.flow, a.template, a.title, a.set)
    else:
        try:
            spec, flow_dir = load_spec(a.flow)
            compiled = compile_flow(spec, flow_dir, resolve_secrets=False)
        except FlowError as e:
            raise SystemExit(f"INVALID: {e}") from e
        if a.cmd == "validate":
            print(f"OK: {spec.name} · {len(spec.agents)} agents · {len(spec.gates)} gates · "
                  f"{len(compiled.options.allowed_tools)} allowed tools")
        else:
            print(flow_section(spec))


def _new(name: str, template: str, title: str | None, sets: list[str]) -> None:
    import re

    if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,39}", name):
        raise SystemExit("invalid flow name: lowercase letters, digits, - or _")
    d = flows_dir() / name
    if d.exists():
        raise SystemExit(f"a flow {name!r} already exists in {d.parent}")
    params = {}
    for s in sets:
        k, sep, v = s.partition("=")
        if not sep:
            raise SystemExit(f"--set {s!r}: use PARAM=VALUE")
        params[k.strip()] = v
    try:
        spec, prompts = render(template, name, title or name, params)
    except TemplateError as e:
        raise SystemExit(f"ERROR: {e}") from e
    for rel, text in prompts.items():
        _atomic_write(d / rel, text)
    _atomic_write(d / "flow.yaml", dump_flow(spec))
    print(f"Created {d / 'flow.yaml'} from template {template}. "
          f"Check it with: agent-flow-studio-flows validate {name}")


if __name__ == "__main__":
    main()

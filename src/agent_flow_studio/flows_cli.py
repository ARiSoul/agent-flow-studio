"""agent-flow-studio-flows: list, validate and inspect flows."""
import argparse
import json
import sys

from .builder import FlowError, list_flows, load_spec
from .builder.catalog import catalog_dict
from .builder.compile import compile_flow, flow_section


def main() -> None:
    p = argparse.ArgumentParser(prog="agent-flow-studio-flows")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list", help="list the flows in flows/")
    v = sub.add_parser("validate", help="validate a flow (format, graph, tools, prompts)")
    v.add_argument("flow")
    s = sub.add_parser("show", help="show the generated orchestrator prompt")
    s.add_argument("flow")
    sub.add_parser("catalog", help="available tools and contracts (JSON)")
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


if __name__ == "__main__":
    main()

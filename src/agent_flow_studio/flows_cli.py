"""agent-flow-studio-flows: list, validate and inspect flows; create flows from templates."""
import argparse
import json
import sys

from . import workspaces
from .builder import FlowError, list_flows, load_spec
from .builder.catalog import catalog_dict
from .builder.compile import compile_flow, flow_locations, flow_section
from .builder.library import LibraryError, agents_dict
from .builder.templates import TemplateError, render, templates_dict
from .editor.validation import _atomic_write
from .editor.yaml_io import dump_flow
from .messages import MsgError


def main() -> None:
    p = argparse.ArgumentParser(prog="agent-flow-studio-flows")
    p.add_argument("--workspace", help="studio workspace: a registered name, or the folder of a "
                                       "workspace or of its repository (default: the studio)")
    p.add_argument("--env", help="environment of the workspace (default: its default one)")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list", help="list the flows in flows/ (or of the workspace)")
    sub.add_parser("workspaces", help="list the studio workspaces on this machine")
    c = sub.add_parser("context", help="show the workspace context, or explore/update it "
                                       "(--workspace; calls a model)")
    how = c.add_mutually_exclusive_group()
    how.add_argument("--explore", action="store_true", help="write it from scratch")
    how.add_argument("--update", action="store_true", help="bring it up to date")
    c.add_argument("--root", action="append", default=[], help="only this root (repeat)")
    v = sub.add_parser("validate", help="validate a flow (format, graph, tools, prompts)")
    v.add_argument("flow")
    s = sub.add_parser("show", help="show the generated orchestrator prompt")
    s.add_argument("flow")
    sub.add_parser("catalog", help="available tools and contracts (JSON)")
    sub.add_parser("templates", help="list the flow templates and their parameters")
    sub.add_parser("agents", help="list the agent library (copied into flows in the editor)")
    sub.add_parser("providers", help="list the model providers of this machine (providers.yaml)")
    k = sub.add_parser("provider-key", help="save a provider's key in the system keyring "
                       "(asked without echo; for a provider with key: keyring)")
    k.add_argument("provider")
    n = sub.add_parser("new", help="create a flow from a template")
    n.add_argument("flow", help="name of the new flow (folder in flows/)")
    n.add_argument("--template", required=True)
    n.add_argument("--title")
    n.add_argument("--set", action="append", default=[], metavar="PARAM=VALUE",
                   help="template parameter (repeat for each one)")
    a = p.parse_args()
    try:
        ws = workspaces.from_cli(a.workspace, a.env)
    except MsgError as e:
        raise SystemExit(str(e)) from e
    with workspaces.using(ws, a.env):
        _run(a)


def _run(a: argparse.Namespace) -> None:
    if a.cmd == "context":
        _context(a)
    elif a.cmd == "workspaces":
        for w in workspaces.list_all():
            envs = ", ".join(w["environments"]) or "-"
            print(f"{w['name']:20} {w['where']:8} {w['folder'] or ''}  (environments: {envs})")
    elif a.cmd == "list":
        for name in list_flows():
            try:
                spec, _ = load_spec(name)
                print(f"{name:24} {spec.title}  ({len(spec.agents)} agents, {len(spec.gates)} gates)")
            except FlowError as e:
                print(f"{name:24} INVALID: {e}")
    elif a.cmd == "providers":
        from .providers import summary
        for pv in summary():
            if pv.get("error"):
                raise SystemExit(pv["error"])
            print(f"{pv['name']:16} {pv['kind']:10} key: {pv['key']:8} "
                  f"models: {', '.join(pv['models']) or '-'}")
    elif a.cmd == "provider-key":
        import getpass

        from .providers import load, set_key
        found = load()
        if a.provider not in found or found[a.provider].key != "keyring":
            raise SystemExit(f"provider {a.provider!r}: not in providers.yaml with key: keyring")
        value = getpass.getpass(f"Key for provider {a.provider} (not shown): ").strip()
        if not value:
            raise SystemExit("no key given: nothing saved")
        set_key(a.provider, value)
        print(f"saved in the system keyring for provider {a.provider}")
    elif a.cmd == "catalog":
        json.dump(catalog_dict(), sys.stdout, ensure_ascii=False, indent=2)
    elif a.cmd == "templates":
        for t in templates_dict():
            print(f"{t['id']:16} {t['title']}  [{t['source']}]")
            for k, prm in t["params"].items():
                extra = f" (default {prm['default']})" if prm["default"] else ""
                need = "" if prm["required"] else " (optional)"
                print(f"    --set {k}=…  {prm['label']}{extra}{need}")
    elif a.cmd == "agents":
        try:
            for ag in agents_dict():
                print(f"{ag['id']:20} {ag['title']}  [{ag['source']}]")
        except LibraryError as e:
            raise SystemExit(f"INVALID: {e}") from e
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


def _context(a: argparse.Namespace) -> None:
    import asyncio

    from . import context
    from .editor.runner import RunError, Runner

    ws = workspaces.current()
    if ws.studio:
        raise SystemExit("the studio's own workspace has no context: give --workspace")
    if not (a.explore or a.update):
        for r, s in context.roots_state(ws).items():
            when = "never explored" if not s["explored"] else (
                f"commits since: {s['commits_since']}" if s["commits_since"] is not None
                else "explored")
            print(f"{r:16} {when:24} {s['path']}")
        print()
        print(context.context_text(ws, limit=10**9) or "(no context yet: --explore)")
        return

    async def go():  # type: ignore[no-untyped-def]
        rn = Runner()
        r = rn.update_context("explore" if a.explore else "update", a.root or None)
        while r.task and not r.task.done():
            await asyncio.sleep(0.2)
        return r
    try:
        run = asyncio.run(go())
    except RunError as e:
        raise SystemExit(str(e)) from e
    for e in run.events:
        if e.get("type") == "context" and e.get("status") != "running":
            print(f"{e['root']}: {e['status']} {e.get('summary') or e.get('message') or ''}")
    if run.status != "done":
        raise SystemExit(1)


def _new(name: str, template: str, title: str | None, sets: list[str]) -> None:
    import re

    if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,39}", name):
        raise SystemExit("invalid flow name: lowercase letters, digits, - or _")
    d = flow_locations()[0][1] / name  # flows/ of the studio, or of the open workspace
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

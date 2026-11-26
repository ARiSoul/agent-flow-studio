"""Loads flows/<name>/flow.yaml and compiles it for the Claude Agent SDK.

The orchestrator receives the graph (agents, gates, edges) as a generated "Flow" section; each
subagent receives its tools, artifacts and common rules. Security does not depend on the model:
closed tool list in the hook, per flow and per subagent, contracts in save_artifact,
`dontAsk`, no native tools besides delegation, and no user settings.

Graph mode (`execution: graph`): `compile_graph` / `compile_agent` give each agent its own
session options instead (its tools only, a guard with a fixed identity, no delegation), with one
`WritePolicy` shared by the run; the engine (`engine.py`) runs the graph.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

import yaml
from claude_agent_sdk import AgentDefinition, ClaudeAgentOptions, create_sdk_mcp_server, tool

from ..config import settings
from ..connections import store as connection_store
from ..connections.actions import describe_action
from ..extensions import extension_flow_dirs
from ..hooks import DELEGATION_TOOL, DELEGATION_TOOLS, make_guard, make_hooks
from ..messages import MsgError, msg, reason
from ..policy import RUN_ACTION_TOOL, WritePolicy
from ..tools import artifacts, media
from ..tools import git as git_tool
from ..tools import io as io_tool
from ..tools.common import err, ok
from . import expr
from .catalog import BUILTIN_SERVERS, BUILTIN_TOOLS, CONTRACTS
from .graph import parallel_groups
from .spec import AgentSpec, Edge, ExternalMcp, FlowSpec
from .types import IOType

DISALLOWED = [
    "Bash", "Write", "Edit", "MultiEdit", "NotebookEdit", "WebFetch", "WebSearch",
    "Read", "Glob", "Grep",
]
SUBAGENT_RULES = (
    "\n\n## Common rules\nRead-only; do not make up data; every conclusion points to an "
    "`evidence_ref`. If `save_artifact` returns a contract or name ERROR, fix the JSON (only the "
    "expected fields, with the expected types) and save again. Always use the run id that "
    "the orchestrator gives you as `run_id`."
)
# Graph mode: each agent is its own session, run by the studio's engine (no orchestrator).
AGENT_RULES = (
    "\n\n## Common rules\nYou are one step of a flow that the studio runs: do only your own "
    "work (you cannot delegate). Do not make up data; every conclusion points to an "
    "`evidence_ref`. Change nothing unless a Writes section allows it. If `save_artifact` or "
    "`set_output` returns an ERROR, fix the value and save it again. Always use the run id given "
    "in the message as `run_id`. Finish with a short summary of what you did."
)
DEFAULT_AGENT_TURNS = 40
# a tool result with images is one message of the SDK stream, and the SDK ends the session on a
# message over its own limit (1 MB by default)
MAX_MESSAGE = 64 * 1024 * 1024
_ENV_IN = re.compile(r"\$\{([A-Z_][A-Z0-9_]*)\}")
# Orchestrator-only tool: signals in a structured way that the flow reached a gate
# (the editor uses it to show the pending approval). It runs nothing.
GATE_SERVER = "flow_control"
GATE_TOOL = f"mcp__{GATE_SERVER}__reach_gate"
# Orchestrator-only tool: evaluates the `if` expressions of the edges that leave a node.
NEXT_TOOL = f"mcp__{GATE_SERVER}__next_steps"


class FlowError(MsgError):
    pass


@dataclass(frozen=True)
class CompiledFlow:
    spec: FlowSpec
    options: ClaudeAgentOptions
    agent_tools: dict[str, list[str]]  # subagent → SDK names
    policy: WritePolicy | None = None  # the editor runner attaches the `approver` to it

    def task(self, run_input: str) -> str:
        """Legacy flows (no typed `inputs`): the one input is also the run id."""
        if not re.fullmatch(self.spec.input.pattern, run_input):
            raise FlowError(msg("compile.input_invalid", "invalid {label}: {value}",
                                label=self.spec.input.label, value=repr(run_input)))
        return self.spec.task.replace("{input}", run_input)

    def task_for(self, run_id: str, values: dict[str, Any]) -> str:
        """The first message of a run: the task with the inputs filled in, the run id and a
        summary of the inputs. Legacy flows keep their exact task."""
        if not self.spec.inputs:
            return self.task(str(values.get("input", run_id)))

        def short(v: Any) -> str:
            text = v if isinstance(v, str) else json.dumps(v, ensure_ascii=False)
            return text if len(text) <= 200 else text[:200] + "…"

        text = self.spec.task.replace("{run_id}", run_id).replace("{input}", run_id)
        for name in self.spec.inputs:
            text = text.replace("{" + name + "}", short(values.get(name, "")))
        lines = [text, "", f"Run id: `{run_id}` (give it as `run_id` to every subagent).",
                 "Flow inputs (each agent gets its own with `get_inputs`):"]
        lines += [f"- `{n}`: {short(values[n])}" for n in self.spec.inputs if n in values]
        return "\n".join(lines)


def flows_dir() -> Path:
    return Path(os.getenv("FLOWS_DIR", "flows"))


def flow_locations(base: Path | None = None) -> list[tuple[str, Path]]:
    """Where flows live: "local" (flows/, the base) and each loaded extension's `flows/` folder
    (extensions/<name>/flows/, outside the public repo; an extension can be its own private repo).
    Flow names are unique across all of them."""
    from .. import workspaces
    ws = workspaces.current()
    if not ws.studio:  # a workspace: only its own flows
        return [("workspace", ws.flows_dir)]
    return [("local", base or flows_dir()), *extension_flow_dirs()]


def find_flow(name: str, base: Path | None = None) -> tuple[str, Path] | None:
    """(location, folder) of a flow by its name, or None."""
    for loc, d in flow_locations(base):
        if (d / name / "flow.yaml").is_file():
            return loc, d / name
    return None


def list_flows(base: Path | None = None) -> list[str]:
    return sorted({p.parent.name for _, d in flow_locations(base) for p in d.glob("*/flow.yaml")})


def load_spec(name_or_path: str | Path, base: Path | None = None) -> tuple[FlowSpec, Path]:
    p = Path(name_or_path)
    if p.suffix in (".yaml", ".yml"):
        path = p
    else:
        found = find_flow(str(name_or_path), base)
        path = (found[1] if found else (base or flows_dir()) / str(name_or_path)) / "flow.yaml"
    if not path.exists():
        raise FlowError(f"flow not found: {path}")
    from .. import workspaces
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        data, unknown = workspaces.substitute(data)  # {vars.x} / {roots.x} of the workspace
        if unknown:
            raise FlowError(workspaces.unknown_error(unknown).msgs[0])
        spec = FlowSpec.model_validate(data)
    except (yaml.YAMLError, ValueError) as e:
        if isinstance(e, FlowError):
            raise
        raise FlowError(f"{path}: {e}") from e
    return spec, path.parent


def _read_prompt(flow_dir: Path, inline: str | None, file: str | None, what: str) -> str:
    if inline is not None:
        return inline
    assert file is not None
    p = (flow_dir / file).resolve()
    if flow_dir.resolve() not in p.parents:
        raise FlowError(msg("compile.prompt_outside", "{what}: prompt_file outside the flow folder",
                            what=what))
    if not p.exists():
        raise FlowError(msg("compile.prompt_missing", "{what}: prompt_file does not exist ({file})",
                            what=what, file=file))
    from .. import workspaces
    text, unknown = workspaces.substitute_text(p.read_text(encoding="utf-8"))
    if unknown:
        raise FlowError(workspaces.unknown_error(unknown).msgs[0])
    return text


def _sdk_tool(spec: FlowSpec, tool_id: str) -> str:
    if tool_id in BUILTIN_TOOLS:
        return BUILTIN_TOOLS[tool_id].sdk_name
    server, name = tool_id.split(".", 1)
    return f"mcp__{server}__{name}"


def describe_type(t: IOType | str, multiple: bool = False) -> str:
    """Short text of a type for the prompts: choice: low|high, json (contract X), file[] …"""
    if isinstance(t, str):
        return t + ("[]" if multiple else "")
    base = t.type
    if t.type == "choice":
        base = "choice: " + "|".join(t.options)
    elif t.type == "json" and t.contract:
        base = f"json (contract {t.contract})"
    elif t.type == "file" and t.accept:
        base = f"file ({', '.join(t.accept)})"
    elif t.type in ("number", "integer") and (t.min is not None or t.max is not None):
        lo = "" if t.min is None else f"{t.min:g}"
        hi = "" if t.max is None else f"{t.max:g}"
        base = f"{t.type} {lo}..{hi}"
    elif t.type == "markdown" and t.draft:
        base = "markdown (draft)"
    return base + ("[]" if t.multiple else "")


def io_tools(spec: FlowSpec, agent: str) -> list[str]:
    """`io` tools an agent gets implicitly from its inputs and outputs (SDK names)."""
    a = spec.agents[agent]
    out = []
    if a.inputs:
        out.append("mcp__io__get_inputs")
    if a.outputs:
        out.append("mcp__io__set_output")
    files = file_tools(spec, agent)
    out += [f"mcp__io__{t}" for t in ("read_file", "view_image", "view_video") if t in files]
    return out


def file_tools(spec: FlowSpec, agent: str) -> set[str]:
    """What an agent with file inputs needs to read them: by the extensions the file accepts
    (`accept`), or all of them when it accepts any file."""
    out: set[str] = set()
    for n, i in spec.agents[agent].inputs.items():
        if spec.input_type(agent, n) != "file":
            continue
        src = spec.source_io(i.from_) if i.from_ else None
        exts = {e.lower() for e in src.accept} if src is not None else set()
        if not exts or exts - media.IMAGE_EXTS - media.VIDEO_EXTS:
            out.add("read_file")
        if not exts or exts & media.IMAGE_EXTS:
            out.add("view_image")
        if not exts or exts & media.VIDEO_EXTS:
            out.add("view_video")
    return out


def _io_section(spec: FlowSpec, agent: str) -> str:
    a = spec.agents[agent]
    if not a.inputs and not a.outputs:
        return ""
    lines = [(f"Your name in this flow is `{agent}`: always pass `agent: \"{agent}\"` and the run "
             "id (`run_id`) to the io tools.")]
    if a.inputs:
        lines.append("Inputs — call `get_inputs` first:")
        for n, i in a.inputs.items():
            src = f"from {i.from_}" if i.from_ else "given to you"
            need = "required" if i.required else "optional"
            desc = f" — {i.description}" if i.description else ""
            io = spec.source_io(i.from_) if i.from_ and not i.type else None
            shown = describe_type(io) if io else spec.input_type(agent, n)
            lines.append(f"- `{n}` ({shown}, {need}, {src}){desc}")
        if any((i.from_ or "").startswith("previous.") for i in a.inputs.values()):
            lines.append("Inputs from `previous.*` are the values of the last finished run of this "
                         "flow; on the first run they are missing.")
        files = file_tools(spec, agent)
        if files:
            how = [text for t, text in (
                ("read_file", "text files with `read_file`"),
                ("view_image", "images with `view_image`"),
                ("view_video", "videos with `view_video` (frames, no sound)")) if t in files]
            lines.append("File inputs are references {name, path}: open "
                         + ", ".join(how) + " (path in the run folder).")
    if a.outputs:
        lines.append("Outputs — save each one with `set_output` before you finish (the value must "
                     "match the type; a file is {\"filename\", \"content\"} or {\"path\"}):")
        for n, t in a.outputs.items():
            need = "required" if t.required else "optional"
            desc = f" — {t.description}" if t.description else ""
            lines.append(f"- `{n}` ({describe_type(t)}, {need}){desc}")
        lines.append("If `set_output` returns an ERROR, fix the value and save it again.")
    return "\n\n## Inputs and outputs\n" + "\n".join(lines)


def _artifacts_section(a: AgentSpec) -> str:
    lines = []
    if a.reads:
        lines.append("You read with `read_artifact`: " + ", ".join(f"`{r}`" for r in a.reads) + ".")
    if not a.reads and "artifacts.read_artifact" in a.tools:  # it would guess names
        lines.append("No artifacts are listed for you to read: call `read_artifact` only with a "
                     "name you were given; do not guess names.")
    for w in a.writes:
        extra = f" (contract {w.contract})" if w.contract else (" (draft)" if w.draft else "")
        lines.append(f"You save with `save_artifact`: `{w.name}`{extra}.")
    return ("\n\n## Artifacts\n" + "\n".join(lines)) if lines else ""


def _describe_target(spec: FlowSpec, node: str) -> str:
    if node in spec.agents:
        return f"delegate to `{node}`"
    g = spec.gates[node]
    if g.kind == "decision":
        return f"go to the decision **{g.label}** (`{node}`)"
    if g.kind == "human":
        return f"STOP and ask for human approval: **{g.label}** (gate `{node}`)"
    return f"STOP and report: **{g.label}** (gate `{node}`)"


def _typed(spec: FlowSpec) -> bool:
    """The flow uses typed inputs/outputs or expressions (legacy flows keep their old prompt)."""
    return (bool(spec.inputs) or any(a.inputs or a.outputs for a in spec.agents.values())
            or _has_if(spec))


def _edge_condition(e: Edge) -> str:
    """'if `expr` (check with `next_steps`) and <text>' · 'if <text>' · '' (always)."""
    parts = []
    if e.if_:
        parts.append(f"if `{e.if_}` (check with `next_steps`)")
    if e.when:
        parts.append(f"{'and ' if parts else 'if '}{e.when}")
    return " ".join(parts)


def _agent_io_line(spec: FlowSpec, name: str) -> str:
    a = spec.agents[name]
    parts = []
    if a.inputs:
        ins = []
        for n, i in a.inputs.items():
            src = f"from {i.from_}" if i.from_ else "given"
            ins.append(f"`{n}` ({src}{'' if i.required else ', optional'})")
        parts.append("Inputs: " + ", ".join(ins) + ".")
    if a.outputs:
        parts.append("Outputs: " + ", ".join(f"`{n}` ({describe_type(t)})"
                                             for n, t in a.outputs.items()) + ".")
    return ("   " + " ".join(parts)) if parts else ""


def flow_section(spec: FlowSpec) -> str:
    """The "Flow" section of the orchestrator prompt, generated from the graph. Flows with typed
    inputs/outputs also get each agent's inputs and outputs, the `if` expressions (checked with
    `next_steps`) and the groups of agents that can run at the same time."""
    typed = _typed(spec)
    out = ["## Flow (generated from the flow graph)", f"Start at `{spec.start}`."]
    for i, name in enumerate(spec.agent_order(), 1):
        a = spec.agents[name]
        writes = ", ".join(f"`{w.name}`" for w in a.writes) or "—"
        saves = "" if typed and not a.writes else f" Saves: {writes}."
        out.append(f"\n{i}. `{name}`: {a.description}{saves}")
        if typed and (line := _agent_io_line(spec, name)):
            out.append(line)
        for e in spec.outgoing(name):
            if typed:
                cond = _edge_condition(e) or "next"
            else:
                cond = f"if {e.when}" if e.when else "next"
            out.append(f"   - {cond}: {_describe_target(spec, e.to)}.")
    decisions = [g for g, x in spec.gates.items() if x.kind == "decision"]
    if decisions:
        out.append("\nDecisions (no agent runs there): call `next_steps` with the run id and the "
                   "decision name, and follow only the branch it marks `chosen` (`go_to`):")
        for g in decisions:
            for e in spec.outgoing(g):
                cond = f"if `{e.if_}`" if e.if_ else "otherwise"
                out.append(f"- **{spec.gates[g].label}**, {cond}: {_describe_target(spec, e.to)}.")
    gates_out = [g for g in spec.gates if spec.outgoing(g) and g not in decisions]
    if gates_out:
        out.append("\nAfter a human stop (in a run resumed with the answer):")
        for g in gates_out:
            for e in spec.outgoing(g):
                if typed and e.if_:
                    cond = ", " + _edge_condition(e)
                else:
                    cond = f" and {e.when}" if e.when else ""
                out.append(f"- after **{spec.gates[g].label}**{cond}: {_describe_target(spec, e.to)}.")
    if typed:
        groups = parallel_groups(spec)
        if groups:
            out.append("\nThese agents do not depend on each other: when their conditions hold, "
                       "delegate to them in the same message (they run at the same time):")
            out += ["- " + ", ".join(f"`{n}`" for n in g) for g in groups]
        out.append("\nGive every agent the run id: each one reads its inputs with `get_inputs` "
                   "and saves its outputs with `set_output`.")
        if _has_if(spec):
            out.append("After an agent finishes, call `next_steps` with the run id and its name: "
                       "it evaluates the `if` conditions from the saved outputs. Follow only the "
                       "edges whose condition holds.")
    out.append("\nNever skip a step or use an agent outside this graph. When you reach a "
               "stop, call `reach_gate` with the gate name and the reason, then end the "
               "run and explain what is left to decide. If an agent fails or ends without its "
               "outputs, delegate to it again at most once; if it fails again, stop and report "
               "what went wrong (do not keep retrying).")
    return "\n".join(out)


def _external_config(name: str, m: ExternalMcp) -> dict[str, Any]:
    def expand(v: str) -> str:
        def rep(mt: re.Match[str]) -> str:
            val = os.getenv(mt.group(1))
            if val is None:
                raise FlowError(msg("compile.mcp_env", "MCP {name}: environment variable {var} is not set",
                                    name=repr(name), var=mt.group(1)))
            return val
        return _ENV_IN.sub(rep, v)

    if m.type == "stdio":
        cfg: dict[str, Any] = {"type": "stdio", "command": m.command, "args": list(m.args)}
        if m.env:
            cfg["env"] = {k: expand(v) for k, v in m.env.items()}
        return cfg
    cfg = {"type": m.type, "url": m.url}
    if m.headers:
        cfg["headers"] = {k: expand(v) for k, v in m.headers.items()}
    return cfg


def _connections(spec: FlowSpec) -> dict[str, Any]:
    wanted = {c for a in spec.agents.values() for c in (*a.connections, *a.write_connections)}
    wanted |= {a.action.connector for a in spec.agents.values() if a.action}
    if not wanted:
        return {}
    try:
        conns = connection_store.load()
    except connection_store.ConnectorConfigError as e:
        raise FlowError(str(e)) from e
    missing = sorted(wanted - set(conns))
    if missing:
        users = [n for n, a in spec.agents.items()
                 if {*a.connections, *a.write_connections} & set(missing)]
        raise FlowError(msg("compile.connector_missing",
                            "agent {agents}: connector(s) not configured on this machine: {connectors}",
                            agents=", ".join(users), connectors=", ".join(missing)))
    errors = []
    for n, a in spec.agents.items():
        if a.action:  # an action node: a read action that exists, with known parameters
            act = conns[a.action.connector].actions.get(a.action.name)
            if act is None:
                errors.append(msg("compile.action_unknown",
                                  "agent {agent}: connector {connector} has no action {action}",
                                  agent=repr(n), connector=repr(a.action.connector),
                                  action=repr(a.action.name)))
            elif act.access != "read":
                errors.append(msg("compile.action_write",
                                  "agent {agent}: {action} is a write action; an action node runs "
                                  "only read actions (writes go through an agent and its autonomy)",
                                  agent=repr(n), action=repr(a.action.name)))
            else:
                extra = sorted(set(a.action.params) - set(act.params))
                lacking = sorted(k for k, p in act.params.items()
                                 if p.required and k not in a.action.params)
                if extra or lacking:
                    errors.append(msg("compile.action_params",
                                      "agent {agent}: parameters of {action}: unknown {extra}; "
                                      "missing {lacking}", agent=repr(n),
                                      action=repr(a.action.name),
                                      extra=", ".join(extra) or "—",
                                      lacking=", ".join(lacking) or "—"))
        for c in a.connections:  # a connector without read actions is used with http_get
            if conns[c].type == "website" and not conns[c].actions:
                errors.append(msg("compile.website_no_actions",
                                  "agent {agent}: website connector {connector} has no actions yet "
                                  "(a website is only used through its actions)",
                                  agent=repr(n), connector=repr(c)))
            elif not _has_actions(conns[c], "read") and "connections.http_get" not in a.tools \
                    and conns[c].type != "website":
                errors.append(msg("compile.needs_http_get",
                                  "agent {agent}: connector {connector} has no read actions, so the "
                                  "agent needs connections.http_get", agent=repr(n), connector=repr(c)))
        direct = [c for c in {*a.connections, *a.write_connections}
                  if conns[c].type != "website" and not conns[c].actions_only]
        for http in ("connections.http_get", "connections.http_write"):
            if http in a.tools and not direct and (a.connections or a.write_connections):
                errors.append(msg("compile.http_tool_unused",
                                  "agent {agent}: {tool} is refused on its connectors (websites and "
                                  "'only these actions' are used through run_action): remove it",
                                  agent=repr(n), tool=http))
        for c in a.write_connections:
            if not _has_actions(conns[c], "write") and "connections.http_write" not in a.tools:
                errors.append(msg("compile.needs_http_write",
                                  "agent {agent}: connector {connector} has no write actions, so the "
                                  "agent needs connections.http_write", agent=repr(n), connector=repr(c)))
            if conns[c].access != "write":
                errors.append(msg("compile.connector_read_only",
                                  "agent {agent}: connector {connector} is read-only (access: read)",
                                  agent=repr(n), connector=repr(c)))
    if errors:
        raise FlowError(*errors)
    return conns


def _has_actions(conn: Any, access: str) -> bool:
    return any(x.access == access for x in conn.actions.values())


def action_tools(a: AgentSpec, conns: dict[str, Any]) -> list[str]:
    """`run_action` comes implicitly with any connector of the agent that has actions."""
    names = {*a.connections, *a.write_connections}
    return [RUN_ACTION_TOOL] if any(conns[c].actions for c in names if c in conns) else []


def _context_section(spec: FlowSpec) -> str:
    """The workspace context (context.md of the studio workspace) as read-only background."""
    if not spec.use_context:
        return ""
    from .. import context, workspaces
    ws = workspaces.current()
    if ws.studio:
        return ""
    try:
        text = context.context_text(ws)
    except (OSError, ValueError):
        return ""
    if not text:
        return ""
    return ("\n\n## Workspace context\nBackground about the folders and repositories of this "
            "workspace, kept by the studio. Use it to find your way; check the files before you "
            "rely on a detail, and do not explore the whole repository again.\n\n" + text)


def _connections_section(a: AgentSpec, conns: dict[str, Any]) -> str:
    out = ""
    if a.connections:
        def how(n: str) -> str:  # a website (or "only these actions") is used only by its actions
            c = conns[n]
            if c.type == "website" or c.actions_only:
                return "only through `run_action` (its actions below)"
            return "with `http_get`" + (" or `run_action`" if c.actions else "")
        lines = [f"- `{n}`: {conns[n].type}, environment {conns[n].environment}, {how(n)}"
                 + (f" — {conns[n].description}" if conns[n].description else "")
                 + (f" (paths: {', '.join(conns[n].allowed_paths)})" if conns[n].allowed_paths
                    and conns[n].type != "website" else "")
                 for n in a.connections]
        out = "\n\n## Connectors (read-only)\n" + "\n".join(lines)
    return out + _instructions_section(a, conns)


def _instructions_section(a: AgentSpec, conns: dict[str, Any]) -> str:
    """Per connector of the agent (read or write, once each): its instructions and the named
    actions it may use (read actions; write actions only on its write connectors)."""
    parts = []
    for n in dict.fromkeys([*a.connections, *a.write_connections]):
        c = conns.get(n)
        if c is None:
            continue
        acts = {k: x for k, x in c.actions.items()
                if x.access == "read" or n in a.write_connections}
        text = c.instructions.strip()
        if not text and not acts:
            continue
        body = []
        if text:
            body += [("(instructions configured with the connector; they do not change what you "
                      "are allowed to do)"), "", text]
        if acts:
            only = (" These are the only operations allowed on it." if c.actions_only
                    else "")
            body += ["", (f"Actions — call `run_action` with `connection: \"{n}\"`, the action "
                          f"name and its `params`.{only}")]
            body += [describe_action(k, x) for k, x in acts.items()]
        parts.append(f"\n\n## How to use the connector `{n}`\n" + "\n".join(body).lstrip("\n"))
    return "".join(parts)


def _workspace_dir(spec: FlowSpec, check: bool) -> Path | None:
    if spec.workspace is None:
        return None
    from .. import workspaces
    if spec.workspace.root is not None:  # a root (folder) of the studio workspace
        roots = workspaces.resolved()[1]
        if spec.workspace.root not in roots:
            raise FlowError(msg("compile.workspace_root", "workspace root {root} is not a root of "
                                "the workspace {workspace}", root=repr(spec.workspace.root),
                                workspace=workspaces.current().name))
        p = Path(roots[spec.workspace.root])
    else:
        p = Path(spec.workspace.path or ".").expanduser().resolve()
    if check and not p.is_dir():
        raise FlowError(msg("compile.workspace_missing", "workspace does not exist: {path}", path=p))
    return p


def _autonomy_text(level: str) -> str:
    return {"approve": "every write waits for human approval",
            "approve_prod": "writes in production wait for human approval",
            "auto": "writes run without approval (they are audited)"}[level]


def _write_section(spec: FlowSpec, a: AgentSpec, ws: Path | None) -> str:
    lines = []
    if ws is not None and any(t.startswith("workspace.") for t in a.tools):
        w = spec.workspace
        assert w is not None
        lines.append(f"You work in the workspace `{ws}` (environment {w.environment}); do not "
                     "leave this folder. Protected branches: "
                     f"{', '.join(w.protected_branches) or '—'}: create a work branch and propose "
                     "the changes in a PR (as a draft).")
    if a.write_connections:
        lines.append("Connectors with write (`http_write`): "
                     + ", ".join(f"`{c}`" for c in a.write_connections) + ".")
    if lines:
        lines.append(f"Autonomy: {_autonomy_text(a.autonomy or spec.autonomy)}. If a write "
                     "is refused, do not insist: continue without it or report.")
        return "\n\n## Writes\n" + "\n".join(lines)
    return ""


def _gate_server(spec: FlowSpec) -> Any:
    gates = dict(spec.gates)
    nodes = set(spec.agents) | set(spec.gates)

    @tool("reach_gate", "Signals that the flow reached a gate (human approval or stop). "
          "Call it once, before you finish.", {"gate": str, "reason": str})
    async def reach_gate(args: dict[str, Any]) -> dict[str, Any]:
        g = gates.get(args["gate"])
        if g is None:
            return err(f"unknown gate {args['gate']!r}; gates: {', '.join(gates)}")
        return ok(f"Recorded: {g.label}. End the run now and explain what is left to decide.")

    @tool("next_steps", "Evaluates the `if` conditions of the edges that leave a node, from the "
          "saved inputs, outputs and human decisions. Call it after an agent finishes.",
          {"run_id": str, "node": str})
    async def next_steps(args: dict[str, Any]) -> dict[str, Any]:
        return next_steps_result(spec, str(args.get("run_id", "")), str(args.get("node", "")),
                                 nodes)

    tools = ([reach_gate] if gates else []) + ([next_steps] if _has_if(spec) else [])
    return create_sdk_mcp_server(name=GATE_SERVER, version="0.1.0", tools=tools)


def _has_if(spec: FlowSpec) -> bool:
    return any(e.if_ for e in spec.edges) or any(g.kind == "decision" for g in spec.gates.values())


def decide_branch(spec: FlowSpec, node: str, scope: dict[str, Any]
                  ) -> tuple[int | None, dict[int, str]]:
    """A decision: the index (in spec.edges) of the first branch whose `if` holds, else the
    branch without `if` ("otherwise"); None when none applies. Also the errors per branch."""
    idxs = [i for i, e in enumerate(spec.edges) if e.from_ == node]
    errors: dict[int, str] = {}
    for i in idxs:
        e = spec.edges[i]
        if not e.if_:
            continue
        try:
            if expr.evaluate(e.if_, scope):
                return i, errors
        except expr.ExprError as x:
            errors[i] = str(x)
    other = [i for i in idxs if not spec.edges[i].if_]
    return (other[0] if other else None), errors


def gate_message(spec: FlowSpec, gate: str, run_id: str) -> str:
    """A stop's message with its {expression} parts filled in ("" without a message)."""
    g = spec.gates[gate]
    if g.kind != "stop" or not g.message.strip():
        return ""
    try:
        return expr.render(g.message, io_tool.scope(spec, run_id))[:2000]
    except ValueError:
        return g.message


def next_steps_result(spec: FlowSpec, run_id: str, node: str,
                      nodes: set[str] | None = None) -> dict[str, Any]:
    """Each outgoing edge of `node`: {to, if, if_holds, when} (if_holds None: no expression)."""
    if node not in (nodes or set(spec.agents) | set(spec.gates)):
        return err(f"unknown node {node!r}")
    try:
        scope = io_tool.scope(spec, run_id)
    except ValueError as e:
        return err(str(e))
    if node in spec.gates and spec.gates[node].kind == "decision":
        chosen, errors = decide_branch(spec, node, scope)
        branches = []
        for i, e in enumerate(spec.edges):
            if e.from_ == node:
                b: dict[str, Any] = {"to": e.to, "if": e.if_ or "otherwise", "chosen": i == chosen}
                if i in errors:
                    b["error"] = errors[i]
                branches.append(b)
        return ok({"node": node, "decision": True, "next": branches,
                   "go_to": spec.edges[chosen].to if chosen is not None else None})
    steps = []
    for e in spec.outgoing(node):
        item: dict[str, Any] = {"to": e.to, "if": e.if_ or None, "if_holds": None,
                                "when": e.when or None}
        if e.if_:
            try:
                item["if_holds"] = expr.evaluate(e.if_, scope)
            except expr.ExprError as x:
                item["error"] = str(x)
        steps.append(item)
    return ok({"node": node, "next": steps})


def compile_flow(spec: FlowSpec, flow_dir: Path, *, resume: str | None = None,
                 resolve_secrets: bool = True, configure_artifacts: bool = True) -> CompiledFlow:
    """configure_artifacts=False: validate only (leaves the global state of `artifacts` as is)."""
    agents: dict[str, AgentDefinition] = {}
    agent_tools: dict[str, list[str]] = {}
    conns = _connections(spec)
    ws = _workspace_dir(spec, check=configure_artifacts)
    _check_subflows(spec, flow_dir)
    ctx = _context_section(spec)
    for name, a in spec.agents.items():
        if a.runs_itself:  # an action or subflow node: the graph engine runs it (no subagent)
            continue
        body = _read_prompt(flow_dir, a.prompt, a.prompt_file, f"agent {name}")
        tools = [_sdk_tool(spec, t) for t in a.tools]
        tools += [t for t in (*io_tools(spec, name), *action_tools(a, conns)) if t not in tools]
        agent_tools[name] = tools
        agents[name] = AgentDefinition(
            description=a.description,
            prompt=(body + _io_section(spec, name) + _artifacts_section(a)
                    + _connections_section(a, conns) + ctx
                    + _write_section(spec, a, ws) + SUBAGENT_RULES),
            tools=tools,
            model=a.model,
            skills=a.skills or None,
            maxTurns=a.max_turns or DEFAULT_AGENT_TURNS,  # never without a limit
        )

    used = {t for ts in spec.agents.values() for t in ts.tools}
    used_builtin = {BUILTIN_TOOLS[t].server for t in used
                    if t in BUILTIN_TOOLS and BUILTIN_TOOLS[t].native is None}
    natives = sorted({str(BUILTIN_TOOLS[t].native) for t in used
                      if t in BUILTIN_TOOLS and BUILTIN_TOOLS[t].native})
    if any(io_tools(spec, n) for n in spec.agents):
        used_builtin.add("io")
    if any(action_tools(a, conns) for a in spec.agents.values()):
        used_builtin.add("connections")
    servers: dict[str, Any] = {s: BUILTIN_SERVERS[s] for s in sorted(used_builtin)}
    for name, m in spec.mcp_servers.items():
        if any(t.startswith(f"{name}.") for t in used):
            servers[name] = _external_config(name, m) if resolve_secrets else {"type": m.type}

    allowed = sorted({t for ts in agent_tools.values() for t in ts})
    if spec.gates or _has_if(spec):  # orchestrator only: the subagents have their own list
        servers[GATE_SERVER] = _gate_server(spec)
        allowed += ([GATE_TOOL] if spec.gates else []) + ([NEXT_TOOL] if _has_if(spec) else [])
    orch = _read_prompt(flow_dir, spec.orchestrator.prompt, spec.orchestrator.prompt_file,
                        "orchestrator") if (spec.orchestrator.prompt or spec.orchestrator.prompt_file) \
        else "You orchestrate this flow. You delegate to the subagents and never change anything."

    if configure_artifacts:
        _configure_run(spec, ws)
    policy = _write_policy(spec, ws, conns)
    options = ClaudeAgentOptions(
        system_prompt=orch.rstrip() + ctx + "\n\n" + flow_section(spec),
        tools=[DELEGATION_TOOL, *natives],
        cwd=str(ws) if ws else None,  # also without native tools: `claude --resume` finds it
        mcp_servers=servers,
        allowed_tools=[*allowed, DELEGATION_TOOL],
        disallowed_tools=[d for d in DISALLOWED if d not in natives],
        permission_mode="dontAsk",
        setting_sources=[],
        max_buffer_size=MAX_MESSAGE,
        agents=agents,
        hooks=make_hooks(make_guard(allowed, agent_tools,
                                    {n: a.connections for n, a in spec.agents.items()}, policy)),
        max_turns=spec.max_turns,
        model=spec.model or settings.model,
        resume=resume,
    )
    return CompiledFlow(spec, options, agent_tools, policy)


def load_flow(name: str, *, resume: str | None = None, base: Path | None = None) -> CompiledFlow:
    spec, flow_dir = load_spec(name, base)
    return compile_flow(spec, flow_dir, resume=resume)


# --- shared per-run state ----------------------------------------------------------------------
def _artifact_names(spec: FlowSpec) -> tuple[set[str], dict[str, Any]]:
    names = {w.name for a in spec.agents.values() for w in a.writes}
    contracts = {w.name: CONTRACTS[w.contract] for a in spec.agents.values()
                 for w in a.writes if w.contract}
    return names, contracts


MAX_SUBFLOW_DEPTH = 3


def _check_subflows(spec: FlowSpec, flow_dir: Path, chain: tuple[str, ...] = ()) -> None:
    """Subflow nodes: the child flow exists in the same studio workspace, runs in graph mode, has
    no human gates, gets only inputs it declares (and all it requires), and has the mapped
    outputs; no cycles (A runs B runs A) and at most 3 levels."""
    subs = {n: a.subflow for n, a in spec.agents.items() if a.subflow is not None}
    if not subs:
        return
    chain = (*chain, spec.name)
    errors: list[str] = []
    for n, sub in subs.items():
        if sub.flow in chain:
            errors.append(msg("compile.subflow_cycle", "agent {agent}: subflow cycle {path}",
                              agent=repr(n), path=" → ".join([*chain, sub.flow])))
            continue
        if len(chain) >= MAX_SUBFLOW_DEPTH:
            errors.append(msg("compile.subflow_depth", "agent {agent}: more than {n} levels of "
                              "subflows ({path})", agent=repr(n), n=MAX_SUBFLOW_DEPTH,
                              path=" → ".join([*chain, sub.flow])))
            continue
        found = find_flow(sub.flow, flow_dir.parent) or find_flow(sub.flow)
        if found is None:
            errors.append(msg("compile.subflow_missing", "agent {agent}: flow {flow} does not "
                              "exist in this workspace", agent=repr(n), flow=repr(sub.flow)))
            continue
        try:
            child, child_dir = load_spec(found[1] / "flow.yaml")
        except FlowError as e:
            errors.append(msg("compile.subflow_invalid", "agent {agent}: flow {flow} is not "
                              "valid: {reason}", agent=repr(n), flow=repr(sub.flow),
                              reason=reason(e)))
            continue
        if child.execution != "graph":
            errors.append(msg("compile.subflow_graph", "agent {agent}: flow {flow} must run with "
                              "execution: graph to be a subflow", agent=repr(n),
                              flow=repr(sub.flow)))
        human = sorted(g for g, x in child.gates.items() if x.kind == "human")
        if human:
            errors.append(msg("compile.subflow_human", "agent {agent}: flow {flow} has human "
                              "gates ({gates}); a subflow cannot pause (approve in this flow)",
                              agent=repr(n), flow=repr(sub.flow), gates=", ".join(human)))
        unknown = sorted(set(sub.inputs) - set(child.inputs))
        lacking = sorted(k for k, t in child.inputs.items()
                         if t.required and t.default is None and not t.show_if
                         and k not in sub.inputs)
        if unknown or lacking:
            errors.append(msg("compile.subflow_inputs", "agent {agent}: inputs of flow {flow}: "
                              "unknown {unknown}; missing {lacking}", agent=repr(n),
                              flow=repr(sub.flow), unknown=", ".join(unknown) or "—",
                              lacking=", ".join(lacking) or "—"))
        for o, m in sub.outputs.items():
            if m.from_ == "message":
                continue
            ca, co = m.from_.split(".", 1)
            if ca not in child.agents or co not in child.agents[ca].outputs:
                errors.append(msg("compile.subflow_output", "agent {agent}: output {output}: "
                                  "flow {flow} has no output {source}", agent=repr(n),
                                  output=repr(o), flow=repr(sub.flow), source=m.from_))
        if not errors:
            try:
                _check_subflows(child, child_dir, chain)
            except FlowError as e:
                errors += e.msgs
    if errors:
        raise FlowError(*errors)


def _configure_run(spec: FlowSpec, ws: Path | None) -> None:
    """Global state of the in-process tools for the active flow (one run at a time)."""
    names, contracts = _artifact_names(spec)
    artifacts.configure(names, contracts)
    git_tool.configure(ws)
    io_tool.configure(spec, ws)


def _env_kind() -> str | None:
    from .. import workspaces
    return workspaces.env_kind()


def _write_policy(spec: FlowSpec, ws: Path | None, conns: dict[str, Any]) -> WritePolicy:
    return WritePolicy(
        flow_autonomy=spec.autonomy,
        agent_autonomy={n: a.autonomy for n, a in spec.agents.items() if a.autonomy},
        workspace=ws,
        # the studio workspace's environment decides (dev/staging/prod), else the flow's
        workspace_env=_env_kind() or (spec.workspace.environment if spec.workspace else "dev"),
        protected_branches=list(spec.workspace.protected_branches) if spec.workspace else [],
        write_connections={n: set(a.write_connections) for n, a in spec.agents.items()},
        read_connections={n: set(a.connections) for n, a in spec.agents.items()},
        connection_env={n: c.environment for n, c in conns.items()},
        connection_actions={n: {k: x.access for k, x in c.actions.items()}
                            for n, c in conns.items()},
        actions_only={n for n, c in conns.items() if c.actions_only or c.type == "website"},
    )


# --- graph mode: one session per agent ---------------------------------------------------------
def compile_agent(spec: FlowSpec, flow_dir: Path, agent: str, *,
                  policy: WritePolicy | None = None, conns: dict[str, Any] | None = None,
                  workspace: Path | None = None, resume: str | None = None,
                  resolve_secrets: bool = True) -> ClaudeAgentOptions:
    """Options for ONE agent as its own session (graph mode). The session is the agent: only its
    tools (closed list in the hook, with a fixed identity), no delegation, `dontAsk`, no user
    settings. `policy` is shared by all the agents of a run (the runner sets its approver)."""
    if agent not in spec.agents:
        raise FlowError(f"agent {agent!r} does not exist in the flow")
    a = spec.agents[agent]
    conns = _connections(spec) if conns is None else conns
    ws = workspace if workspace is not None else _workspace_dir(spec, check=False)
    policy = policy or _write_policy(spec, ws, conns)
    body = _read_prompt(flow_dir, a.prompt, a.prompt_file, f"agent {agent}")
    tools = [_sdk_tool(spec, t) for t in a.tools]
    tools += [t for t in (*io_tools(spec, agent), *action_tools(a, conns)) if t not in tools]

    natives = sorted({str(BUILTIN_TOOLS[t].native) for t in a.tools
                      if t in BUILTIN_TOOLS and BUILTIN_TOOLS[t].native})
    builtin = {BUILTIN_TOOLS[t].server for t in a.tools
               if t in BUILTIN_TOOLS and BUILTIN_TOOLS[t].native is None}
    if io_tools(spec, agent):
        builtin.add("io")
    if action_tools(a, conns):
        builtin.add("connections")
    servers: dict[str, Any] = {s: BUILTIN_SERVERS[s] for s in sorted(builtin)}
    for name, m in spec.mcp_servers.items():
        if any(t.startswith(f"{name}.") for t in a.tools):
            servers[name] = _external_config(name, m) if resolve_secrets else {"type": m.type}

    model = a.model if a.model and a.model != "inherit" else (spec.model or settings.model)
    prompt = (body.rstrip() + _io_section(spec, agent) + _artifacts_section(a)
              + _connections_section(a, conns) + _context_section(spec)
              + _write_section(spec, a, ws) + AGENT_RULES)
    return ClaudeAgentOptions(
        system_prompt=prompt,
        tools=natives,  # [] = no built-in tools at all
        cwd=str(ws) if ws else None,  # also without native tools: `claude --resume` finds it
        mcp_servers=servers,
        allowed_tools=list(tools),
        disallowed_tools=[d for d in DISALLOWED if d not in natives] + sorted(DELEGATION_TOOLS),
        permission_mode="dontAsk",
        setting_sources=[],
        max_buffer_size=MAX_MESSAGE,
        hooks=make_hooks(make_guard(tools, {agent: tools}, {agent: a.connections}, policy,
                                    fixed_agent=agent)),
        max_turns=a.max_turns or DEFAULT_AGENT_TURNS,
        model=model,
        resume=resume,
    )


@dataclass
class CompiledGraph:
    """A flow compiled for the graph engine: the options of each agent and one shared
    `WritePolicy` for the run."""

    spec: FlowSpec
    flow_dir: Path
    policy: WritePolicy
    workspace: Path | None
    agents: dict[str, ClaudeAgentOptions] = field(default_factory=dict)

    @property
    def model(self) -> str | None:
        return self.spec.model or settings.model

    def agent_options(self, agent: str, resume: str | None = None) -> ClaudeAgentOptions:
        return replace(self.agents[agent], resume=resume)

    def fingerprint(self, node: str) -> str:
        """What a node is made of (its definition and the prompt its session gets): a run records
        it when the node finishes, so a resume can tell that the node changed since."""
        a = self.spec.agents[node]
        opts = self.agents.get(node)
        blob = json.dumps({"agent": a.model_dump(mode="json", by_alias=True),
                           "prompt": getattr(opts, "system_prompt", "")},
                          sort_keys=True, ensure_ascii=False, default=str)
        return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:12]

    def activate(self) -> None:
        """Points the in-process tools (artifacts, git, io) at this flow again (another run may
        have used them while this one was paused)."""
        _configure_run(self.spec, self.workspace)

    def register(self, run_id: str) -> None:
        """A nested run (subflow): the io and artifact tools know this flow for `run_id` only,
        while the parent flow stays the active one."""
        artifacts.configure_run(run_id, *_artifact_names(self.spec))
        io_tool.configure_run(run_id, self.spec, self.workspace)

    @staticmethod
    def release(run_id: str) -> None:
        artifacts.release_run(run_id)
        io_tool.release_run(run_id)


def compile_graph(spec: FlowSpec, flow_dir: Path, *, resolve_secrets: bool = True,
                  configure_artifacts: bool = True) -> CompiledGraph:
    conns = _connections(spec)
    _check_subflows(spec, flow_dir)
    ws = _workspace_dir(spec, check=configure_artifacts)
    policy = _write_policy(spec, ws, conns)
    compiled = CompiledGraph(spec, flow_dir, policy, ws)
    for name in (n for n, a in spec.agents.items() if not a.runs_itself):  # no session
        compiled.agents[name] = compile_agent(spec, flow_dir, name, policy=policy, conns=conns,
                                              workspace=ws, resolve_secrets=resolve_secrets)
    if configure_artifacts:
        compiled.activate()
    return compiled


def load_graph(name: str, *, base: Path | None = None) -> CompiledGraph:
    spec, flow_dir = load_spec(name, base)
    return compile_graph(spec, flow_dir)

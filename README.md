# Agent Flow Studio

A local studio to **build, test and run agent flows** with the
[Claude Agent SDK](https://docs.claude.com/en/api/agent-sdk/overview) (Python): flows in YAML, an
n8n/Langflow-style visual editor, a chat-based build assistant, execution with trace, human
approvals, connectors and writes with controlled autonomy.

> Current state and next steps: [`STATUS.md`](STATUS.md).

## Concepts
- **Flow** (`flows/<name>/flow.yaml` + `prompts/*.md`): a graph of **agents** (subagents with a
  prompt, tools, model, skills, artifacts they read/write), **gates** (human approval or stop) and
  **links** with a natural-language condition. An orchestrator receives the graph as instructions.
- **Tools**: `artifacts.*` (intermediate results in `runs/<id>/`, with optional contracts),
  `connections.http_get|http_write` (connectors), `workspace.read|glob|grep|edit|write|bash`
  (files/commands inside the flow's workspace), approved external MCPs and extension tools.
- **Security** (does not depend on the model): closed list of tools per flow and per subagent in the
  hook, no native tools outside the workspace, `dontAsk`, no user settings, secrets outside files,
  audit in `runs/audit.jsonl` (no file contents).

## Visual editor
`agent-flow-studio-editor` opens http://127.0.0.1:8765:
- **Language**: the interface is available in Portuguese, English and Spanish (selector in the
  toolbar; the choice is saved in the browser).
- **Canvas**: drag agents and gates, link nodes, arrange, undo/redo; live validation with the same
  rules as the runtime; only valid flows are saved (copy in `flow.yaml.bak`; delete →
  `flows/.trash/`).
- **Properties**: prompts (inline or file), tools, artifacts and contracts, model, connectors,
  autonomy, workspace, external MCPs.
- **Assistant**: a chat that creates/changes agents, links, prompts, MCPs… in a draft; you review
  and save. It does not approve MCPs or choose `auto` autonomy. It replies in the interface language.
- **Run**: runs the saved flow with an input and shows the active agent, the finished ones and the
  gate where it stopped, the trace, the cost and the artifacts. Modes: *full flow*, *step by step*
  (pause after each agent), *until agent…* and *single agent (isolated)*. Gates and writes ask for
  a live decision.
- **Connectors**: HTTP API (no auth, key, bearer, basic, client credentials), Azure Resource
  Manager (az login, client credentials), Azure DevOps (PAT, az login, client credentials) and
  GitHub (PAT, gh auth login), with environment, allowed paths, read/write access and "Test
  connection". Secrets in the system Credential Manager (keyring) or as `${VAR}`.

## Writes with autonomy
In the flow you set the **autonomy** (approve each write · approve only in production · autonomous)
and the **workspace** (folder/repository, environment, protected branches); each agent can override
the autonomy. When a write needs approval, the run pauses and the editor shows the file and
content, the command or the HTTP request. The hook always denies paths outside the workspace, force
pushes or pushes to protected branches, and destructive commands.

## Local extensions
Tools, contracts and reports for a domain/company live in `extensions/<name>/` (outside git), in a
Python package that exposes `TOOL_GROUPS`, `CONTRACTS` and `REPORTS` (see
[`extensions/README.md`](extensions/README.md) and `src/agent_flow_studio/extensions.py`).

## Getting started
```bash
python -m venv .venv && source .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -e ".[dev]"
cd editor && npm install && npm run build && cd ..      # frontend (Node 20+)
pytest
agent-flow-studio-editor                                # editor at http://127.0.0.1:8765
agent-flow-studio-flows list | validate <flow> | show <flow> | catalog
agent-flow-studio test-001 --flow repo-review           # from the CLI (writes that need approval are denied)
```
Authentication: Claude Code login on the machine or `ANTHROPIC_API_KEY`. Frontend development:
`cd editor && npm run dev` (port 5173, forwards `/api` to 8765).

## Layout
```
src/agent_flow_studio/
  builder/        spec.py (format + validation), catalog.py, compile.py
  editor/         server.py (API), assistant.py, runner.py, validation.py, yaml_io.py, static/ (build)
  connections/    model.py, store.py (keyring/${VAR}), client.py
  tools/          artifacts.py, connections.py, guards.py, common.py
  hooks.py        guard (closed list) + audit · policy.py  writes, workspace, autonomy
  extensions.py   loading of local extensions · registry.py  in-process MCP servers
  main.py · orchestrator.py · flows_cli.py
editor/           frontend (Vite + React + React Flow; strings in src/i18n/)
flows/            flows (example: repo-review)
extensions/       local extensions (outside git, except the README)
tests/            core tests (generic flow in tests/fixtures/flows/pipeline)
```

# Agent Flow Studio

A local studio to **build, test and run agent flows** with the
[Claude Agent SDK](https://docs.claude.com/en/api/agent-sdk/overview) (Python): flows in YAML, an
n8n/Langflow-style visual editor, a chat-based build assistant, execution with trace, human
approvals, connectors and writes with controlled autonomy.

> Current state and next steps: [`STATUS.md`](STATUS.md).

## Concepts
- **Flow** (`flows/<name>/flow.yaml` + `prompts/*.md`): a graph of **agents** (with a prompt,
  tools, model, skills), **gates** (human approval or stop) and **links** with conditions.
- **Typed data**: the flow declares its **inputs** (text, markdown, number, integer, boolean,
  date, datetime, url, path, file, choice, json — with required, default, min/max, pattern,
  options, accept, contract, multiple); the Run panel shows them as a form. Each agent declares
  **inputs** (`from: input.<x>` or `from: <agent>.<output>`) and typed **outputs**, which the
  studio checks when the agent saves them (`io.set_output`): the output of one agent is the input
  of the next. Values live in `runs/<run id>/io/`; the run id is separate from the inputs.
- **Execution** (per flow): `orchestrator` — an LLM orchestrator receives the graph as
  instructions and delegates to the agents (as before); `graph` — the studio runs the graph
  itself: each agent is its own session, independent branches run **in parallel** (up to
  `max_parallel`), a node with several incoming links waits for all of them (`join: any` waits
  for the first), skipped branches propagate, and loops stop at `max_runs`.
- **Conditions** on links: `if` — an expression on the inputs and outputs, evaluated by the studio
  without an LLM (`triage.severity == "high"`, `tests.failed > 0`, `len(scan.items) > 0`,
  `approve.decision == "approve"`); `when` — text, decided by the orchestrator (or by a small
  router call in graph mode). Both on one link: both must hold.
- **Artifacts** (`reads`/`writes`): free files in `runs/<id>/`, still supported.
- **Tools**: `artifacts.*` (intermediate results in `runs/<id>/`, with optional contracts),
  `connections.http_get|http_write` (connectors), `workspace.read|glob|grep|edit|write|bash`
  (files/commands inside the flow's workspace), `workspace.git` (read-only git: status, diff,
  log, show, branch, ls-files, blame), approved external MCPs and extension tools.
- **Security** (does not depend on the model): closed list of tools per flow and per subagent in the
  hook, no native tools outside the workspace, `dontAsk`, no user settings, secrets outside files,
  audit in `runs/audit.jsonl` (no file contents).

## Visual editor
`agent-flow-studio-editor` opens http://127.0.0.1:8765:
- **Language**: the interface is available in Portuguese, English and Spanish (selector in the
  toolbar; the choice is saved in the browser).
- **Theme**: System (follows light/dark of the OS), Light, Dark, Sepia, Ocean and High contrast
  (selector in the toolbar; saved in the browser).
- **Node style**: Cards, Compact or Avatars (selector in the toolbar; saved in the browser); each
  agent can have an icon (built-in set or an emoji). While a run goes, running agents show a
  green dot, a "running" badge and a moving bar; stopped ones show a hollow dot.
- **Canvas**: drag agents and gates, link nodes, arrange, undo/redo; live validation with the same
  rules as the runtime; only valid flows are saved (copy in `flow.yaml.bak`; delete →
  `flows/.trash/`).
- **Properties**: prompts (inline or file), tools, artifacts and contracts, model, connectors,
  autonomy, workspace, external MCPs.
- **Assistant**: a chat that creates/changes agents, links, prompts, MCPs… in a draft; you review
  and save. It does not approve MCPs or choose `auto` autonomy. It replies in the interface language.
- **Run**: a form for the flow inputs (files included) and the run id; it runs the saved flow
  and shows the active agents (several at once in graph mode), the finished and skipped ones, the
  gate where it stopped, the trace, the cost, the outputs of each agent and the artifacts. Modes:
  *full flow*, *step by step*, *until agent…* and *single agent (isolated)* — with a form for that
  agent's inputs. Gates and writes ask for a live decision.
- **Connectors**: HTTP API, Azure Resource Manager, Azure DevOps, Microsoft Graph, GitHub, GitLab
  and Atlassian Cloud (Jira, Confluence), with environment, allowed paths, read/write access and
  "Test connection". Authentication: none, API key, bearer, basic, PAT, client credentials, CLI
  session (az login, gh auth login), Azure managed identity and **sign-in on a page** (OAuth 2
  with PKCE: the provider's page opens, you sign in, and the tokens go to the keyring and renew
  themselves). HTTP API has presets for Facebook, Instagram, Threads, WhatsApp Business and
  Reddit (each needs an app that you register with the provider). Secrets and tokens live in the
  system Credential Manager (keyring) or as `${VAR}`.

## Flow templates
**New** in the editor (or `agent-flow-studio-flows new`) can start from a template; you fill in its
parameters (repository folder, base branch, connector…):
- **code-review** — reviews the current branch against the base branch (read-only git) and writes
  the review as a draft for approval.
- **test-runner** — runs your test command (you approve it first) and diagnoses each failure,
  without changing code.
- **fix-and-pr** — reads an issue (GitHub, Azure DevOps or GitLab) through a connector, fixes it
  on `fix/<issue>`, runs the tests, pushes the branch and, after your approval, opens a draft PR.

Templates live in `src/agent_flow_studio/templates/<id>/` (`template.yaml` + `flow.yaml` +
`prompts/`); `{{param}}` placeholders are filled in on creation. An extension can add its own in
`extensions/<name>/templates/`. All templates start with autonomy "approve each write".

```bash
agent-flow-studio-flows templates
agent-flow-studio-flows new review-app --template code-review --set workspace=D:/repos/app
```

## Agent library
Generic agents you reuse across flows. In the editor, palette → **Agent library**: drag or click
an agent to **copy** it into the flow, then tune it there (prompt, tools, model…); link its inputs
and place it wherever the flow needs it. A copy remembers where it came from (`origin` in
`flow.yaml`), so the node shows a "lib" badge and the properties say whether it was changed in this
flow and whether the library has a newer version. Nothing in a flow changes when the library
changes: **Update from library** (on the node's right-click menu or in its properties) brings the
new version in on request, keeping the input links, join, loops and icon; **Detach** drops the
link. **Save to library…** stores any agent of a flow in your local library.

Sources: built-in generic agents in `src/agent_flow_studio/agents/<id>/` (`agent.yaml` +
`prompt.md`; repo explorer, code reviewer, summarizer), an extension's `extensions/<name>/agents/`
and the local library `agents/` (outside git, or `AFS_AGENTS_DIR`), the only one the editor writes
to. Library inputs have no `from` and the library never keeps autonomy "autonomous".

```bash
agent-flow-studio-flows agents
```

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
Authentication: the agents run through the Claude Code CLI, so they use its login on the machine
(`claude`, then `/login`; separate from the Claude app), or the `ANTHROPIC_API_KEY` environment
variable set before you start the editor or the CLI (`.env` is not read). When the login expires,
the Run panel says so and shows these steps. Frontend development: `cd editor && npm run dev`
(port 5173, forwards `/api` to 8765).

## Layout
```
src/agent_flow_studio/
  builder/        spec.py (format + validation), catalog.py, compile.py, templates.py, library.py
  templates/      flow templates (code-review, test-runner, fix-and-pr)
  agents/         built-in agent library (repo_explorer, code_reviewer, summarizer)
  editor/         server.py (API), assistant.py, runner.py, validation.py, yaml_io.py, static/ (build)
  connections/    model.py, store.py (keyring/${VAR}), client.py
  tools/          artifacts.py, connections.py, git.py (read-only), guards.py, common.py
  hooks.py        guard (closed list) + audit · policy.py  writes, workspace, autonomy
  extensions.py   loading of local extensions · registry.py  in-process MCP servers
  main.py · orchestrator.py · flows_cli.py
editor/           frontend (Vite + React + React Flow; strings in src/i18n/)
flows/            flows (example: repo-review)
extensions/       local extensions (outside git, except the README)
agents/           local agent library saved from the editor (outside git, except the README)
tests/            core tests (generic flow in tests/fixtures/flows/pipeline)
```

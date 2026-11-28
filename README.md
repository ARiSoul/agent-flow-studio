# Agent Flow Studio

[![CI](https://github.com/ARiSoul/agent-flow-studio/actions/workflows/ci.yml/badge.svg)](https://github.com/ARiSoul/agent-flow-studio/actions/workflows/ci.yml)

A local studio to build, test and run **agent flows** with the
[Claude Agent SDK](https://docs.claude.com/en/api/agent-sdk/overview).

You draw a flow of AI agents in a visual editor. The studio runs the flow, shows each step and
stops for your approval before an agent writes. Everything runs on your machine.

- [What it does](#what-it-does)
- [Safety](#safety)
- [Get started](#get-started)
- [Where things live](#where-things-live)
- [Documentation](#documentation)
- [Project layout](#project-layout)

## What it does

| Area | What you get |
|---|---|
| **Flows** | A flow is a graph of agents, gates and links, saved as YAML. Each agent has a prompt, tools, a model and typed inputs and outputs. |
| **Visual editor** | Draw and edit flows on a canvas. The editor checks the flow while you work, with the same rules as the runtime. |
| **Build assistant** | A chat that creates and changes agents, links and prompts in a draft. You review the draft and save it. |
| **Runs** | Run a whole flow, step by step or one agent. See each step, its tool calls, its outputs and its cost. Pause, resume, retry or run again from a step. |
| **Human gates** | A flow stops at an approval gate. You approve, reject or answer, and the flow continues. |
| **Connectors** | HTTP APIs, Azure, Azure DevOps, Microsoft Graph, GitHub, GitLab, Atlassian and websites with your own sign-in. Named actions with typed parameters. |
| **Workspaces** | Group the flows, agents and connectors of one project, with variables and environments (dev, staging, prod). The Board shows the flows and how they feed each other. |
| **Agent library** | Keep generic agents and copy them into flows. A copy shows when the library has a newer version. |
| **Models** | Claude by default. Amazon Bedrock, Google Vertex, Microsoft Foundry or any gateway that speaks the Anthropic API (for example, LiteLLM). |
| **Claude Code** | Continue a step in Claude Code. Start and follow runs from Claude Code through an MCP server. |

The editor is in English, Portuguese and Spanish. It has five themes and can follow the system's
light or dark mode.

## Safety

The rules below do not depend on the model. A guard hook applies them to every tool call.

- **Read only by default.** An agent writes only with the write tools that its flow gives it.
- **Controlled autonomy.** Each write asks for your approval, or only in production. Only a
  person can choose "autonomous", in the editor.
- **Hard limits.** The hook always denies paths outside the workspace, force pushes, pushes to
  protected branches and destructive commands.
- **Secrets.** Keys and tokens stay in environment variables or the system keyring. They are never
  in flows, settings files, the editor or the logs.
- **Closed tool lists.** Each flow and each agent has a fixed list of tools. An external MCP server
  must be approved, read only and limited to named tools.
- **Audit.** Every tool call goes to `runs/audit.jsonl`, without file contents.

## Get started

You need Python 3.10 or later, Node.js 20 or later and the
[Claude Code CLI](https://docs.claude.com/en/docs/claude-code) with a login on your machine.

1. Install the package:

   ```bash
   python -m venv .venv
   .venv\Scripts\activate            # macOS/Linux: source .venv/bin/activate
   pip install -e ".[dev]"
   ```

2. Build the editor:

   ```bash
   cd editor && npm install && npm run build && cd ..
   ```

3. Start the editor:

   ```bash
   agent-flow-studio-editor
   ```

4. Open http://127.0.0.1:8765.

The agents use the login of the Claude Code CLI (`claude`, then `/login`). You can also set
`ANTHROPIC_API_KEY` before you start the editor.

**Optional extras:**
- Website connectors and app previews: `pip install -e ".[web]"` (Playwright).
- Videos and large images: ffmpeg on the PATH, or `pip install -e ".[media]"`.

**Command line:**

```bash
agent-flow-studio-flows list | validate <flow> | show <flow> | templates | agents
agent-flow-studio-flows new <flow> --template code-review --set workspace=D:/repos/app
agent-flow-studio <run-id> --flow <flow> [--workspace <name>] [--env <env>]
```

From the command line, a write that needs approval is denied.

## Where things live

This is a public repository. Your own work stays on your machine by default.

| Item | Default place | In git |
|---|---|---|
| Flows | `flows/<name>/` | No. Only public examples are listed in `.gitignore`. |
| Connectors, model providers | `connections.yaml`, `providers.yaml` | No |
| Local agent library | `agents/` | No |
| Runs, conversations, audit | `runs/` | No |
| Workspaces | `<repo>/.agent-flow/` or `workspaces/<name>/` | In your project's repository, if you choose it |
| Extensions | `extensions/<name>/` | No (use a private repository) |

"Public (git)" is a separate, explicit choice. The studio refuses to make an item public when it
names a word from your `private_terms.txt`.

## Documentation

- [User guide](docs/guide.md): the full reference of every feature.
- [Operations](operations.md): start and stop, update, tests, Claude Code setup.
- [Status](STATUS.md): the current state and the next steps.
- [Extensions](extensions/README.md): your own tools, contracts and reports.

## Project layout

```
src/agent_flow_studio/
  builder/       flow format, validation, compilation, templates, agent library
  editor/        local API (FastAPI): routers in api/, the run engine in runs/
  connections/   connectors, actions, website and OAuth sign-in
  tools/         internal tools (artifacts, connectors, workspace, git, io)
  hooks.py       guard and audit
  policy.py      writes, workspace limits and autonomy
  engine.py      graph engine (parallel branches, joins, loops)
editor/          visual editor (Vite, React, React Flow)
tests/           core tests and end-to-end tests (Playwright)
docs/            user guide
```

Development commands:

```bash
pytest                      # all tests
ruff check .                # lint
cd editor && npm test       # editor unit tests
cd editor && npm run dev    # editor with hot reload on port 5173
```

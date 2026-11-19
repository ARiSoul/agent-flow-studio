# CLAUDE.md — Agent Flow Studio

## First of all: STATUS.md
This project is worked on from several machines and accounts (Claude Code memory is per account and
does not travel between them). **At the start of each session read `STATUS.md`**: it says where the
work stopped and what comes next. **At the end of each task update `STATUS.md`** (current state, next
steps and one line in the log) and propose commit and push, so the other machine can pick it up.
If `CLAUDE.local.md` exists (outside git), follow those local instructions too.

## Language
- **Everything in the repository is in English**: code, comments, docstrings, messages, logs,
  prompts, flows, tests, docs (`README.md`, `CLAUDE.md`, `STATUS.md`…), commit messages and PRs.
- **Chat with the user in Portuguese** (European Portuguese). Only the conversation is in Portuguese.
- The **editor UI is multilingual** (`pt`, `en`, `es`): every user-facing text in `editor/src` goes
  through `t("key")` with the strings in `editor/src/i18n/<lang>.ts`; `en.ts` is the reference and
  the other languages must have the same keys. A new language = a new file + an entry in
  `editor/src/i18n/index.tsx`. Backend messages (validation, errors) are in English.

## Goal
A studio to build, test and run **agent flows** (Claude Agent SDK, Python): flows in YAML, visual
editor (React Flow + FastAPI), chat-based build assistant, execution with trace, human approvals,
connectors (Azure, Azure DevOps, GitHub, APIs) and writes with controlled autonomy. See `README.md`.

## Inviolable rules
1. **Read-only by default.** Writing is only possible with write tools declared in the flow —
   `workspace.edit|write|bash` (inside the workspace) and `connections.http_write` (connectors with
   `access: write` assigned to the agent) — and follows the agent/flow **autonomy** (`approve` ·
   `approve_prod` · `auto`), enforced by `policy.py` in the hook. The orchestrator never writes.
   With no one to approve (CLI), writes that need approval are denied. Drafts live in `runs/<id>/`.
2. **Secrets** only in environment variables/`.env` or the system keyring (connectors) — never in
   code, flows, `connections.yaml`, the UI or logs. In a flow, secrets are always `${VAR}`.
   Do not record PII or file contents in `runs/audit.jsonl`.
3. **Every conclusion has evidence**: each agent cites its source (file:line, request, record).
4. Internal tools follow the pattern in `src/agent_flow_studio/tools/` and are registered in
   `registry.py`; tools for a domain/company go into an **extension** (`extensions/<name>/`,
   outside git — see `extensions.py`).
5. **External MCPs** only enter a flow with `approved: true`, `read_only: true` and a closed list of
   tools (no wildcards). The hook enforces the list per flow and per subagent.
6. **`auto` autonomy** is only chosen by a person in the editor (the assistant only uses `approve` /
   `approve_prod`). Even in `auto`, the hook denies paths outside the workspace, force pushes or
   pushes to protected branches, and destructive commands; Bash is not a sandbox (so it is treated
   as a write).
7. Do not remove the guard hook, `dontAsk`, `setting_sources=[]` or the contract validation.
8. **Public repository:** nothing internal to companies/clients goes into git (code, flows, system
   names, data). That lives in `extensions/`, locally ignored `flows/` and `CLAUDE.local.md`.

## Architecture
- `builder/`: `spec.py` (flow format + validation), `catalog.py` (tools, contracts),
  `compile.py` (flow → `ClaudeAgentOptions`; the orchestrator's "Flow" section is generated from
  the graph).
- `hooks.py` (guard + audit) and `policy.py` (writes, workspace, Bash, autonomy, approvals,
  delegation limit for step-by-step tests).
- `connections/`: profiles in `connections.yaml` (outside git), `keyring`/`${VAR}` secrets,
  `http_get` (read) and `http_write` (write, only with `access: write`).
- `editor/`: `server.py` (local API, 127.0.0.1, local Hosts, `X-Flow-Editor` header on writes),
  `assistant.py` (chat: only changes an in-memory draft), `runner.py` (full/step/until/agent runs,
  gates, live approvals, decisions in `runs/<id>/decisions.jsonl`), `yaml_io.py`, `validation.py`.
- `extensions.py`: loads `extensions/<name>/` (TOOL_GROUPS, CONTRACTS, REPORTS).
- Frontend in `editor/` (Vite + React + @xyflow/react, i18n in `editor/src/i18n/`) → build in
  `src/agent_flow_studio/editor/static/`.

## Conventions
- Python ≥ 3.10, typing, `ruff` (line 100). Flow prompts in `flows/<flow>/prompts/*.md`.
- Tools return `ok(...)`/`err(...)` from `tools/common.py`.
- Core tests use the generic flow `tests/fixtures/flows/pipeline` and must pass without extensions
  (`AFS_NO_EXTENSIONS=1 pytest tests`). Extension tests live in `extensions/<name>/tests`.

## SDK (verified against claude-agent-sdk 0.2.185 / CLI 2.1.328)
Delegation: the `Agent` tool (`Task` is an alias). `@tool` returns `{"content": [...], "is_error"?}`.
Subagents accept MCP tools by their full name `mcp__server__tool`.

## Commands
- `pytest` · `AFS_NO_EXTENSIONS=1 pytest tests` · `ruff check .`
- `agent-flow-studio <input> --flow <flow> [--resume <session_id>]`
- `agent-flow-studio-flows list|validate <flow>|show <flow>|catalog`
- `agent-flow-studio-editor [--port 8765] [--flows <folder>]` · frontend: `cd editor && npm run build`

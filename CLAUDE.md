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

## Editor themes
Colors only through CSS tokens (`var(--bg)`, `var(--accent)`, `var(--on-accent)`…) in
`editor/src/styles.css` — no hard-coded colors in components. Each theme is a
`[data-theme="<id>"]` block with the same tokens. A new theme = that block + an entry in `THEMES`
(`editor/src/theme.tsx`) + its id in `editor/public/theme-init.js` + `theme.<id>` in every i18n file.

No native browser dialogs (`alert`/`confirm`/`prompt`): use `useDialog()` from
`editor/src/Dialog.tsx` (`confirm` for yes/no, `form` for text input with validation). Only the
browser's own "leave page?" warning stays native.

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
   OAuth tokens from a sign-in on a page live only in the keyring; errors never show them.
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
  the graph), `templates.py` (flow templates: `templates/<id>/` with `template.yaml`, `flow.yaml`,
  `prompts/`; `{{param}}` filled in only inside YAML string values, after parsing). Templates are
  generic and public, start with autonomy `approve`, and company templates go in
  `extensions/<name>/templates/`. `library.py` (agent library: `agents/<id>/` with `agent.yaml` +
  `prompt.md`; core in `src/agent_flow_studio/agents/`, extensions in `extensions/<name>/agents/`,
  local in `agents/` outside git). Agents are **copied** into flows with an `origin`
  (`{agent, hash}`); `fingerprint` ignores flow-only fields (`from`, `join`, `max_runs`, icon) and
  the validation returns `origins` (changed here / newer in the library). Library agents have no
  `from` and never `autonomy: auto`.
- Typed data: `builder/types.py` (`IOType`, `check_value`), `builder/expr.py` (safe `if`
  expressions: `ast` with a whitelist, never `eval`), `tools/io.py` (MCP `io`: `get_inputs`,
  `set_output`, `read_file`; values in `runs/<id>/io/`; the hook checks that `agent` is the caller).
  Agents get the `io` tools implicitly from their inputs/outputs.
- Execution per flow (`execution`): `orchestrator` (LLM, `compile_flow`) or `graph` (`engine.py`:
  one session per agent from `compile_agent`, guard with `fixed_agent`, parallel branches, joins,
  `if`/`when`, loops with `max_runs`). Legacy flows (no `inputs`) keep the one `input` that is also
  the run id.
- `tools/git.py`: `workspace.git`, read-only git (closed list of subcommands and options, no
  `-c`/`--output`/external diff, fsmonitor off). Writing with git stays in `workspace.bash`.
- `hooks.py` (guard + audit) and `policy.py` (writes, workspace, Bash, autonomy, approvals,
  delegation limit for step-by-step tests).
- `connections/`: profiles in `connections.yaml` (outside git), `keyring`/`${VAR}` secrets,
  `http_get` (read) and `http_write` (write, only with `access: write`); `oauth.py` = sign-in on
  a page (authorization code + PKCE, single-use `state`, callback `/api/oauth/callback`, tokens
  only in the keyring); managed identity in `client.py`. Provider presets are data
  (`OAUTH_PRESETS` in `model.py`), never code paths per provider.
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
- `agent-flow-studio-flows list|validate <flow>|show <flow>|catalog|templates|agents`
- `agent-flow-studio-flows new <flow> --template <id> --set <param>=<value>`
- `agent-flow-studio-editor [--port 8765] [--flows <folder>]` · frontend: `cd editor && npm run build`

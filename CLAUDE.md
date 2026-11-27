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
  `editor/src/i18n/index.tsx`. Backend messages (validation, errors) are in English, built with
  `msg(code, template, …)` from `messages.py`; each code has a line in
  `editor/src/i18n/server.<lang>.ts` (pt, es) so the editor shows it in the UI language.

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
2. **Secrets** only in environment variables/`.env` or the system keyring (connectors, providers) — never in
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
   `flows/*` is ignored; public example flows are listed one by one in `.gitignore`. Flows, library
   agents and connector definitions can also live in `extensions/<name>/` (`flows/`, `agents/`,
   `connections.yaml`; `compile.flow_locations`, `library.agent_dirs`, `store.connection_files`).
   "Public" is an explicit location (`publish.py`): flows get a `!flows/<name>/` line in
   `.gitignore`, library agents go to the built-in library, connectors to
   `connections.public.yaml`; anything naming a word from `private_terms.txt` is refused.

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
- Typed data: `builder/types.py` (`IOType`, `check_value`; flow inputs may have `show_if` on the
  inputs before them, applied by `runner.prepare_inputs` and `/api/inputs/visible`), `builder/expr.py` (safe `if`
  expressions: `ast` with a whitelist, never `eval`), `tools/io.py` (MCP `io`: `get_inputs`,
  `set_output`, `read_file`; values in `runs/<id>/io/`; the hook checks that `agent` is the caller).
  Agents get the `io` tools implicitly from their inputs/outputs. `previous.<agent>.<output>`
  (in `from` and in expressions) = the outputs of the last finished run of the flow, saved by the
  runner in `runs/.previous/<flow>.json` (not after agent-test runs); `input` and `previous` are
  reserved node names. Gates: `human`, `stop` (optional `message` with `{expression}` parts,
  `expr.render`) and `decision` (no pause; `compile.decide_branch`: first outgoing edge whose
  `if` holds, else the one without `if`; `next_steps` gives `go_to` in orchestrator mode).
  Edges may have a `label`. `trace.py`: the short tool detail and error text in the run log.
  Action nodes: `AgentSpec.action` (`connector`, `name`, `params` with `{expression}` parts,
  `outputs` with `from`/`match`); graph mode only, read actions only (`compile.action_*`); the
  engine runs them with `connections.actions.run` (no session, no prompt; audited with names
  only) and saves the outputs with `io.store_output` (type checks).
- Execution per flow (`execution`): `orchestrator` (LLM, `compile_flow`) or `graph` (`engine.py`:
  one session per agent from `compile_agent`, guard with `fixed_agent`, parallel branches, joins,
  `if`/`when`, loops with `max_runs`). Legacy flows (no `inputs`) keep the one `input` that is also
  the run id. The orchestrator is for short flows: `builder/convert.py` (`orchestrator_limit` =
  a validation warning above `ORCH_MAX_AGENTS` = 3; `graph_notes` = what works differently in
  graph mode; `to_graph` = `execution: graph` + clears go-on `when` texts like "finished", which
  the graph router would be asked about). `POST /api/flows/{name}/to-graph` plans it (nothing
  saved); the editor's Flow properties has "Convert to the graph engine…". New features are
  graph-only; orchestrator resume (old backlog 13) is not planned.
- Files of a run: `io.read_file` (text), `io.view_image`, `io.view_video` (`tools/media.py`: the
  format comes from the content and is forced on ffmpeg with only the file/pipe protocols; MCP
  image blocks). `compile.file_tools` gives them by the input's `accept`; `MAX_MESSAGE` raises
  the SDK's 1 MB message limit (an image over it ended the session). Large uploads are streamed:
  `editor/uploads.py` (`PUT /api/uploads`, `{name, upload}` in the run inputs).
- `tools/git.py`: `workspace.git`, read-only git (closed list of subcommands and options, no
  `-c`/`--output`/external diff, fsmonitor off). Writing with git stays in `workspace.bash`.
- `hooks.py` (guard + audit) and `policy.py` (writes, workspace, Bash, autonomy, approvals,
  delegation limit for step-by-step tests).
- `connections/`: profiles in `connections.yaml` (outside git) with optional `instructions` (how to
  use the system; `compile.py` adds them to the prompt of each agent with that connector, read or
  write; secret-like text is refused; they never widen access) and named `actions`
  (`connections/actions.py`: typed params, templates for path/query/body; `run_action` is given
  implicitly to agents whose connectors have actions; `policy.py` checks the action and its access;
  `actions_only` refuses http_get/http_write on that connector). **Website** connectors
  (`connections/web.py`, optional Playwright): the person signs in in a visible window (profile in
  `~/.agent-flow-studio/browser/<name>`), actions run declared `steps` headless, read actions
  block non-GET requests, navigation stays on the site; the model never clicks freely; `get_json`
  opens the site's JSON in the browser (system certificates) and keeps only the listed fields;
  `redact` masks phones/emails; session cookies are saved from the sign-in window
  (`session-state.json` in the profile) and restored before each action; `recorder.py` records
  a draft action while the person clicks in a visible window (typed values become `{params}`,
  passwords are skipped; the editor shows the draft and the person saves it). Also
  `keyring`/`${VAR}` secrets,
  `http_get` (read) and `http_write` (write, only with `access: write`); `oauth.py` = sign-in on
  a page (authorization code + PKCE, single-use `state`, callback `/api/oauth/callback`, tokens
  only in the keyring); managed identity in `client.py`. Provider presets are data
  (`OAUTH_PRESETS` in `model.py`), never code paths per provider.
- Module layout of the editor backend (split so no file grows past ~500 lines):
  `server.py` = the app (`create_app`: middleware, catalog, the runner and its queue clock, static
  files) + `editor/api/` routers, each a `register(app, ctx)` with its routes (`connectors`,
  `providers`, `flows`, `runs`, `workspaces`; shared bodies/helpers and `Ctx` in `api/common.py`).
  `runner.py` = the `Runner` (state, `start`, the event log) built from mixins in `editor/runs/`
  (`queue`, `history`, `control`, `execution`, `workspace_context`) plus `runs/model.py` (Run,
  statuses, inputs) and `runs/events.py` (saved events, run files); `runner.py` re-exports the
  public names (`__all__`). A new route goes in its router; a new runner method in its mixin.
  Modules that read `settings` are listed in `tests/conftest.py` (the runs-folder safety net).
- `editor/`: `server.py` (local API, 127.0.0.1, local Hosts, `X-Flow-Editor` header on writes),
  `assistant.py` (chat: only changes an in-memory draft; `view_library`, `add_library_agent`,
  `update_from_library` copy library agents into the draft; `propose_library_agent` checks a new
  or edited library agent with `library.check` and the chat shows it with a "Save to library"
  button — the assistant never saves), `commands.py` (saved assistant commands: message +
  instructions in `assistant_commands.yaml` outside git; `/api/assist` takes `command` and
  `with_command` puts its instructions before the message), `ws_assistant.py` (+ `ws_assistant.md`:
  the workspace assistant of the Board, `POST /api/assist/workspace`; read tools over the current
  workspace and `propose_workspace_change` / `propose_flow` / `propose_connector`, checked with
  `WorkspaceSpec`, `_validate` and `Connection` — never `auto`, MCPs unapproved, connectors
  read-only without secrets; the done event carries `proposals`, the editor's cards apply them;
  chat history key `@workspace`; `propose_flow` fills a short spec with `fill_defaults` and
  returns all problems as JSON with where they are, and refuses a flow with `warnings`).
  `validation.unused_inputs` gives the `warnings` of a validation (a flow input no agent declares
  and the task does not name; editor: ⚠ in the Problems tab, not an error). Both assistants stream through
  `assistant.stream`: `turn` events (n of `MAX_TURNS` = 60) and, at the limit, a `done` with
  `limit: true` instead of an error (the editor offers Continue in the same session), `chats.py` (conversation history per
  flow in `runs/chats/<flow>/<id>.json`: the editor saves its items + session id after each reply;
  `/api/chats`), `runner.py` (full/step/until/agent runs,
  gates, live approvals, decisions in `runs/<id>/decisions.jsonl`; `ask` = ask an agent for more
  after a run; `past_runs`/`open_past` = open a saved run again from its `events.jsonl`; `run_from` (mode `from`: any step of a saved run, even done,
  via `prepare_resume(start_at=…)`, `given` = by-hand inputs) and `rename` (`run_meta.json`),
  `resume_plan`/`resume`
  (`GET /api/runs/<id>/resume-plan`, `POST …/resume`) go on with a graph run that ended in error or
  was stopped: the engine is rebuilt with the flow as it is now plus `_restore_engine` (saved
  events), then `GraphEngine.prepare_resume` keeps the done nodes and resets the failed/interrupted
  ones and everything after them (`forward_reach`); a finished node's `agent_done` carries `fp`
  (`CompiledGraph.fingerprint`) so nodes edited since are flagged, repeated unless `reuse_changed`), `yaml_io.py`, `validation.py`.
- Runs and the Run panel. One run goes at a time across the studio (one io/artifact registry).
  **Queue** (`Runner.enqueue/queue/unqueue/start_queued_now`, `/api/runs-queue`, saved in
  `runs/.queue.json`): a queued run starts when `holds()` is empty — no run goes, and no run of its
  workspace waits for the person (`waiting()` without gates older than `STALE_GATE` or dismissed in
  `run_meta.json`; a decision being changed holds any workspace). **Decisions are never refused**:
  decided while another run goes → status `decided` (an `end` event with the decision, restored by
  `open_past`), it goes on first when nothing runs; it can be changed (`hold` = the run waits up to
  `HOLD_FOR`, `supersedes` in decisions.jsonl). The server clock (`queue_clock`, lifespan, every
  `QUEUE_TICK`) calls `Runner.tick()` (restores the queue and decided runs once, then moves them).
  **Retry** (`FlowSpec.retry`, graph): a step whose session ends with a model API error
  (`engine.is_transient`, same regex as `TRANSIENT` in runSteps.ts) runs again after a doubling
  wait, never after a write that ran (`retry_skipped`); `retry_control` = now/cancel.
  **Pause** (graph, agent nodes): `Runner.pause(run, node?)` → `GraphEngine.pause` cancels the
  agent tasks, keeps the live session id (`live_sessions`, from the SDK messages), emits `paused`
  and ends the run `paused`; their waiting write approvals end unanswered. `continue_` runs the
  paused nodes with `resume=<session>` and `AFTER_PAUSE` (outputs kept, no new attempt; the
  delegate has `after_pause`); `_restore_engine` restores `engine.paused` after a restart.
  `pricing.py` (list prices) gives a live `est` on `usage` events. The run states are written once
  in `editor/tests/fixtures/run_status.json` (checked against `Status` and the UI texts); the
  shape of `waiting()` in `editor/tests/fixtures/waiting.json`. Editor: `RunPanel.tsx` (header,
  views, actions via `runActions.ts`), `RunSetup`, `RunAttention` (cards: writes, retry, decided,
  gate, change of decision), `RunStepsView`, `RunLog` + `runLines.ts`, `RunHistory` (totals per
  period, Compare), `RunResults`, `attention.ts` (one count of what waits for you), `runSteps.ts`,
  `runLink.ts` (`#flow=&run=&step=`), `useRunStream.ts`; pure parts tested with `npm test`.
- Reviewing runs. `changes.py`: a `snapshot` of each git root at the start (`start` event `git`:
  head, branch, files already dirty with a content hash — `hash-object` without `-w`, nothing
  written) and `changes` (files that differ since; a file dirty before only when the run changed it
  again); `GET /api/runs/{input}/changes`, editor `RunChanges.tsx` + `diffLines.ts`.
  `GET /api/runs/{input}/files` and `/file?path=` (only `files/`, `outputs/`, `inputs/`; type from
  the bytes; SVG/HTML as text; nosniff + sandbox CSP); `OutputViewer` shows images, videos, code.
  `tools/preview.py` (`preview.screenshot`, write_local; the hook checks `agent` like `io`): the
  workspace's `preview.url` (localhost only) headless, a PNG in `runs/<id>/files/<agent>/` and the
  console errors; the page stays on its origin. The editor CSP allows framing/connecting only
  `localhost`/`127.0.0.1` (the Preview tab, `RunPreview.tsx`).
- Claude Code. Agent sessions get the workspace as `cwd` (also without native tools) so
  `claude --resume` finds them; `claude_sessions.session_cwd/resume_command/open_terminal`,
  `GET …/steps/{node}/session`, `POST …/steps/{node}/terminal`. `studio_mcp.py`
  (`agent-flow-studio-mcp`, stdio, MCP 2 `MCPServer`) calls the local editor API: list_flows,
  flow_inputs, start_run (queues on 409), run_status, run_outputs, run_changes — never a tool that
  decides a gate or approves a write.
- `workspaces.py`: studio workspaces (`workspace.yaml`: roots, vars, environments with a `kind`;
  in a repo `<repo>/.agent-flow/` or in the studio `workspaces/<name>/`; registry
  `~/.agent-flow-studio/workspaces.json`). The current workspace/environment is a ContextVar
  (`current()`, `using()`); the editor sends it in the cookies `afs_ws`/`afs_env` (middleware in
  `server.py`), runs keep theirs for life, the CLI takes `--workspace`/`--env`. The studio's own
  workspace (`studio`) is today's behaviour. A workspace layers its `flows/`, `agents/` and
  `connections.yaml` over the studio ones (keyring keys `ws:<ws>/<conn>/<field>`); `{vars.x}`/
  `{roots.x}` are substituted when a flow loads (`substitute`; `if` keeps `vars.x` as names);
  the environment kind drives `approve_prod`. Machine state per workspace via `scoped()`
  (`runs/@<ws>/<id>`, chats, `.previous`, connector tests, browser profiles).
  `editor/copying.py`: `copy_plan`/`copy`/`duplicate` across workspaces (dependency check; secrets
  never in files, "same secrets" copies the keyring entry). Hand-off: flow input `source` hint,
  `Runner.flow_outputs`/`output_value`, `io/_handoff.json`. Subflow nodes (`AgentSpec.subflow`,
  graph only): `compile._check_subflows` (same workspace, graph, no human gates, no cycles, depth
  ≤ 3), `engine._subflow_node` (child `GraphEngine` with `nested=True` on `<run>--<node>`, the
  flow registered per run id in `io`/`artifacts`, shared approver, events tagged `sub`).
- `context.py`: the workspace context (`context.md` with one marked section per root + `notes`,
  `context.json` with the commit per root) in the workspace folder; `compile._context_section`
  adds it to every agent prompt of a workspace flow (`FlowSpec.use_context`). The context keeper
  is a flow built in code (`keeper_flow(root)`: one agent, read-only `workspace.*` tools on that
  root, typed outputs `section`/`summary`); `Runner.update_context` / `_context_steps` run it per
  root and the studio stores the section (the model never writes files). After a done full run,
  `FlowSpec.context_update` (`suggest` default · `always` = last system step in the same run ·
  `never`). `copying.move` = copy then remove the original (only the workspace's own items).
  A root's **history** (`<root>.history` section, ≤ `MAX_HISTORY`, last in the prompt) comes from
  past Claude Code sessions: `claude_sessions.py` lists/reads them with the SDK
  (`list_sessions`, `get_session_messages`; text only, reminders stripped, secrets masked; kinds
  person/studio/program — the studio tags its own sessions with `mark_ours`), and
  `Runner.learn_context` runs `context.reader_flow()` per source (no tools) then
  `historian_flow(root)` (read-only tools) and keeps a **draft** in `runs/@<ws>/.context/`; the
  person saves it (`apply_draft`, records the learned sources) or discards it.
- `providers.py`: model providers as data (`providers.yaml` outside git +
  `extensions/<name>/providers.yaml`; kinds `anthropic`·`bedrock`·`vertex`·`foundry`·`gateway`).
  A model `<provider>/<model>` → `compile` sets the session `env` (`session_env`: the CLI's own
  switches or `ANTHROPIC_BASE_URL` + token; always blanks the other Anthropic keys/switches);
  keys only `${VAR}`/`keyring`, read only when a session starts (validation never reads them).
  An unknown provider prefix is a validation error. Orchestrator flows: one provider for all
  agents. Gateway sessions have `cost: None` (`CompiledGraph.unpriced`, `CompiledFlow.priced`).
  API `/api/providers` (GET, PUT/DELETE `{name}`, PUT `{name}/key` = keyring value or env name,
  POST `{name}/test` = `GET /v1/models`, nothing generated); editor `Providers.tsx`, opened from
  every model field (`openProviders`); the assistant's `view_catalog` lists the providers.
- `extensions.py`: loads `extensions/<name>/` (TOOL_GROUPS, CONTRACTS, REPORTS).
- Frontend in `editor/` (Vite + React + @xyflow/react, i18n in `editor/src/i18n/`) → build in
  `src/agent_flow_studio/editor/static/`. `App.tsx` is the Editor (state, canvas handlers, layout)
  with its parts as hooks in `editor/src/appParts/` (`graph`: canvas nodes/links from the flow;
  `nodeActions`, `libraryActions`, `menus`, `flowFiles`); each hook takes a `Pick<EditorCtx, …>`
  (`appParts/shared.tsx`) of exactly what it uses. `Inspector.tsx` picks the panel; the panels
  are in `editor/src/inspectorPanels/` (`AgentPanel`, `GatePanel`, `FlowPanel`, `shared`). Folder
  names never equal a file name in another case (`App.tsx` vs `app/` look alike on Windows).

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
- CI: `.github/workflows/ci.yml` (push to main, pull requests): ruff + core tests on Linux
  (Python 3.10, 3.13) and Windows (3.12), editor typecheck + `npm test` + build. Tests never
  need a real model, secrets or extensions; a test that needs git, ffmpeg or Playwright skips
  when it is missing. Job `e2e`: builds the editor, installs Playwright's Chromium and runs
  `tests/e2e` with `AFS_E2E_REQUIRED=1` (missing browser/build = failure, not a skip).
- E2E (`tests/e2e`, marker `e2e`): the real API in a uvicorn thread on a free port + the built
  editor in headless Chromium (Playwright sync API; the browser fixture is module-scoped — its
  event loop must not outlive the module), a `FakeModel` as the SDK `query`, English UI via
  localStorage; each test starts on the loaded `parallel` flow. Writes go through the real guard
  hook (`FakeModel.writes`: the hook from the session options decides, the file is written only
  when it lets the call through), on the `dev` test flow (workspace `tmp_path/repo`). Find
  elements by role and accessible name (as a person would); a change to the main paths of the UI
  (run, gate, pause, stop, writes, providers) updates these tests.
- Website connectors: `pip install -e ".[web]"` (Playwright; uses the installed Edge/Chrome)
- `agent-flow-studio <input> --flow <flow> [--resume <session_id>] [--workspace <ws>] [--env <env>]`
- `agent-flow-studio-flows list|validate <flow>|show <flow>|catalog|templates|agents`
- `agent-flow-studio-flows new <flow> --template <id> --set <param>=<value>`
- `agent-flow-studio-editor [--port 8765] [--flows <folder>]` · frontend: `cd editor && npm run build`

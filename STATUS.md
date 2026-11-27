# STATUS — shared status

Hand-over file between machines/accounts. **When you start:** read this. **When you finish a task:**
update "Current state" and "Next steps", add a line to the "Log" (date, machine/account in generic
terms — no emails —, what was done) and commit + push.

## Where we stopped (2026-11-27, home machine / account B)
- **Backend split** (no behaviour change): `editor/server.py` (1895 lines) → the app + routers
  in `editor/api/`; `editor/runner.py` (2112 lines) → the `Runner` + mixins and helpers in
  `editor/runs/`. Same 106 API routes; all tests pass; a new test caught and keeps fixed the
  built editor path (`api/common.STATIC`). Next suggested: CI (GitHub Actions: ruff, pytest, tsc,
  npm test, build), then Playwright E2E for the main paths, then a real provider check.
Notes from real use on the work machine (a PBI flow: analyse, plan, implement, test, review),
three fixed (not committed when this was written; see the Log):
- **Markdown tables** in the output viewer and on gates: a table right after a line of text (no
  blank line), single-dash separators, no outer pipes, `\|` and pipes in code. The parser is now
  `editor/src/markdownParse.ts` (tested in `editor/tests/markdown.test.ts`).
- **Autonomy changes reach the runs**: saving the flow updates the write policy of its runs in
  memory (also mid-step: `Runner.refresh_autonomy`), continue and gate decisions take it too, and
  a waiting write approval the new autonomy no longer asks for is approved (logged "by autonomy").
  Resume and re-run already reloaded the flow. **The likely cause of what was seen**: the agents
  had their *own* autonomy, which wins over the flow's — Properties now lists them with
  "Use the flow's autonomy for all".
- **workspace.git** when the workspace folder is not a repository: it uses the repositories one
  level down and the workspace roots that are repositories; `repo` (a root name or a sub-folder)
  chooses when there are several (the error lists them).
- **Pause** (backlog 14, done): Pause in the Run panel header (all running agents) or "Pause this
  step" in a running step's ⋯ menu (graph runs). The agent's session stops where it is, its
  outputs so far stay, the other branches go on, and the run ends `paused` with a "Paused by you"
  card. Go on resumes each paused agent in its own SDK session (`resume`) with a short "go on from
  where you stopped" message, with the flow as it is now (autonomy refreshed). Works after an
  editor restart (`_restore_engine`). Not for action or subflow nodes, nor orchestrator runs.
  Not tried yet with a real model.
- **Execution modes are explained** in Flow properties: both modes side by side, what only the
  graph engine has (resume, run from a step, retry, pause, action/subflow nodes) and when the
  orchestrator fits. New blank flows and the chat assistant now default to `graph` (templates
  already did); flows without `execution` still mean orchestrator.
- **Other models and providers** (backlog 15, step 1 done): `providers.yaml` (see
  `providers.example.yaml`), models `<provider>/<model>`, the session env per agent (Bedrock,
  Vertex, Foundry through the CLI switches; anything else through an Anthropic-API gateway such as
  LiteLLM), keys only `${VAR}`/keyring, the machine's Anthropic key blanked for other providers,
  unknown cost on a gateway, the picker and a note in Properties; CLI `providers` and
  `provider-key`; the **Providers window** (from "Providers…" under every model field: add,
  change, check, delete, key in the keyring or an env var) and the assistant's catalog. **Not
  tried with a real provider yet.** Not done: opening a provider session in Claude Code
  (`claude --resume` uses the default sign-in), the assistants and the `when` router stay on the
  default model.

## Where we stopped earlier (2026-11-26, night, home machine / account B)
**Reviewing runs like in Claude Code** (on top of the Run panel rewrite, commit `51b16fc`). After
you pull: stop the editor, `pip install -e ".[dev,web]"` (new entry point `agent-flow-studio-mcp`;
`mcp>=2` was already there through the SDK), `cd editor; npm run build`, restart the editor.
- **Changes**: what a run changed in its workspace's git roots (Current → *Changes*, and on a
  gate); a snapshot at the start, read only.
- **Previews**: run files (images, video, code) in the Run panel; image outputs shown as images.
- **Game preview**: workspace setting `preview.url` (localhost) → *Preview* tab; tool
  `preview.screenshot` for agents (headless Chrome/Edge with WebGL, PNG + console errors).
- **Claude Code ↔ studio**: step ⋯ → Continue in Claude Code (`claude --resume` in a terminal);
  `claude mcp add` (see operations.md) drives the studio from Claude Code
  (no tool decides gates or writes).
- **Checked**: 627 backend tests (`tests/test_changes.py`, `test_run_files.py`,
  `test_preview.py` with a real headless browser, `test_claude_code.py`), 31 editor tests, the
  browser on the check server (Changes on a gate, the sprite preview, the Preview tab).
  **Not tried for real**: the MCP server inside Claude Code, the terminal button on this machine,
  `preview.screenshot` on the real game.
- **Next**: a species flow for "Bugs in the Box" in the game's own workspace (outside this repo):
  a planner, a writer with workspace write tools (approve), a checker with `preview.screenshot`,
  a human gate with the Changes.

## Where we stopped earlier (2026-11-26, later, home machine / account B)
**Run panel rewrite** (not committed when this was written; see the Log). Twelve review rounds by
three critic agents (AI automation, DevOps, developer) until all scored ≥ 9.8. After you pull:
`pip install -e ".[dev,web]"` (stop the editor first), `cd editor; npm run build`,
restart the editor. New: `npm test` in `editor/` (Node 23.6+ runs the TypeScript tests directly; no new packages).

- **Panel**: views New run / Current / History; a header that folds on scroll; the run in the URL
  (`#flow=&run=&step=`); one count of what waits for you (`attention.ts`: Run tab badge, page
  title, notifications); a popover of the other waiting runs (older than a day or dismissed apart)
  and of the queue.
- **Steps / Log / History**: what each step received, attempts, live tokens and a "≈ $" estimate
  (`pricing.py`), tool calls read on demand from the local session (never stored), recovered
  failures in amber; a compact log (one row per event where it fits, toggles kept); History totals
  per period (7/30 days/all) with the period before, Compare with a run picker, deltas and steps.
- **Retry** (`retry: {transient: 2, wait: 15}` in a graph flow, Properties → Retry): model API
  errors only; never after a write that ran; cancel or run now from a card.
- **Queue** (`runs/.queue.json`): one run at a time; a new run waits until no run goes or waits
  for you; "Start anyway"; marked "from the queue" in History.
- **Decisions never refused**: while another run goes a decision is kept (status `decided`, kept
  across restarts) and goes on first; it can be changed (the run is held up to 15 min, a
  superseding record in decisions.jsonl).
- **Checked**: 617 backend tests, 31 in `tests/test_run_history.py`; 30 editor tests; the browser
  on a check server with a fake model. **Not tried with the real model**: retries on a real 529,
  the live cost estimate against real prices, the queue with real runs.
- **Known limit (by design)**: one run at a time across the studio — the engine activates one
  flow's io/artifact registry at a time; per-workspace slots need that registry per run first
  (backlog).

## Where we stopped earlier (2026-11-26, work machine / account A)
The commit after `eeab0e5` holds this session (pushed). It builds on the home machine's two commits
(history from past Claude Code sessions, the model on agent cards).

**After you pull on the other machine**
1. **Stop the editor first**, then `.\.venv\Scripts\python -m pip install -e ".[dev,web]"` (a
   running `agent-flow-studio-editor.exe` is locked on Windows: pip stops halfway; if the package
   disappears from the venv, delete `.venv/Lib/site-packages/~gent_flow_studio-*` and install
   again). No new dependencies.
2. `cd editor; npm run build; cd ..` and **restart** `agent-flow-studio-editor` (new routes:
   `/api/runs/<id>/resume-plan`, `/resume`, `/title`, run mode `from`).
3. `.\.venv\Scripts\pytest` — all pass. `tests/test_learn_sessions.py` **skips** 5 tests when the
   Windows temp path is too long (long paths disabled: the fake session file goes over 260
   characters); with a short `--basetemp` (e.g. `D:\ptmp`) all 10 pass. Not a studio limit.

**What this session added (2026-11-26)**
- **Resume a run** that ended with an error or was stopped (graph mode): a Run panel card lists
  what is kept and what runs again (the failed/interrupted step and what depends on it); works on a
  run opened from "Past runs" after a restart. Each finished step records a fingerprint (`fp` in
  `agent_done`): steps edited since they ran are flagged and repeated, unless you tick "keep".
  A failed attempt does not count for `max_runs`.
- **Run from here** (node context menu, or mode *From a step* in the Run panel): continue a saved
  run, even a done one, from any step; that step and everything after it run again, the earlier
  steps keep their work (same run folder and log). The step's inputs: use what the earlier steps
  saved (shown as a short list) or define them by hand (prefilled). Waiting gates are dropped.
- **Rename runs**: ✎ in "Past runs" (a name only, `runs/<id>/run_meta.json`; folder and id stay).
- **Human gates (graph mode)**: *Answer and continue* with no matching link now asks the gate
  again with a reason (it used to end the run `done` with everything after the gate skipped); a
  reject with no link still ends the run. An agent input can be `from: <human gate>.comment` (or
  `.decision`): empty until asked, never "missing" — so a loop back to a planner gets the answers.
- **Unused flow inputs**: the validation returns **warnings** (⚠ in the Problems tab, not
  blocking) for a flow input that no agent declares (`inputs: {x: {from: input.x}}`) and the task
  does not name; `propose_flow` refuses such a flow; both assistant prompts state the rule.
- **Run panel layout**: the setup area scrolls on its own (max 60% of the panel) with the run id
  and **Run** button always on top, so long agent-input forms no longer cover the log.

**How it was checked**: unit/API tests (`tests/test_resume.py`, `tests/test_gate_answer.py`,
warnings in `tests/test_assistant.py` / `tests/test_ws_assistant.py`) and the browser on seeded
saved runs (resume card, run-from form, rename). **Not tried with the real model yet**: resume,
run from here, the gate that asks again, the `feedback` loop.

**Found with real use (work machine, local flows outside git)**: a PBI-implementation flow made
by the workspace assistant had (a) a flow input no agent received, (b) a gate whose links did not
handle *answer*, (c) a planner prompt using an undeclared artifact — all fixed in that local flow
and, for (a) and (b), in the studio. The Azure DevOps connector there showed the next bug below.

**Known bug, not fixed yet**: connector `allowed_paths` are plain prefixes — a `*` is taken
literally (so `/<project>/_apis/wit/*` allows nothing), and "Test connection" fails with "path not
allowed" when the type's test path (Azure DevOps: `/_apis/projects`) is not in the list. The studio
should refuse `*` (or treat it as one path segment) and say which path the test used. Backlog 12.

**Next suggested task**: on the work machine, run the PBI flow with the real model and use
**Run from here** / **Resume** on it; then fix backlog 12. Still open from the home machine: try
Board → Context → **Learn** and the workspace assistant with the real model.

**Earlier hand-overs (summary; details in the Log)**: 2026-11-20 agent library, assistant
commands/history, connector actions and website connectors, local/extension/public locations;
second machine: run links on the canvas, server messages in the UI language, action recording,
decision/stop-message nodes, `previous.*`; 2026-11-22 studio workspaces (roots, vars,
environments, Board, hand-off, subflows, Copy to…, workspace context); 2026-11-24 images and
videos for agents; 2026-11-24/25 workspace assistant, 60 assistant turns with Continue, Board top
bar fixes, workspace name rules, workspace connectors; 2026-11-25/26 (home machine) history from
past Claude Code sessions (Board → Context → Learn, draft saved by the person), the model on agent
cards and in the node context menu.

## Current state
Works and is tested (`pytest`: core 583 tests without extensions (567 pass, 16 skip) — some skip without the `media` extra or with long temp paths; `ruff` clean; `npm run build`):
- **Repository in English**: code, messages, prompts, flows, tests and docs. Only the chat with the
  user is in Portuguese (rule in `CLAUDE.md`).
- **Multilingual editor UI** (`pt`, `en`, `es`): selector in the toolbar, saved in the browser
  (`afs.lang`). Strings in `editor/src/i18n/<lang>.ts` (`en.ts` is the reference; `tsc` fails if a
  language misses a key). The assistant replies in the UI language. Backend messages are English text plus a code and
  parameters (`messages.py`: `msg(code, template, …)`, `MsgError`); the editor shows them in the UI
  language with `editor/src/i18n/server.<lang>.ts` (English: the server text). A new message = a
  `msg()` call + a line in each `server.<lang>.ts` (`tests/test_messages.py` checks it).
- **Editor themes**: System, Light, Dark, Sepia, Ocean, High contrast (`afs.theme`). Colors only
  through CSS tokens in `editor/src/styles.css`; `editor/public/theme-init.js` avoids a flash.
- **Node styles**: Cards, Compact, Avatars (toolbar, `afs.nodeStyle`); each agent can have an
  `icon` (20 built-in icons in `editor/src/icons.tsx`, or an emoji).
- **In-app dialogs** (`editor/src/Dialog.tsx`), no native `alert/confirm/prompt`.
- **Context menu** (`editor/src/ContextMenu.tsx`) on nodes, links and the canvas; Shift+F10 /
  context-menu key; Ctrl+C / Ctrl+V / Ctrl+D / F2 on the selected node.
- **Flows in YAML** (`flows/<name>/flow.yaml`): agents, gates (human approval / stop), links with
  conditions, artifacts with contracts, approved external MCPs, workspace and autonomy.
- **Typed inputs/outputs**: flow `inputs` (text, markdown, number, integer, boolean, date,
  datetime, url, path, file, choice, json) → a form in the Run panel; the run id is separate and
  generated. Agent `inputs` (`from: input.x` / `<agent>.<output>`) and typed `outputs`, checked on
  save (`io.set_output`), stored in `runs/<id>/io/`. Typed ports and dashed data links on the
  canvas (drag an output to an input), IO editors in the properties panel.
- **Two execution modes per flow**: `orchestrator` (an LLM follows the graph) and `graph`
  (`engine.py`: one session per agent, parallel branches up to `max_parallel`, joins all/any, skip
  propagation, loops with `max_runs`, human/stop gates). Edge conditions: `if` (safe expression,
  `builder/expr.py`) and/or `when` (text; a small router call in graph mode).
- **Run** in the editor: typed run form; modes full / step by step / until an agent / single agent
  (one form with that agent's inputs and where each value comes from; flow inputs optional there);
  live trace; run state on the canvas (filled green dot + "running" badge + moving bar while an
  agent runs, hollow dot when stopped, red-bordered when interrupted by Stop); links followed in
  the run are green with `✓` (moving while their target runs), links not followed are faded with
  `✗`, a failed `if` is red (graph mode: the engine's `edge` events; orchestrator mode: worked out
  from the order of the agents and gates); outputs view;
  artifacts; gates with a decision; live write approvals; a card with the `claude` → `/login`
  steps when the CLI is not signed in.
- **Resume / run from a step / rename runs** (graph mode): Resume a run that failed or was stopped, Run from here (any step of a saved run, even done; inputs from the saved outputs or by hand), a name per saved run.
- **Gates**: *answer* with no matching link asks again; agents can read `<gate>.comment|decision`.
- **Validation warnings**: flow inputs that no agent receives (⚠, not blocking).
- **Workspace assistant** in the Board (Assistant tab): reads the workspace and proposes settings,
  new flows and connectors as cards the person applies; never writes.
- **Assistant limits**: 60 turns per message, turn counter, Continue at the limit (both assistants).
- **Chat assistant** in the editor: builds/changes the flow in a draft (never saves on its own);
  knows typed IO, `if` conditions, execution modes, icons, the agent library (copy, update,
  propose new/edited library agents) and the connectors' instructions and actions. Saved
  commands (message + instructions, favorites), conversation history per flow (`runs/chats/`,
  resumable), Markdown replies.
- **Connectors**: HTTP API, Azure ARM, Azure DevOps, Microsoft Graph, GitHub, GitLab, Atlassian
  Cloud and **Website** (your sign-in in a browser, Playwright optional); secrets in the keyring or
  `${VAR}`; test connection; `http_get`/`http_write` per agent; **instructions** for the agents;
  named **actions** with typed params (`run_action`, checked by the policy; "only these actions";
  website actions run declared steps or `get_json`, `redact` for phones/emails; **Record**
  writes a draft action from your clicks in a visible window).
  **Sign-in on a page** (OAuth 2 + PKCE, tokens only in the keyring) with presets for Facebook,
  Instagram, Threads, WhatsApp Business, Reddit; **Azure managed identity**. Tested with mocked
  providers only.
- **Writes with autonomy** (`approve` / `approve_prod` / `auto`) and a confined workspace; the hook
  denies paths outside the workspace, force pushes/pushes to protected branches and destructive
  commands. **`workspace.git`**: read-only git.
- **Where things live**: local by default (`flows/*` ignored by git, `agents/`, `connections.yaml`,
  `assistant_commands.yaml`, `runs/`); an extension (`extensions/<name>/flows`, `agents`,
  `connections.yaml`) or, explicitly, **public (git)** (`publish.py`); the location tag next to the
  flow picker moves a flow; `private_terms.txt` blocks anything public that names those words.
- **Editor**: resizable and hideable panels (Ctrl+B / Ctrl+J / Ctrl+Alt+B), last open flow
  remembered, save with problems after a confirmation, `show_if` fields in the run form.
- **Agent library**: generic agents (`builder/library.py`; core in `src/agent_flow_studio/agents/`,
  extensions in `extensions/<name>/agents/`, local in `agents/`) copied into flows with an `origin`;
  the editor shows changed-here / newer-in-library and can update, detach or save to the library;
  the library window (palette → Manage) edits, duplicates, moves and deletes agents.
- **Flow templates** (New → "Start from", or `agent-flow-studio-flows new --template`):
  `code-review`, `test-runner`, `fix-and-pr`, `ticket-triage` (typed IO, graph mode); extensions can
  add templates.
- **Local extensions** (`extensions/<name>/`, outside git) for domain tools/contracts/templates.
  `AFS_MOCK` only affects extension tools (the core never uses it).
- Public example flow: `flows/repo-review` (repository review, read only, graph mode).
- **Studio workspaces** (`workspaces.py`, `editor/copying.py`, `Workspaces.tsx`, `Board.tsx`):
  workspace + environment selectors in the top bar (cookies `afs_ws`/`afs_env`); a workspace
  lives in a repo (`<repo>/.agent-flow/`) or in the studio (`workspaces/<name>/`, ignored by git);
  registry in `~/.agent-flow-studio/workspaces.json`. `roots`, `vars` and `environments` (kind
  dev/staging/prod drives `approve_prod`); `{vars.x}`/`{roots.x}` in flows and prompts, `vars.x`
  in conditions, `workspace: {root: app}`. The workspace's flows, agents and connectors come
  first (keyring per workspace); runs, chats, `previous` per workspace (`runs/@<ws>/`).
  Board view (cards, source/subflow/hand-off links, settings panel). Hand-off: `source` hint on
  flow inputs, "Fill from a run…", `io/_handoff.json`. Subflow nodes (graph mode, no human gates
  inside, depth ≤ 3, no cycles). "Copy to…" with a dependency check for flows, flow agents,
  library agents, connectors, variables/environments; "Duplicate workspace"; secrets never in
  files ("use the same secrets" copies the keyring entry). Assistant: `view_workspace`,
  `set_agent(subflow=…)`, `source` hints. CLI: `--workspace`/`--env`,
  `agent-flow-studio-flows workspaces`.

**Note for the other machine (from the translation):** local flows and the extension there may
still have Portuguese prompts/texts (outside git). Defaults changed: `input.label` "Entrada" →
"Input", `task` "Executa o flow para {input}." → "Run the flow for {input}.", the orchestrator
section "## Fluxo" → "## Flow", the validation API key `fluxo` → `flow_section`; the assistant tools
have English names. No YAML key, enum value, tool id or env var changed, so old flows still load.

## Next steps (backlog, in suggested order)
1. [ ] Run `ticket-triage` end to end with the model and a real connector (work machine), then the
   other templates and a graph flow (check `tools=[]` per agent session, the router, parallel
   sessions); tune the template prompts. Try the assistant's library tools in a real chat, and
   the workspace assistant (Board → Assistant) with the real model.
2. [ ] Website/API actions, next steps: ~~record the steps by clicking on the site~~ (done
   2026-11-20: Connectors → website → Record; try it with a real site); the assistant
   proposes actions for a connector (as proposals the user saves); a "guided" mode (the model
   browses read-only following instructions, URL allow list, screenshots in the audit).
3. [x] Canvas: highlight followed / not followed edges (done 2026-11-20, second machine).
4. [ ] Try a real sign-in per provider (register an app, check the presets). Instagram and Threads
   only accept an https redirect URI, so they need an https tunnel or a hosted editor.
5. [ ] Writes through external MCPs (today read only).
6. [ ] More flows/templates, library agents and skills (phase 4 of the builder).
7. [x] Translate backend messages (validation, errors, input reasons such as "required") to the
   UI language (done 2026-11-20, second machine).
8. [ ] Make building flows much easier. The first real flow by hand (website action → compare
   with the last run → message) took many rounds; with the assistant it worked. Ideas: starters
   for common patterns ("check a value and compare it with the last run"), the assistant as the
   default way to start a flow, a guided wizard, clearer empty states and hints on the canvas.

9. [ ] Workspaces, next steps: try them with a real repository and real flows; the board could
   start a run and show running flows; "Fill from a run" could also take files; copy a whole
   run's inputs to another flow; the assistant could propose splitting a big flow into
   subflows.

10. [ ] Files for agents, next: sound of a video (a transcription tool), PDF and Office files,
   images/videos in the workspace folder (`workspace.read` shows images; no video there yet).

11. [ ] Board of items: cards that come from a connector action (e.g. Azure DevOps PBIs of a
   sprint), columns from a field (the state), a flow per column run on a card, the last run and
   state per item; first step: an `azure_devops` connector with named read actions and one flow
   per stage. Check whether HTTP read actions can use POST (WIQL) or stay on saved queries (GET).

12. [ ] Connector `allowed_paths`: refuse `*` (or treat it as one path segment) and, when "Test
   connection" is refused, name the test path (Azure DevOps tests `/_apis/projects`).

13. [ ] Resume / run from here for orchestrator-mode runs (today graph mode only).
14. [x] **Pause a running agent and go on from the same place** (graph mode). Engine `pause(node |
    None)`: cancel the running agent tasks, keep each one's live session id (the messages carry
    it), mark the node `paused`, cancel its waiting write approvals; the run ends `paused`. Going
    on: the paused nodes run with `resume=<session>` and a short "you were paused, continue from
    where you stopped; do not redo finished work" message; the policy is refreshed first (the
    autonomy changed meanwhile applies). Editor: Pause in the header and on a running step; a
    "Paused by you" card with Go on / Stop. Tests with the fake SDK (a slow agent paused, its
    session resumed, nothing redone).
15. [x] **Other models and providers** (step 1). The runtime stays the Claude Agent SDK (the guard hook,
    the write policy, the io tools all live there). Step 1: providers as data (studio settings:
    name, an Anthropic-compatible base URL, the key as `${VAR}` or keyring, the models it offers)
    — Amazon Bedrock / Google Vertex / Microsoft Foundry through the CLI's own switches, any other
    model (OpenAI, Gemini, local Ollama…) through a gateway that speaks the Anthropic API (e.g.
    LiteLLM). An agent or flow model becomes `provider/model`; `compile` sets the session's `env`
    (`ANTHROPIC_BASE_URL`, token from the env/keyring, never in files). The model picker groups by
    provider; no cost estimate for unknown prices. Step 2 only if needed: a second runtime.

Done: multilingual UI · themes · connectors phase 2 · code flow templates · typed IO + graph engine
· editor polish · context menu · agent library · assistant commands/history · connector actions
and website connectors · local by default / extensions / public · studio workspaces · workspace
assistant · resume / run from here (see the Log).

## Getting started on a new machine
```powershell
git clone <url> agent-flow-studio; cd agent-flow-studio
python -m venv .venv; .\.venv\Scripts\python -m pip install -e ".[dev,web]"   # web: optional
cd editor; npm install; npm run build; cd ..
.\.venv\Scripts\pytest
.\.venv\Scripts\agent-flow-studio-editor
```
SDK authentication: the Claude Code login on this machine (run `claude`, then `/login`; the
Claude app login is separate), or the `ANTHROPIC_API_KEY` environment variable set before you
start the editor or the CLI (the studio does not read `.env`).

## Log
- 2026-11-19 · work machine (account A) · Project renamed and reorganized as agent-flow-studio;
  company-specific part moved to a local extension outside git; created this STATUS.md and the
  example flow `repo-review`; repository started.
- 2026-11-19 · second machine (account B) · Whole repository translated to English (rule in
  CLAUDE.md: repo in English, chat in Portuguese); multilingual editor UI (pt/en/es) with
  `editor/src/i18n/`; assistant replies in the UI language (`lang` in `/api/assist`).
- 2026-11-19 · second machine (account B) · Editor themes: System, Light, Dark, Sepia, Ocean, High
  contrast (CSS tokens + `theme.tsx` + `public/theme-init.js`).
- 2026-11-19 · second machine (account B) · Connectors phase 2: sign-in on a page (OAuth 2 +
  PKCE) with presets (Facebook, Instagram, Threads, WhatsApp, Reddit), managed identity, new types
  Microsoft Graph, GitLab, Atlassian Cloud.
- 2026-11-19 · second machine (account B) · Code flow templates (code-review, test-runner,
  fix-and-pr), template engine with parameters (editor, API, CLI, extensions), read-only
  `workspace.git` tool.
- 2026-11-19 · second machine (account B) · Typed inputs/outputs, data between agents, graph
  engine with parallel agents (execution per flow), `if` expressions + `when` text, run form with
  files, IO editors and typed ports in the editor; example flow and templates migrated.
- 2026-11-19 · second machine (account B) · Simpler agent test form: one form with the agent's
  inputs and where each value comes from; flow inputs optional in agent mode (the run keeps its
  saved ones); given values type-checked and cleared by a full run; ports show `← input.x`.
- 2026-11-19 · second machine (account B) · Clear message when the Claude Code CLI is not signed
  in (events marked `hint: "auth"`, a card with the `claude` → `/login` steps in the Run panel);
  fixed the authentication notes (`ANTHROPIC_API_KEY` is an environment variable, `.env` is not
  read).
- 2026-11-19 · second machine (account B) · Run state on the nodes (dot: filled = running, hollow =
  stopped; "running" badge; moving multicolor bar on the node and on top of the Run panel). Node
  style selector (Cards, Compact, Avatars; browser preference) and an agent `icon` (20 built-in
  icons or an emoji). The AFS_MOCK badge now shows only for flows with extension tools (the core
  never uses it: model, files, git and connectors are always real).
- 2026-11-19 · second machine (account B) · Fix: after Stop (or an error in the middle of a run)
  the agents that were running show as interrupted, not "running".
- 2026-11-20 · second machine (account B) · End of day: STATUS updated for the hand-over; backlog
  item 1 added (right-click context menu on the canvas and cards).
- 2026-11-20 · work machine (account A) · Right-click context menu on nodes, links and the
  canvas (keyboard accessible, themed, pt/en/es), copy/paste/duplicate of nodes, F2 rename.
- 2026-11-20 · work machine (account A) · Agent library: generic agents copied into flows with an
  origin (changed here / newer in the library, update, detach, save to library), 3 built-in agents,
  `GET/POST /api/agents`, CLI `agents`.
- 2026-11-20 · work machine (account A) · Chat assistant uses the agent library
  (`view_library`, `add_library_agent`, `update_from_library`).
- 2026-11-20 · work machine (account A) · Assistant creates/edits library agents (proposal card
  in the chat, saved by the user); editor remembers the last open flow.
- 2026-11-20 · work machine (account A) · Saved assistant commands (message + instructions,
  favorites, edit/delete) in the chat.
- 2026-11-20 · work machine (account A) · Assistant conversation history per flow (open,
  continue, rename, delete); clear message when the server is older than the page.
- 2026-11-20 · work machine (account A) · Resizable panels (splitters) and Markdown rendering in
  the assistant chat.
- 2026-11-20 · work machine (account A) · Show/hide panels; fix the splitter overlays that covered
  the palette and the bottom panel.
- 2026-11-20 · work machine (account A) · Connector instructions for the agents (prompt, assistant,
  secret check, audit by size).
- 2026-11-20 · work machine (account A) · Connector actions (named operations = permissions,
  run_action, policy), website connectors (Playwright, sign-in by the person, steps), show_if on
  flow inputs, template ticket-triage.
- 2026-11-20 · work machine (account A) · Save a flow with problems after a confirmation (never with
  a literal secret).
- 2026-11-20 · work machine (account A) · First real website connector (local): get_json step with field list,
  redact on actions, session cookies kept from the sign-in, JSON opened through the browser.
- 2026-11-20 · work machine (account A) · Local by default: flows/* ignored, locations (local /
  extension) for flows, library agents and connectors, public badge, private terms check.
- 2026-11-20 · work machine (account A) · Public as an explicit location (flows, library agents,
  connectors) blocked by private terms; move flows between places; agent library window.
- 2026-11-20 · second machine (account B) · Canvas shows the links of a run: followed (green, ✓,
  moving while the target runs), not followed (faded, ✗), failed `if` (red). Checked in the browser
  with a fake model on the `parallel` test flow.
- 2026-11-20 · second machine (account B) · Server messages in the UI language: codes + params
  (`messages.py`) in spec, types, expressions, compile, run form and validation (pydantic errors
  get `pydantic.<type>`); tables `server.pt.ts` / `server.es.ts`; `test_messages.py`.
- 2026-11-20 · second machine (account B) · Record a website action: Connectors → website → Record
  opens a visible window (connector session), a page script reports clicks/fields/choices/Enter and
  Alt+click extracts; typed values become params, passwords skipped; draft added to the actions
  text (`connections/recorder.py`, `test_web_recorder.py`). Playwright installed on this machine
  (Edge and Chrome both start); the recorder test runs headless with Edge.
- 2026-11-20 · second machine (account B) · Fixes from the first real run + decision node: tool
  detail/error in the run log (`trace.py`), Markdown log, default agent turns 40, retry rule, run id
  only for flows without inputs, connector info in the agent panel (`compile.http_tool_unused`),
  recorder skips sign-in steps, `no_viewport` windows, `previous.*`, stop `message`, `decision`
  gates and edge `label` (`test_decisions.py`). Known: `test_chats.py::test_rename_keeps_the_order`
  can fail once in a while (two saves in the same clock tick); it passes on a rerun.
- 2026-11-20 · second machine (account B) · Recorder: each typed field is a parameter or, if
  ticked in the editor, a fixed value (the typed text stays in memory/local editor until
  chosen; passwords never leave the page); the hint in the recording window moves away from
  the mouse and hides while Alt is held. Blank flows: no `read_artifact` by default and no
  legacy task (only a run id); agents with `read_artifact` and no `reads` are told not to
  guess names.
- 2026-11-20 · second machine (account B) · Website actions say where they failed: the step,
  the page path and the requests a read action blocked (with the `allow_post` hint). Connectors
  → an action → Test runs a read action now (a website one in a visible window, slow, open 20 s
  after a failure) and shows what an agent gets (`test_action_test.py`, real Edge headless).
- 2026-11-20 · second machine (account B) · Recorder: list items/options are recorded by their
  text (ids ending in a number are positions, not stable); extracts never by text. POST seen
  while recording → an `allow_post` checkbox on the draft; a failed Test that blocked POST →
  "Allow POST in this action and test again" (`/actions/<a>/allow-post`, audited). Sites that
  connect to their server by POST (e.g. a live-connection negotiate) need it.
- 2026-11-20 · second machine (account B) · Website actions keep the window size they were
  recorded at (`viewport: {width, height}`; sites change menus with the size, e.g. a "More"
  link only in a narrow layout); the recorder warns when the window is resized while
  recording; a failed step says the window size and, on a timeout, suggests the viewport.
- 2026-11-20 · second machine (account B) · Website steps act on the first VISIBLE match (a
  hidden copy of a menu before the visible one made clicks wait forever); Playwright errors
  keep the reasons of their call log ("resolved to …", "element is not visible").
- 2026-11-20 · second machine (account B) · `tag:text-is("…")` steps also find the element
  whose VISIBLE text is that text (`:text-is` counts hidden text inside, e.g. a counter badge
  drawn once the site connects); website actions keep the browser language they were
  recorded in (`locale`, e.g. pt-PT: multilingual sites change the texts the steps look for).
- 2026-11-20 · second machine (account B) · Recorder: a click on a link to a page of the site
  becomes `goto <address>` (language-free); a goto right after a goto replaces it, so menu
  paths collapse to the final page (Home → More → Users = `goto /admin/users`).
- 2026-11-20 · second machine (account B) · The last Test of each action is kept on this
  machine (`runs/.connector-tests/<connector>.json`, removed with the connector) and shown
  again from "last test ✓/✗" next to the action, with its date and parameters.
- 2026-11-20 · second machine (account B) · Action nodes (palette → "Action (no model)"): an
  agent with `action:` runs one read action of a connector in graph mode, with no model, and
  maps the result to typed outputs (`from`, `match`); validated (graph only, read actions,
  known params, mapped outputs exist), audited with names only, `test_action_nodes.py`. Checked
  in the browser with a fake action: action → decision → stop messages ("14 users", "2 new
  users").
- 2026-11-21 · second machine (account B) · Decision panel: every branch in one row (label,
  destination, condition, and the stop's message) with value chips, ↑/↓ and "+ Branch" (a new
  stop, placed before "otherwise"); diamond text centered. Assistant: `set_agent(action=…)`
  builds action nodes, `view_catalog` shows each action's last Test values, and `http_get` is
  only added for connectors used with direct requests; its instructions explain action
  nodes, decisions, stop messages and `previous.*`.
- 2026-11-21 · second machine (account B) · First real flow working end to end (local, not in
  git): a website action node → decision with the previous run → stop messages, built
  with the assistant. Backlog item 8 added: make building flows much easier.
- 2026-11-21 · second machine (account B) · Resize nodes on the canvas: handles on the selected
  node (cards, compact, decisions; not avatars), the size kept as `w`/`h` in the layout
  (also used by Arrange), right-click → Automatic size.
- 2026-11-21 · second machine (account B) · Control links now attach to the control points (top
  and bottom, handle id `c`): before, a link from a node with output ports left from its
  first port (it looked like a data link, and got lost when the node was resized or
  compact). A resized agent node is never shorter than its ports.
  The minimum is the title and the ports (card 70 / compact 46 + 18 per port row): the
  description and badge rows give way, the ports stay at the bottom; only valid sizes are
  stored. After pulling: restart the editor server (the flow format now has w/h).
- 2026-11-21 · second machine (account B) · Resizing, round 3: each node style keeps its own
  size (`w`/`h` for cards, `compact: {w, h}`, `avatar: {w, h}` in the layout); a resized card
  adapts with container queries (no description below 130 px, compact-like below 105 px);
  avatars resize too (the circle follows the size, ports stay on the ring); the handles
  have a bigger invisible grab margin (10–12 px).
- 2026-11-21 · second machine (account B) · Run panel: text boxes grow with the text (and resize
  by hand); text/markdown outputs are also files (`runs/<id>/outputs/<agent>.<output>.md`,
  set_output tells the agent the path); each output has Open (large view, Markdown
  formatted), Copy and Download, the last result opens by itself, and "Open folder" opens
  the run folder (`/api/runs/<id>/reveal`). "Ask for more" after a run (or at a gate):
  resumes the session of the agent that did the step (graph) or the orchestrator, with the
  same tools and rules (`Runner.ask`, `GraphEngine.ask`, `/api/runs/<id>/ask`).
- 2026-11-21 · second machine (account B) · Past runs: the Run panel lists the saved runs of the
  flow (`/api/flows/<flow>/runs`) and opens one again from its `events.jsonl`
  (`Runner.open_past`: state, gate, outputs, the agents' session ids; graph engine state
  restored), so "Ask for more" and gate decisions work after a restart. The agents use the
  flow as it is now.
- 2026-11-21 · second machine (account B) · Run panel layout: a drag bar between the conversation
  and the outputs (double click: default), the run setup can be hidden (▴/▾ next to Run),
  and the Outputs/Artifacts blocks fold; all kept in the browser (`afs.run.*`).
- 2026-11-22 · second machine (account B) · Studio workspaces (one go): `workspace.yaml` with
  roots, vars and environments, in a repo or in the studio, registry per machine; workspace
  flows/agents/connectors layered over the studio ones (keyring per workspace); `{vars.x}`/
  `{roots.x}` substitution and `vars` in conditions; environment kind → `approve_prod`; runs,
  chats, previous and connector tests per workspace; Board view; hand-off between flows (`source`
  hints, "Fill from a run", provenance); subflow nodes; "Copy to…" (flows, flow agents, library
  agents, connectors, vars/environments) with a dependency check and "Duplicate workspace";
  assistant `view_workspace` and subflows; CLI `--workspace`/`--env`. Tests: 52 new (495 core).
  Checked in the browser with a fake model and a fake keyring.
- 2026-11-22 · second machine (account B) · Workspaces, after the first look: "Browse…" next to
  folder fields (New / Open / Duplicate workspace) opens an in-dialog folder browser
  (`/api/folders`: subfolders by name, drives, git repositories and workspaces marked); the
  repository field shows only "In a repository"; the top bar says "Workspace" before the
  workspace selector and "Flow" before the flow selector (the "Flows" brand is gone).
- 2026-11-22 · second machine (account B) · Workspace context and move: `context.md` per
  workspace (a section per root + notes) that every agent of its flows reads; the context
  keeper (read-only system agent per root) explores or updates it from the board, when a root
  is added (Browse… in the roots table), from the Run panel ("suggest", the default) or as a
  last system step of each run ("always", shown on the canvas); "Move to workspace…" for flows,
  flow agents, library agents, connectors (with their secret) and variables/environments, in
  the top bar, board, canvas, library and connector menus. CLI `context`. Checked in the
  browser with a fake model (explore, always, suggest, move).
- 2026-11-22 · second machine (account B) · Board: the side panel shows where the workspace
  lives (the repository folder, the workspace files) and the absolute folder of each root; it
  can be resized (drag bar, shared with the editor's right panel); context menus on the canvas
  (new flow, update/explore the context, arrange, fit, new/duplicate workspace) and on each
  card (open, run, duplicate here, copy, move, delete).
- 2026-11-22 · second machine (account B) · Context keeper progress: the board's context block
  follows the run live (spinner, elapsed time, state per root, its last actions, Stop) and
  finds a run that is already going (started from the board menu, a new root or another tab).
- 2026-11-23 · second machine (account B) · Agents see images and videos: `view_image` and
  `view_video` (ffmpeg frames) for file inputs, by the input's `accept`; large files are
  streamed (`/api/uploads`, up to 1 GB); the SDK's 1 MB message limit is raised (a large image
  ended the session); `read_file` now declares the `agent` argument the guard checks. Checked
  with the real CLI against a local fake API: the images reach the model request. After you
  pull: `npm run build`, restart the editor, and install ffmpeg (or `.[media]`) for videos.
- 2026-11-24 · work machine (account A) · Pulled and rebuilt; first real workspace with the local ticket
  flows copied into it (connector fixed in the copy); local extension uses the core draft marker.
- 2026-11-25 · work machine (account A) · Workspace names: spaces/- become _, invalid or repeated
  names refused with a message, order kept; a renamed root keeps its explored context.
- 2026-11-25 · work machine (account A) · Workspace assistant in the Board (read + proposals the
  person applies); Board top bar cleaned up and the Connectors window opens over the Board.
- 2026-11-25 · work machine (account A) · Assistants: 60 turns, turn counter, Continue at the limit;
  short flow proposals filled in, all problems at once.
- 2026-11-25 · home machine (account B) · History from past Claude Code sessions: Learn on a root
  (sessions and memory notes of its folder, studio sessions set apart and tagged), readers + a
  history keeper, a draft the person reviews, `<root>.history` in context.md.
- 2026-11-26 · home machine (account B) · Agent cards show their model (own / flow / studio
  default); model choice in the node context menu; workspace settings fit a narrow side panel.
- 2026-11-26 · work machine (account A) · Unused flow inputs: validation warning, assistants fixed.
- 2026-11-26 · work machine (account A) · Resume a failed or stopped graph run keeping the finished work.
- 2026-11-26 · work machine (account A) · Gate answered with no matching link asks again; agents can read a gate's comment.
- 2026-11-26 · work machine (account A) · Run from here (any step of a saved run) and renaming runs.
- 2026-11-26 · work machine (account A) · Run panel layout for long input forms; STATUS hand-over rewritten.
- 2026-11-26 · home machine (account B) · Run panel rewrite: views, steps/log/history, Compare,
  retry of model API errors, live cost estimate, queue (saved), decisions never refused (decided,
  changeable, kept across restarts), one count of what waits for you, run links; 12 critic rounds.
- 2026-11-26 · home machine (account B) · Reviewing runs: Changes (git diff since the start),
  previews of run files, the workspace's web app (Preview tab, preview.screenshot), Continue in
  Claude Code and the studio MCP server for Claude Code.
- 2026-11-27 · home machine (account B) · Notes from real use: Markdown tables after text, saved
  autonomy reaches runs (and a warning for agents with their own), workspace.git finds repos;
  pause and other providers planned (backlog 14, 15).
- 2026-11-27 · home machine (account B) · Pause a running agent and go on in its own session
  (graph runs): `POST /api/runs/{id}/pause`, Pause in the header and on a step, "Paused by you"
  card, `paused` events restored after a restart; tests with the fake SDK.
- 2026-11-27 · home machine (account B) · Flow properties compare the two execution modes; new
  blank flows and the assistant default to the graph engine.
- 2026-11-27 · home machine (account B) · Model providers (step 1): providers.yaml, `provider/model`,
  session env per agent, keys from env/keyring, unknown gateway cost, picker and Properties note,
  CLI providers/provider-key; tests with the fake SDK.
- 2026-11-27 · home machine (account B) · Providers window in the editor (from every model field), providers API
  (save, delete, key, check), assistant catalog lists the providers.
- 2026-11-27 · home machine (account B) · Split the editor backend: server.py → app + editor/api/
  routers; runner.py → Runner + editor/runs/ mixins; no behaviour change, same routes, tests pass.

# STATUS — shared status

Hand-over file between machines/accounts. **When you start:** read this. **When you finish a task:**
update "Current state" and "Next steps", add a line to the "Log" (date, machine/account in generic
terms — no emails —, what was done) and commit + push.

## Where we stopped (2026-11-25, home machine / account B)
The commit after `dbd1c69` adds **history from past Claude Code sessions** (below). After you
pull: `cd editor; npm run build; cd ..` and restart the editor (new routes under
`/api/context/`); no new dependencies.

**History from past Claude Code sessions (new, 2026-11-25, home machine)**
- Board → Context → **Learn** on a root: lists the Claude Code sessions and memory notes about the
  root's folder on this machine (`claude_sessions.py`, SDK `list_sessions` /
  `get_session_messages`); the studio's own runs and other programs' sessions are set apart (tag
  `agent-flow-studio` added to new studio sessions by `mark_ours`; older ones known by their first
  prompt or `entrypoint`). Size estimate before starting.
- One `history_reader` per source (no tools; text only, reminders stripped, secrets masked), then
  `history_keeper` (read-only tools on the root) checks the notes against the code and drafts the
  `<root>.history` section. The person edits and saves or discards the draft; nothing is saved by
  the run. Learned sources are kept per machine in `runs/@<ws>/.context/learned.json`.
- Agents get the history after all root sections (`MAX_PROMPT` now 16 000, history ≤ 8 000).
- Checked with fake sessions and a fake model (tests + browser). **Not tried with the real model
  or real sessions yet.

**Then (2026-11-26, home machine)**
- **Model on agent cards**: each agent card (all three styles) shows the model it runs with, under
  its name: its own (accent colour), else the flow's, else the studio default
  (`/api/catalog` → `default_model` = `AFS_MODEL`; "default model" when unset = the Claude CLI's).
  Action and subflow nodes show none. The tooltip says where the model comes from.
- **Model in the node context menu**: a row "flow · sonnet · opus · haiku · …"; "…" asks for a
  full model ID. "flow" removes the agent's own model.
- **Workspace settings fit a narrow side panel**: the roots table has a fixed layout, and long
  paths in the "stored in" box wrap (the panel scrolled sideways before).

## Earlier stop (2026-11-25, work machine / account A)
Commit `dbd1c69` holds everything below.

**After you pull on the other machine**
1. **Stop the editor first**, then `.\.venv\Scripts\python -m pip install -e ".[dev,web]"` (a
   running `agent-flow-studio-editor.exe` is locked on Windows: pip stops halfway and the package
   disappears from the venv; fix: delete `.venv/Lib/site-packages/~gent_flow_studio-*` and install
   again). No new dependencies in this session.
2. `cd editor; npm run build; cd ..` and **restart** `agent-flow-studio-editor`: the server has
   new routes (`POST /api/assist/workspace`); with an old server the editor says "restart".
3. `.\.venv\Scripts\pytest` (all should pass; the 11 video/ffmpeg tests are skipped without the
   `media` extra).

**What this session added (2026-11-24 → 25)**
- **Workspace assistant** (Board → side panel → **Assistant** tab; `editor/ws_assistant.py` +
  `ws_assistant.md`, `editor/src/WsProposals.tsx`). It reads the workspace (settings, context
  sections, flows, connectors, templates, library), answers questions citing the context, and only
  **proposes** — workspace settings, a new flow (from a template or a short spec that the studio
  fills in), a connector (read-only, no secrets). Each proposal is a card the person applies
  (PUT workspace / create + save the flow in the workspace / PUT connector with location
  `workspace`); secrets, sign-in and write access stay in the Connectors window. Saved commands
  and history (key `@workspace`) as in the flow assistant.
- **Both assistants**: 60 model turns per message (was 30 / 40), "turn n of 60" while working, and
  at the limit no error: a message with **Continue** (same session). Shared event loop
  `assistant.stream`. `propose_flow` gives all problems at once (JSON with where each one is).
- **Board**: the top bar shows only what works there (⋯ / Flow editor / New / Connectors); the
  Connectors and library windows now open over the Board (before, only in the flow view).
- **Workspace settings**: names with spaces or "-" become "_" (with a message), invalid or
  repeated names are refused, order is kept; a root renamed with the same folder keeps its
  explored context (`context.follow_renames`).
- **Connectors in a workspace**: a new connector defaults to location "workspace", and the label
  says "workspace" (it said "extension workspace").
- **Tests**: the real-browser tests use `ThreadingHTTPServer` (the single-threaded one could hang
  for ever on Edge's idle sockets); the whole suite takes about 2 min.

**Checked how**: unit/API tests, plus the browser with **simulated** assistant replies (cards
applied in a temporary workspace, then removed; the limit message and Continue). **Not tried yet
with the real model**: the workspace assistant, the turn counter and Continue.

**Local only (work machine, outside git)**: a studio workspace with the two local ticket flows
copied into it (the originals are still in `flows/`); the local extension now uses the core's
draft marker. The website connector's session expires after a few days: sign in again
(Connectors → Sign in) before a run. Details in `CLAUDE.local.md` there.

**Idea discussed, not started**: a flow that reads Azure DevOps boards/PBIs and follows each item
through stages (analysis → refinement → approved → development ⇄ tests ⇄ review → final approval).
Possible today as one flow per stage with the item id as input (the `azure_devops` connector with
named read actions, human gates, a decision loop with `max_runs`, a final write with approval).
Missing: a **board of items** in the studio (cards from a connector action, columns from a field,
a flow per column) and state per item across runs — see backlog item 11.

**Next suggested task**: on a real repository with past Claude Code sessions, open it as a
workspace and use Board -> Context -> **Learn** with the real model (a few sessions first; read the
draft for private data before saving). Then try the workspace assistant with the real model
(e.g. "propose a flow for …" in a workspace); then item 1 of the backlog (sign in on the website connector and run the
copied triage flow end to end from the workspace).

**Earlier hand-overs (summary; details in the Log)**: 2026-11-20 agent library, assistant
commands/history, connector actions and website connectors, local/extension/public locations;
second machine: run links on the canvas, server messages in the UI language, action recording,
decision/stop-message nodes, `previous.*`; 2026-11-22 studio workspaces (roots, vars,
environments, Board, hand-off, subflows, Copy to…, workspace context); 2026-11-24 images and
videos for agents.

## Current state
Works and is tested (`pytest`: core 563 tests without extensions — 552 + 11 that need the `media` extra; `ruff` clean; `npm run build`):
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

Done: multilingual UI · themes · connectors phase 2 · code flow templates · typed IO + graph engine
· editor polish · context menu · agent library · assistant commands/history · connector actions
and website connectors · local by default / extensions / public · studio workspaces · workspace
assistant (see the Log).

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

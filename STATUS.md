# STATUS — shared status

Hand-over file between machines/accounts. **When you start:** read this. **When you finish a task:**
update "Current state" and "Next steps", add a line to the "Log" (date, machine/account in generic
terms — no emails —, what was done) and commit + push.

## Where we stopped (2026-11-20, work machine / account A)
Last commit: `dcb9990` (all the work of this session; pushed by the user).

**After you pull on the other machine**
1. `.\.venv\Scripts\python -m pip install -e ".[dev,web]"` (`web` = Playwright, optional: only for
   website connectors; it uses the installed Edge/Chrome, no browser download). **Stop the
   editor first**: on Windows a running `agent-flow-studio-editor.exe` is locked, pip stops halfway
   and the package disappears from the venv (`No module named 'agent_flow_studio'`; fix: delete
   `.venv/Lib/site-packages/~gent_flow_studio-*` and install again).
2. `cd editor; npm run build; cd ..` and **restart** `agent-flow-studio-editor` (the server has new
   routes; with an old server the editor says "restart agent-flow-studio-editor").
3. `git status`: flows in `flows/` other than `repo-review` are now **ignored by git** (nothing is
   deleted). Create `private_terms.txt` there too (outside git) with the words that must never be
   published.

**What this session added** (details in "Current state" and the Log)
- Assistant: agent library tools (view / copy / update, and proposals of new or edited library
  agents saved by the user), saved commands, conversation history per flow, Markdown in the chat.
- Editor: last open flow remembered, resizable and hideable panels, save with problems (asks
  first; never with a literal secret), agent library window (palette → Manage).
- Connectors: instructions for the agents; named **actions** as permissions (`run_action`,
  policy, "only these actions"); **website** connector type (sign-in by the person, declared steps
  or `get_json`, read actions cannot write, `redact`).
- Flows: `show_if` on inputs; template `ticket-triage` (text / URL / number → one ticket text).
- **Where things live**: local by default (`flows/*` ignored); an extension (can be its own private
  repo) or, explicitly, public (git) for flows, library agents and connectors; private terms
  block anything public.

**Real use on the work machine (local only, not in git)**: one website connector to the internal
ticket platform works (`get_ticket` on a real ticket, phones redacted); details in
`CLAUDE.local.md` there.

**Not tried yet**: `ticket-triage` end to end with the model; the assistant's new tools in a real
chat with the model; resuming a conversation after a server restart.

**Known**: the local extension's `test_save_artifact_marks_drafts` fails on the
work machine (the core draft marker is English since the translation; the extension test expects
the Portuguese one). Fix it in the extension (outside git).

**Since then, on the second machine (account B)**: backlog items 3 (links of a run on the canvas),
7 (server messages in the UI language) and the first part of item 2 (record a website action).
Recording is tested with Playwright and Edge (headless, on a local page); it was not tried yet by
a person on a real site: try it on the work machine. After you pull: `npm run build` and restart the editor.

Then the fixes from the first real run of a hand-made flow (a website connector, one agent, a stop):
run log with the tool detail and its error text, Markdown in the log, a default turn limit per
agent (40) and "retry at most once" for the orchestrator, a run id only for flows without inputs,
the agent panel says what each connector gives (no more automatic `http_get` for websites), the
recorder leaves out sign-in steps and warns without extracts, resizable visible browser windows,
`previous.<agent>.<output>` (the last finished run), stop **messages** with `{expression}` parts
and the **decision** node (diamond, first branch whose `if` holds, "otherwise", labels "yes/no").
Checked in the browser with a fake model (no real model run). The flow on this machine
(local, outside git) still has its old recorded action (with the sign-in clicks and no extract):
record it again after "Sign in" and rebuild the flow with a decision.

**Next suggested task**: item 1 of the backlog.

## Current state
Works and is tested (`pytest`: core 433 tests, without extensions; `ruff` clean; `npm run build`):
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

**Note for the other machine (from the translation):** local flows and the extension there may
still have Portuguese prompts/texts (outside git). Defaults changed: `input.label` "Entrada" →
"Input", `task` "Executa o flow para {input}." → "Run the flow for {input}.", the orchestrator
section "## Fluxo" → "## Flow", the validation API key `fluxo` → `flow_section`; the assistant tools
have English names. No YAML key, enum value, tool id or env var changed, so old flows still load.

## Next steps (backlog, in suggested order)
1. [ ] Run `ticket-triage` end to end with the model and a real connector (work machine), then the
   other templates and a graph flow (check `tools=[]` per agent session, the router, parallel
   sessions); tune the template prompts. Try the assistant's library tools in a real chat.
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

Done: multilingual UI · themes · connectors phase 2 · code flow templates · typed IO + graph engine
· editor polish · context menu · agent library · assistant commands/history · connector actions
and website connectors · local by default / extensions / public (see the Log).

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

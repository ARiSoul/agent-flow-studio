# STATUS — shared status

Hand-over file between machines/accounts. **When you start:** read this. **When you finish a task:**
update "Current state" and "Next steps", add a line to the "Log" (date, machine/account in generic
terms — no emails —, what was done) and commit + push.

## Where we stopped (2026-11-20, work machine / account A)
- Last work (agreed with the user as phases 1–3):
  1. **Connector actions** = permissions: named operations with typed params on a connector
     (`connections/actions.py`, YAML editor in the Connectors window, "Only these actions").
     `run_action` comes implicitly with connectors that have actions; `policy.py` checks the
     action, read vs write (write = approval as http_write); `actions_only` refuses http_get/
     http_write; the prompt lists the actions; the assistant sees them. Connectors without actions
     still need http_get/http_write (now checked in `compile`, no longer in `spec`).
  2. **Website connector** (`type: website`, `connections/web.py`): the person signs in in a
     visible browser window (profile outside git, `~/.agent-flow-studio/browser/<name>`); actions
     run declared steps headless (goto/click/fill/select/press/wait_for/extract*); read actions
     block non-GET requests; navigation must stay on the site/allowed paths; expired sessions are
     detected. Optional dependency `pip install -e ".[web]"` (Playwright with the installed Edge).
     A real-browser test runs against a local page (skipped without Playwright); installed here.
  3. **`show_if` on flow inputs** (run form shows/requires a field only when it holds) and the
     template **`ticket-triage`** (text / URL / number → intake → fetcher with the connector action
     → triage, join any → human review).
- **Save with problems**: Save on an invalid flow lists the problems and asks "Save anyway"; the
  server writes it as it is (`yaml_io.dump_raw`, `force: true`) and it opens again with its
  problems; it does not run until fixed. A secret written in the flow is never saved.
- **First real website connector** configured on the work machine (local only, in its
  `connections.yaml`; details in CLAUDE.local.md there): read-only `get_json` action with a field
  list, `redact: [phone]`, sign-in in Edge; `get_ticket` works on a real ticket. Fixes found on the
  way: the JSON is opened by the browser (Playwright's own HTTP client did not trust the company CA)
  and session cookies are saved from the sign-in window (they are dropped when it closes).
- **Where things live**: `flows/*` is ignored by git (public examples listed one by one, today
  `repo-review`); flows, library agents and connector definitions can live in an extension
  (`extensions/<name>/flows`, `agents`, `connections.yaml`; "Where to keep it" in the editor);
  the flow picker marks public flows; `private_terms.txt` (outside git, or per extension) makes a
  public save ask first. An extension can be its own private git repo to sync company work.
  Then **Public (git)** as an explicit location (`publish.py`) for flows (`.gitignore`
  exception), library agents (built-in library) and connectors (`connections.public.yaml`); private
  terms block it. Flows move between places from the location tag; an **agent library window**
  (palette → Manage) edits (YAML definition + prompt), duplicates, moves and deletes agents.
  **On the other machine**: local flows outside `repo-review` are now ignored by git (check
  `git status` after pulling; nothing is deleted).
- Next: run `ticket-triage` end to end with the model using that connector (not done yet).
- After you pull: `pip install -e ".[dev,web]"` (Playwright, optional), `cd editor; npm run build`,
  and **restart** `agent-flow-studio-editor`.
- Then: the **chat assistant knows the library**: tools `view_library` (read only),
  `add_library_agent` (copy into the draft with an origin) and `update_from_library` (keeps input
  links, join, loops, icon); helpers `copy_into` / `refresh_copy` in `builder/library.py`; the
  assistant prompt says to check the library before writing a new agent. It can also **create
  and edit library agents**: `propose_library_agent` (from scratch, `from_agent` or
  `from_library`; checked with `library.check`, which also requires catalog tools only) → a card in
  the chat with "Save to library" that the user presses (overwrite asks first). Built-in and
  extension agents are read only. Tested with the tools directly and the card in the browser with a
  simulated reply; not tried yet in a real chat with the model.
- The editor remembers the last open flow in the browser (`afs.flow`) instead of the first one.
- **Saved assistant commands**: "☆ Save" (next to Send or on a sent message) → a form with name,
  message, instructions (multi-line) and favorite; chips above the chat input (favorites first),
  a click fills the input and attaches the instructions (sent with `command` to `/api/assist`,
  `assistant.with_command`); "⋯" opens a window to see details, use, edit, favorite, delete.
  Stored in `assistant_commands.yaml` (outside git; `editor/commands.py`, `/api/commands`).
  `Dialog.form` now has `multiline` and `checkbox` fields. Checked in the browser (reply simulated).
- **Conversation history** of the assistant per flow (`editor/chats.py`, `runs/chats/<flow>/`,
  `/api/chats`): saved after each reply with the model session id; the last one reopens with the
  flow and continues (resume); "New conversation" keeps the old one; "History" opens, renames,
  deletes. Checked in the browser with simulated replies (a real resume after a server restart
  depends on the CLI keeping its session; not tried with the model yet).
- When the page is newer than the running server (a route is missing), the editor now says
  "restart agent-flow-studio-editor" instead of "Not Found". **After pulling, restart the editor
  server**, not only `npm run build`.
- **Resizable panels** (`editor/src/Splitter.tsx`): drag the edge of the right panel, the palette
  or the bottom panel (double click = default; arrow keys too); sizes saved in the browser
  (`afs.panels`). **Markdown in the chat** (`editor/src/Markdown.tsx`, React elements only, no raw
  HTML): headings, bold/italic, code, lists (nested), quotes, tables, links.
- **Show/hide panels**: toolbar buttons (palette, bottom, right) and Ctrl+B / Ctrl+J / Ctrl+Alt+B
  (physical keys, so AltGr layouts work); saved in the browser (`afs.hiddenPanels`); opening a
  tab from elsewhere (Properties, Test agent) shows the right panel again. Fixed: the splitter
  overlays reused the `pal`/`bottom` class names and covered the palette and the bottom panel.
- **Connector instructions** (`Connection.instructions`, Connectors window, "Insert an outline"):
  added to the prompt of every agent with that connector (read or write, `compile._instructions_section`)
  and shown to the assistant; secret-like text refused (`model._SECRET_LIKE`); a change writes
  `connector_instructions_changed` with the size only to `runs/audit.jsonl`. Write-only connectors
  are now also checked at compile time (must exist on this machine).
- Before that: right-click context menu on the canvas (see the Log).
- After you pull on the other machine: `cd editor; npm run build` and restart
  `agent-flow-studio-editor`. No new dependencies. Your local `agents/` folder starts empty.
- Known: the local extension's `test_save_artifact_marks_drafts` fails on the
  work machine (the core draft marker is English since the translation; the extension test still
  expects the Portuguese one). Fix it there, in the extension (outside git).
- Next suggested task: item 1 below (run the graph flow and templates end to end with the model).

## Current state
Works and is tested (`pytest`: core 371 tests, without extensions; `ruff` clean; `npm run build`):
- **Repository in English**: code, messages, prompts, flows, tests and docs. Only the chat with the
  user is in Portuguese (rule in `CLAUDE.md`).
- **Multilingual editor UI** (`pt`, `en`, `es`): selector in the toolbar, saved in the browser
  (`afs.lang`). Strings in `editor/src/i18n/<lang>.ts` (`en.ts` is the reference; `tsc` fails if a
  language misses a key). The assistant replies in the UI language. Backend messages are English.
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
  agent runs, hollow dot when stopped, red-bordered when interrupted by Stop); outputs view;
  artifacts; gates with a decision; live write approvals; a card with the `claude` → `/login`
  steps when the CLI is not signed in.
- **Chat assistant** in the editor: builds/changes the flow in a draft (never saves on its own);
  knows typed IO, `if` conditions, execution modes and icons.
- **Connectors**: HTTP API, Azure ARM, Azure DevOps, Microsoft Graph, GitHub, GitLab, Atlassian
  Cloud; secrets in the keyring or `${VAR}`; test connection; `http_get`/`http_write` per agent.
  **Sign-in on a page** (OAuth 2 + PKCE, tokens only in the keyring) with presets for Facebook,
  Instagram, Threads, WhatsApp Business, Reddit; **Azure managed identity**. Tested with mocked
  providers only.
- **Writes with autonomy** (`approve` / `approve_prod` / `auto`) and a confined workspace; the hook
  denies paths outside the workspace, force pushes/pushes to protected branches and destructive
  commands. **`workspace.git`**: read-only git.
- **Saved assistant commands** (message + instructions, favorites) in the chat; local file
  `assistant_commands.yaml`.
- **Conversation history** per flow (`runs/chats/`), resumable.
- **Connector actions** (permissions with typed params, `run_action`), **website connectors**
  (Playwright, your own sign-in), `show_if` inputs, template `ticket-triage`.
- **Agent library**: generic agents (`builder/library.py`; core in `src/agent_flow_studio/agents/`,
  extensions in `extensions/<name>/agents/`, local in `agents/`) copied into flows with an `origin`;
  the editor shows changed-here / newer-in-library and can update, detach or save to the library.
- **Flow templates** (New → "Start from", or `agent-flow-studio-flows new --template`):
  `code-review`, `test-runner`, `fix-and-pr` (typed IO, graph mode); extensions can add templates.
- **Local extensions** (`extensions/<name>/`, outside git) for domain tools/contracts/templates.
  `AFS_MOCK` only affects extension tools (the core never uses it).
- Public example flow: `flows/repo-review` (repository review, read only, graph mode).

**Note for the other machine (from the translation):** local flows and the extension there may
still have Portuguese prompts/texts (outside git). Defaults changed: `input.label` "Entrada" →
"Input", `task` "Executa o flow para {input}." → "Run the flow for {input}.", the orchestrator
section "## Fluxo" → "## Flow", the validation API key `fluxo` → `flow_section`; the assistant tools
have English names. No YAML key, enum value, tool id or env var changed, so old flows still load.

## Next steps (backlog, in suggested order)
1. [ ] Website/API actions, next steps: record the steps by clicking on the site; the assistant
   proposes actions for a connector (as proposals the user saves); a "guided" mode (the model
   browses read-only following instructions, URL allow list, screenshots in the audit).
2. [ ] Run a graph flow and each template end to end with the real model (check `tools=[]` per
   agent session, the router, parallel sessions) and tune the template prompts.
3. [ ] Canvas: highlight followed / not followed edges from the `edge` events (today only in the
   trace).
4. [ ] Try a real sign-in per provider (register an app, check the presets). Instagram and Threads
   only accept an https redirect URI, so they need an https tunnel or a hosted editor.
5. [ ] Writes through external MCPs (today read only).
6. [ ] More flows/templates, library agents and skills (phase 4 of the builder).
7. [ ] (Optional) Translate backend messages (validation, errors, input reasons such as
   "required") to the UI language.

Done: multilingual UI · themes · connectors phase 2 · code flow templates · typed IO + graph engine
· editor polish · context menu · agent library (see the Log).

## Getting started on a new machine
```powershell
git clone <url> agent-flow-studio; cd agent-flow-studio
python -m venv .venv; .\.venv\Scripts\python -m pip install -e ".[dev]"
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

# Agent Flow Studio

A local studio to **build, test and run agent flows** with the
[Claude Agent SDK](https://docs.claude.com/en/api/agent-sdk/overview) (Python): flows in YAML, an
n8n/Langflow-style visual editor, a chat-based build assistant, execution with trace, human
approvals, connectors and writes with controlled autonomy.

> Current state and next steps: [`STATUS.md`](STATUS.md).

## Concepts
- **Flow** (`flows/<name>/flow.yaml` + `prompts/*.md`): a graph of **agents** (with a prompt,
  tools, model, skills), **gates** (human approval, stop or decision) and **links** with
  conditions. A **decision** (a diamond on the canvas) does not pause: it follows the first branch
  whose `if` holds, in order, and a branch without `if` means "otherwise"; branches can have a
  label ("yes", "no"). A **stop** can show a **message** with `{expression}` parts filled in from
  the values, e.g. `You have {checker.count - previous.checker.count} new users`.
- **Typed data**: the flow declares its **inputs** (text, markdown, number, integer, boolean,
  date, datetime, url, path, file, choice, json — with required, default, min/max, pattern,
  options, accept, contract, multiple); the Run panel shows them as a form. Each agent declares
  **inputs** (`from: input.<x>`, `from: <agent>.<output>` or `from: previous.<agent>.<output>`,
  the same output in the last finished run of the flow — empty on the first run) and typed
  **outputs**, which the
  studio checks when the agent saves them (`io.set_output`): the output of one agent is the input
  of the next. Values live in `runs/<run id>/io/`; the run id is separate from the inputs.
- **Execution** (per flow): `orchestrator` — an LLM orchestrator receives the graph as
  instructions and delegates to the agents (as before); `graph` — the studio runs the graph
  itself: each agent is its own session, independent branches run **in parallel** (up to
  `max_parallel`), a node with several incoming links waits for all of them (`join: any` waits
  for the first), skipped branches propagate, and loops stop at `max_runs`.
- **Conditions** on links: `if` — an expression on the inputs and outputs, evaluated by the studio
  without an LLM (`triage.severity == "high"`, `tests.failed > 0`, `len(scan.items) > 0`,
  `approve.decision == "approve"`, `counter.count > previous.counter.count`); `when` — text,
  decided by the orchestrator (or by a small
  router call in graph mode). Both on one link: both must hold.
- **Action nodes** (graph mode): an agent with `action:` instead of a prompt. The studio runs
  that read action of a connector itself — no model, no cost, the same result every time — and
  saves the values of its result as the node's typed outputs: `params` are texts with
  `{expression}` parts, and each output can say where it comes from (`from`: a value or field
  path; `match`: a regular expression whose first group is the value, e.g. `(\d+)\D*$` takes 14
  out of "1–14 of 14"). Example: action → decision → stops with a message, no model at all.
- **Images and videos**: an agent with a `file` input sees it with `view_image` (PNG, JPEG, GIF,
  WebP; large ones are scaled down) and `view_video` (MP4, MOV, WebM, MKV, AVI: up to 12 frames per
  call, each with its time, for the whole video or a part; no sound), besides `read_file` for
  text; `accept` on the input decides which of them it gets. Videos and large images need ffmpeg
  (on PATH, or `pip install -e ".[media]"`). The run form streams large files to disk (up to 1 GB,
  kept a day in `runs/.uploads/`). Claude Code keeps its own copy of the images an agent saw with
  the session, in its config folder (`~/.claude`).
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
- **Canvas**: drag agents and gates, resize the selected node in any style (each style keeps its
  own size in the flow's layout; a small card shows less, like the compact style; right-click →
  Automatic size), link nodes, arrange, undo/redo; live validation with the same
  rules as the runtime; only valid flows are saved (copy in `flow.yaml.bak`; delete →
  `flows/.trash/`).
- **Properties**: prompts (inline or file), tools, artifacts and contracts, model, connectors,
  autonomy, workspace, external MCPs.
- **Assistant**: a chat that creates/changes agents, links, prompts, MCPs… in a draft; you review
  and save. It does not approve MCPs or choose `auto` autonomy. It replies in the interface language.
- **Run**: a form for the flow inputs (files included) and the run id; it runs the saved flow
  and shows the active agents (several at once in graph mode), the finished and skipped ones, the
  links followed (green) and not followed (faded), the
  gate where it stopped, the trace, the cost, the outputs of each agent and the artifacts. Modes:
  *full flow*, *step by step*, *until agent…* and *single agent (isolated)* — with a form for that
  agent's inputs. Gates and writes ask for a live decision. Text and markdown outputs are also
  files (`runs/<id>/outputs/`); each output can be opened large, copied or downloaded, and
  "Open folder" shows the run folder. **Ask for more**: after a run (or at a gate) you can ask
  the agent that did a step to improve or change its output — it continues its own session,
  with the same tools and rules. **Past runs**: the Run panel lists the saved runs of the flow; open
  one to see its log and outputs again, ask for more or answer its gate (also after a restart).
- **The Run panel**: three views — *New run*, *Current* (what needs you on top, then the steps or
  the log, then the results) and *History* (totals per period, search, Compare two runs step by
  step). The header keeps the run's name, state, time (with the time it waited for you), cost (an
  estimate "≈" while sessions go) and the action that matters now. Steps show what each received,
  its attempts, live tokens and each tool call (arguments and result read on demand from the local
  session, never stored). A temporary model API error (busy, rate limit) runs the step again by
  itself (`retry` in the flow, graph mode; never after a write). One run goes at a time: a new
  run waits in a **queue** (kept across restarts) and starts when no run goes or waits for you. A
  decision at a gate is **never refused**: while another run goes it is kept ("decided — goes on
  next", kept across restarts, can be changed). The open run is in the page address
  (`#flow=…&run=…&step=…`): reload or share the link to open it again.
- **Connectors**: HTTP API, Azure Resource Manager, Azure DevOps, Microsoft Graph, GitHub, GitLab
  and Atlassian Cloud (Jira, Confluence), with environment, allowed paths, read/write access and
  "Test connection". Each connector can have **instructions for the agents** (how to use that
  system: requests, fields, paging, limits, pitfalls; "Insert an outline" gives a start): every
  agent with the connector gets them in its prompt, and so does the chat assistant. They stay in
  `connections.yaml` (outside git), are refused when they look like a secret, and never widen
  access (paths and read/write still apply); a change is audited by size only.
  **Actions** are a connector's named operations with typed parameters (YAML in the Connectors
  window, e.g. `get_ticket(number)` → `GET /api/tickets/{number}`): agents with the connector get
  `run_action` automatically and the list in their prompt, the studio checks the values and builds
  the request, and with "Only these actions" they are the only way to use the connector. Write
  actions need write access and follow the autonomy (approval). Authentication: none, API key, bearer, basic, PAT, client credentials, CLI
  session (az login, gh auth login), Azure managed identity and **sign-in on a page** (OAuth 2
  with PKCE: the provider's page opens, you sign in, and the tokens go to the keyring and renew
  themselves). HTTP API has presets for Facebook, Instagram, Threads, WhatsApp Business and
  Reddit (each needs an app that you register with the provider). Secrets and tokens live in the
  system Credential Manager (keyring) or as `${VAR}`.

## Website connectors (a site used like a person)
For a platform without an API, the **Website** connector type drives a real browser with **your
own sign-in**: "Sign in" opens the site in a browser window, you sign in there (MFA included) and
close it; the session stays in a browser profile on this machine (`~/.agent-flow-studio/browser`,
outside git) and the studio never types passwords. Agents use the site **only through its actions**,
whose steps you declare (`goto`, `click`, `fill`, `select`, `press`, `wait_for`, `extract`,
`extract_all`, `extract_table`, and `get_json`: one GET of the site's own JSON with your session,
keeping only the listed fields, e.g. `notes: journal[].{date,user.name,text}`); the model picks an
action and its parameters, never free clicks. `redact: [phone, email]` on an action masks those in
everything it returns. Session cookies are kept from the sign-in window, so a site that uses
browser-session cookies still works after you close it (until the site's own session expires).
Read actions block every request that is not GET/HEAD (so they cannot change anything), navigation
must stay on the site and its allowed paths, and an expired session is detected ("signed-in
element" / "sign-in URL marker"). Needs Playwright (`pip install -e ".[web]"`), which uses the
installed Edge or Chrome.

**Record an action** (Connectors → a website → Record) opens the site in a window with the
connector's session: you do the operation once (clicks, typed fields, choices, Enter) and Alt+click
each text to extract. The editor shows the steps as you go; at the end, "Add to the actions" puts
a draft action in the actions text, for you to check, rename, describe and save. Typed values
become parameters (or, if you tick it, fixed values), password fields are not recorded, and the
draft follows the same rules as a hand-written action. A click on a link to a page of the site is
recorded as its address (`goto`), and pages passed through on the way are dropped, so the action
does not depend on the language of the texts or on menus that change with the window size; the
draft also keeps the window size and the browser language.

## Workspaces, variables and environments
A **workspace** groups the flows, agents and connectors of one project (usually one repository,
sometimes several folders), like a Postman workspace. Choose it in the top bar (**⋯** → New / Open
/ Duplicate / Forget); without one you work in the studio's own space, as before.
- **Where it lives** (chosen when you create it): *in a repository* — `<repo>/.agent-flow/`, so it
  travels with the repository — or *in the studio* — `workspaces/<name>/` (ignored by git). Both hold
  `workspace.yaml`, `flows/`, `agents/` and `connections.yaml` (definitions only). This machine
  remembers the open workspaces in `~/.agent-flow-studio/workspaces.json`.
- **Variables and environments**: `workspace.yaml` has `roots` (folders), `vars` and
  `environments` (`dev`, `staging`, `prod`… each with a `kind` and its own `vars`, which replace the
  workspace's). Flows use `{vars.name}` and `{roots.name}` in the task, prompts, parameters and
  messages, `vars.name` in conditions, and `workspace: {root: app}` for the folder the agents work
  in. The environment chosen in the top bar decides the values and the approvals (`approve_prod`
  asks a person in a `prod` environment). Variables are never secrets.
- **What a flow sees**: the workspace's own flows, library agents and connectors first, then the
  studio-wide ones. A workspace connector keeps its secret in the keyring under its workspace, so
  two workspaces can use different credentials under the same name. Runs, chats and `previous.*`
  are kept per workspace (`runs/@<workspace>/`).
- **Board**: the **Board** button shows the workspace's flows as cards (inputs, outputs, last run),
  linked by where an input usually comes from, by subflows and by the values passed in runs; the
  side panel edits roots, variables and environments. In the Board, the top bar keeps only what
  works there (Flow editor, New, Connectors).
- **Workspace assistant**: the **Assistant** tab of the Board side panel is a chat about the whole
  workspace. It reads the settings, the context of each root, the flows, the connectors and the
  library, answers questions (citing the context section), and **proposes**: workspace settings
  (roots, variables, environments), a new flow (from a template or written in full, always valid,
  autonomy `approve`/`approve_prod`, external MCPs unapproved) or a connector (read-only, no
  secrets). Each proposal is a card; **you** apply it (then add the secret / sign in / give write
  access in the Connectors window). It never writes by itself. Saved commands and the
  conversation history work as in the flow assistant. A new flow can be proposed short (agents
  with a description, gates, edges): the studio fills in graph execution, the start, a task with
  `{input}` and a prompt per agent, and gives every problem back at once.
- **Human gates in graph mode**: the links out of a gate decide with `if: <gate>.decision == "approve"`
  (also `"reject"` and `"answer"`). A reject with no matching link ends the run; **Answer and
  continue** with no matching link asks again (it no longer ends the run with everything after
  the gate skipped). An agent can read the person's words with `inputs: {feedback: {from:
  <gate>.comment, required: false}}` (also `<gate>.decision`; empty until the gate was asked), so a
  loop back to a planner can use what was answered or rejected.
- **Resume a run**: a graph run that ended with an error (or was stopped) can be resumed from the
  Run panel (**Resume**, also on a saved run opened from "Past runs", even after a restart). The
  steps that finished well keep their work; the failed or interrupted step, and what depends on
  it, run again in the same run folder (a failed attempt does not count for `max_runs`; human
  approvals that depended on redone work are asked again). Each finished step records a
  fingerprint of its definition and prompt: steps edited since they ran are shown and are repeated
  unless you tick *keep the work of the edited steps*.
- **Run from a step** (**Run from here** in a node's context menu, or the mode *From a step* in the
  Run panel): goes on with a saved run from that step, whatever state the run ended in (even
  done). The step and everything after it run again; the earlier steps keep their work (same run
  folder and log). For the inputs of that step you choose: use the outputs the earlier steps saved,
  or define them by hand (prefilled with the saved values). **Rename** a saved run with the ✎ in
  "Past runs" (a name only; the folder and id do not change, `runs/<id>/run_meta.json`).
- **Unused flow inputs**: an agent only receives the flow inputs it declares
  (`inputs: {x: {from: input.x}}`) or that the `task` names (`{x}`). The Problems tab warns (⚠, the
  flow still saves and runs) about a flow input nothing uses; the assistants fix it before they
  propose or finish a flow.
- **Assistant turns**: both assistants may take up to 60 model turns per message; while working the
  chat shows "turn n of 60". At the limit the chat says so (no error) and **Continue** goes on in
  the same session.
- **Flows that feed each other**: a flow input can say where its value usually comes from
  (`inputs.plan.source: {flow: feature-analysis, output: planner.plan}`); in the Run panel,
  **Fill from a run…** takes the value from a finished run of that flow (typing it stays possible),
  and the run records where it came from (`io/_handoff.json`).
- **Subflows**: a node with `subflow: {flow, inputs, outputs}` runs another flow of the workspace
  as one step (graph mode, no human gates inside, at most 3 levels, no cycles); its events appear
  indented in the log and its mapped outputs (or its stop `message`) become the node's outputs.
- **Workspace context**: `context.md` in the workspace folder says what each root is (stack,
  layout, commands, conventions, key files) plus your own notes; every agent of the workspace's
  flows gets it in its prompt (`use_context`), so flows do not explore the same repository again.
  The **context keeper** (a system agent with read-only tools on one root) writes it: **Explore**
  per root on the board (offered when you add a root; **Browse…** picks the folder), **Update
  workspace context** (what changed since the last commit, plus what a run did), and after each
  run per flow `context_update`: `suggest` (a button in the Run panel, the default), `always` (a
  last system step, drawn on the canvas) or `never`. The studio stores the sections; the model
  never writes a file. `context.json` keeps the commit of each update.
- **History from past Claude Code sessions**: **Learn** on a root of the board lists the Claude
  Code sessions and memory notes about that folder on this machine (`~/.claude/projects/…` or
  `CLAUDE_CONFIG_DIR`; this studio's own runs and other programs' sessions are set apart). You
  pick what to read and see the size first. One reader per source (only the conversation text:
  no tool output, injected reminders removed, secret-like text masked) keeps the decisions,
  conventions, known problems and open work; then the **history keeper** (read-only tools on the
  root) checks them against the code and drafts the root's **history** section. You review and
  edit the draft before it is saved in `context.md` (`<root>.history`, after the root's section;
  agents get it last, so a long context drops it first). Which sessions were learned is kept on
  the machine (`runs/@<ws>/.context/`), so next time only new or changed ones are offered.
- **Copy and duplicate**: **Copy to…** on a flow, an agent of a flow, a library agent, a connector
  or selected variables/environments copies it to another workspace (or duplicates it in the same
  one), after a check of what it uses that the target lacks (variables, roots, connectors, library
  agents, flows) — copy it too, use the target's, or leave it. Secrets are never copied in files:
  *Use the same secrets on this machine* copies the keyring entry without showing it.
  **Move to workspace…** does the same copy, then takes the original out (a flow or library
  agent to `.trash`, a connector with its keyring secret, variables/environments, an agent out
  of its flow with its links); shared items (built-in or extension agents) and the start agent
  are only copied.
- CLI: `agent-flow-studio … --workspace <name|folder> --env <env>`;
  `agent-flow-studio-flows --workspace <name> list | validate <flow>` and
  `agent-flow-studio-flows workspaces`, `agent-flow-studio-flows --workspace <name> context
  [--explore | --update] [--root x]`.

## Flow templates
**New** in the editor (or `agent-flow-studio-flows new`) can start from a template; you fill in its
parameters (repository folder, base branch, connector…):
- **code-review** — reviews the current branch against the base branch (read-only git) and writes
  the review as a draft for approval.
- **test-runner** — runs your test command (you approve it first) and diagnoses each failure,
  without changing code.
- **fix-and-pr** — reads an issue (GitHub, Azure DevOps or GitLab) through a connector, fixes it
  on `fix/<issue>`, runs the tests, pushes the branch and, after your approval, opens a draft PR.
- **ticket-triage** — the ticket comes as pasted text, a URL or a number (the run form shows only
  the field you need, with `show_if`); a URL or a number is read with a connector action (API or
  website, read only), and from there the triage is the same: summary, category, priority, next
  steps, as a draft for review.

Flow inputs can have `show_if` (an expression on the inputs declared before them, e.g.
`input.source == "url"`): the field is shown, and required, only when it holds.

Templates live in `src/agent_flow_studio/templates/<id>/` (`template.yaml` + `flow.yaml` +
`prompts/`); `{{param}}` placeholders are filled in on creation. An extension can add its own in
`extensions/<name>/templates/`. All templates start with autonomy "approve each write".

```bash
agent-flow-studio-flows templates
agent-flow-studio-flows new review-app --template code-review --set workspace=D:/repos/app
```

## Assistant conversations
Each reply saves the conversation of the open flow on this machine (`runs/chats/<flow>/`,
outside git), with the model session, so it is still there after a reload or when you come back
to the flow, and it continues with its context. **New conversation** starts another one (the old
one stays); **History** lists the flow's conversations to open, rename or delete them.

## Saved assistant commands
In the assistant, **☆ Save** (next to Send, or on any message you sent) keeps a message as a
**command**: a name, the message, and **instructions** (rules, context, format…) that go to the
assistant with the message without showing in the chat. Commands appear as small chips above the
chat input (favorites first); a click puts the message in the input with its instructions
attached. "⋯" opens the list to see the details, use, edit, mark as favorite or delete. They are
stored on this machine in `assistant_commands.yaml` (or `AFS_COMMANDS_FILE`), outside git.

## Agent library
Generic agents you reuse across flows. In the editor, palette → **Agent library**: drag or click
an agent to **copy** it into the flow, then tune it there (prompt, tools, model…); link its inputs
and place it wherever the flow needs it. A copy remembers where it came from (`origin` in
`flow.yaml`), so the node shows a "lib" badge and the properties say whether it was changed in this
flow and whether the library has a newer version. Nothing in a flow changes when the library
changes: **Update from library** (on the node's right-click menu or in its properties) brings the
new version in on request, keeping the input links, join, loops and icon; **Detach** drops the
link. **Save to library…** stores any agent of a flow in your local library. The chat assistant
knows the library too: it checks it before writing a new agent and copies one when it fits. It
can also **create or edit** library agents (from scratch, from an agent of the flow or from a
library agent): it shows the proposal in the chat and you save it with "Save to library" — the
assistant itself never writes to the library. Built-in and extension agents are read only (edit
a copy under a new id). Library agents only use catalog tools (external MCPs belong to a flow).

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

## Where things live (public repo, this machine, extensions)
Nothing you create in the editor goes to the public repository by default:
- **Flows** live in `flows/`, which git ignores except the public examples listed one by one in
  `.gitignore` (today `flows/repo-review/`). The flow picker marks a public flow with **public (git)**.
- **Connectors** (`connections.yaml`), **library agents** (`agents/`), saved assistant
  commands, conversations and runs stay on this machine.
- **Extensions** (`extensions/<name>/`) are the other place: "Where to keep it" (new flow, save to
  library, connector) can put a flow, an agent or a connector definition there, so the extension
  — ideally its own private git repository — carries them to other machines (see
  [`extensions/README.md`](extensions/README.md)).
- **Public (git)** is a third, explicit choice (never the default) for flows (`flows/<name>/` with
  an exception in `.gitignore`), library agents (the built-in library in the package) and
  connectors (`connections.public.yaml`, definitions only — e.g. a public site that needs an
  account). Click the location tag next to the flow picker to move a flow between places; the
  library window (palette → Agent library → Manage) edits, duplicates, moves and deletes agents.
- **Private terms**: list company or client names, internal domains… in `private_terms.txt`
  (outside git) or `extensions/<name>/private_terms.txt`; anything that names one of them cannot
  be made public (the editor offers to keep it on this machine instead).

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
  editor/         server.py (API), assistant.py, ws_assistant.py, runner.py, validation.py, yaml_io.py, static/ (build)
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

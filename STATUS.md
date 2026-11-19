# STATUS — shared status

Hand-over file between machines/accounts. **When you start:** read this. **When you finish a task:**
update "Current state" and "Next steps", add a line to the "Log" (date, machine/account in generic
terms — no emails —, what was done) and commit + push.

## Current state (2026-11-19)
Works and is tested (`pytest`: core 147 tests, without extensions):
- **Repository in English**: code, messages, prompts, flows, tests and docs. Only the chat with the
  user is in Portuguese (rule in `CLAUDE.md`).
- **Multilingual editor UI** (`pt`, `en`, `es`): selector in the toolbar, choice saved in the
  browser (`localStorage` `afs.lang`), first default from the browser language. Strings in
  `editor/src/i18n/<lang>.ts` (`en.ts` is the reference; `tsc` fails if a language misses a key).
  The assistant gets `lang` and replies in that language. Backend messages are English only.
- **Editor themes**: System (OS light/dark), Light, Dark, Sepia, Ocean, High contrast; selector in
  the toolbar, saved in `localStorage` `afs.theme`. Tokens in `editor/src/styles.css`, list in
  `editor/src/theme.tsx`; `editor/public/theme-init.js` sets the theme before render (no flash,
  no inline script, so the CSP stays strict).
- **Flows in YAML** (`flows/<name>/flow.yaml`): agents, gates (human approval / stop), links with
  conditions, artifacts with contracts, approved external MCPs, workspace and autonomy.
- **Visual editor** (`agent-flow-studio-editor`, http://127.0.0.1:8765): React Flow canvas, palette,
  properties panel, live validation, create/duplicate/delete flows, undo/redo.
- **Chat assistant** in the editor: builds/changes the flow in a draft (never saves on its own).
- **Run** in the editor: live trace on the canvas, artifacts, gates with a decision, live write
  approvals, and test modes: full flow, step by step, until an agent, single agent.
- **Connectors** (Connectors button): HTTP API, Azure ARM, Azure DevOps, Microsoft Graph,
  GitHub, GitLab, Atlassian Cloud; secrets in the keyring or `${VAR}`; test connection;
  `http_get` per agent; `http_write` with `access: write`.
- **Connectors phase 2**: **sign-in on a page** (`oauth_browser`: OAuth 2 + PKCE, "Sign in"
  button, callback `/api/oauth/callback`, tokens only in the keyring, refresh token renewal,
  Meta long-lived token exchange, token in header or `?access_token=`) for Entra ID (Azure,
  DevOps, Graph), GitHub, GitLab and any HTTP API; presets for Facebook, Instagram, Threads,
  WhatsApp Business and Reddit. **Azure managed identity** (IMDS and App Service/Container Apps).
  Tested with mocked providers only: no real sign-in was tried yet (needs app registrations).
- **Writes with autonomy** (`approve` / `approve_prod` / `auto`) and a confined workspace; the hook
  denies paths outside the workspace, force pushes/pushes to protected branches and destructive
  commands.
- **Local extensions** (`extensions/<name>/`, outside git) for domain tools/contracts.
- Public example flow: `flows/repo-review` (repository review, read only).

**Note for the other machine:** after the translation, the local flows and the extension there
still have Portuguese prompts/texts (outside git — translate them there if you want). Defaults
changed: `input.label` "Entrada" → "Input", `task` "Executa o flow para {input}." → "Run the flow
for {input}.", the "## Fluxo" section of the orchestrator is now "## Flow", and the `fluxo` key in
the validation API is now `flow_section`. The assistant tools have English names (`view_flow`,
`set_agent`, `add_edge`…). No YAML key, enum value, tool id or env var changed.

## Next steps (backlog, in suggested order)
1. [x] Multilingual interface: pt, en, es (to start with).
2. [x] Theme choice (light/dark/system, and others).
3. [x] Connectors phase 2: sign-in on a page (browser session), managed identity, more types.
   - [ ] Try a real sign-in per provider (register an app, check the presets). Instagram and
     Threads only accept an https redirect URI, so they need an https tunnel or a hosted editor.
4. [ ] Code flow templates (review, tests, PR) using workspace + connectors.
5. [ ] Writes through external MCPs (today read only).
6. [ ] More flows/templates and skills (phase 4 of the builder).
7. [ ] (Optional) Translate backend messages (validation, errors) to the UI language.

## Getting started on a new machine
```powershell
git clone <url> agent-flow-studio; cd agent-flow-studio
python -m venv .venv; .\.venv\Scripts\python -m pip install -e ".[dev]"
cd editor; npm install; npm run build; cd ..
.\.venv\Scripts\pytest
.\.venv\Scripts\agent-flow-studio-editor
```
SDK authentication: Claude Code login on this machine (or `ANTHROPIC_API_KEY` in `.env`).

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

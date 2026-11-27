# Operations — common commands

Commands for Windows PowerShell, run from the repository folder. On macOS/Linux use
`.venv/bin/...` instead of `.\.venv\Scripts\...`.

## Start and stop the editor
```powershell
.\.venv\Scripts\agent-flow-studio-editor                  # http://127.0.0.1:8765 (opens the browser)
.\.venv\Scripts\agent-flow-studio-editor --no-browser --port 8765
```
Stop it: `Ctrl+C` in its terminal. When you do not know where it runs:
```powershell
Get-Process agent-flow-studio-editor -ErrorAction SilentlyContinue | Stop-Process
```
What holds a port (e.g. 8765):
```powershell
netstat -ano | Select-String ":8765 .*LISTENING"
```

## After a pull (or on a new machine)
Stop the editor first: on Windows a running `agent-flow-studio-editor.exe` is locked and `pip`
stops halfway.
```powershell
git pull
.\.venv\Scripts\python -m pip install -e ".[dev,web]"     # web: Playwright (website connectors, preview.screenshot)
cd editor; npm install; npm run build; cd ..
```
Then start the editor again. If the package disappeared from the venv after a stopped install,
delete `.venv\Lib\site-packages\~gent_flow_studio-*` and install again.

First time on a machine:
```powershell
python -m venv .venv
.\.venv\Scripts\python -m pip install -e ".[dev,web]"
cd editor; npm install; npm run build; cd ..
```

## Tests and lint
```powershell
$env:AFS_NO_EXTENSIONS = "1"; .\.venv\Scripts\python -m pytest -q; Remove-Item Env:AFS_NO_EXTENSIONS
.\.venv\Scripts\python -m pytest -q                       # with the local extensions
.\.venv\Scripts\python -m ruff check .
cd editor; npm test; cd ..                                # editor unit tests (Node 23.6+)
cd editor; npx tsc --noEmit -p .; cd ..                   # editor types
```
One test file: `.\.venv\Scripts\python -m pytest -q tests\test_run_history.py`.
Long Windows temp paths can skip some tests: add `--basetemp D:\ptmp` (any short folder).

CI (`.github/workflows/ci.yml`) runs the same on every push to `main` and every pull request:
ruff + the core tests (Python 3.10 and 3.13 on Linux, 3.12 on Windows) and the editor (types,
tests, build). See the runs in the repository's **Actions** tab, or:
```powershell
gh run list --limit 5
gh run view --log-failed
```

## Editor development
```powershell
cd editor; npm run dev                                    # http://localhost:5173, /api goes to 8765
cd editor; npm run build                                  # the build the studio serves
```

## Flows from the command line
```powershell
.\.venv\Scripts\agent-flow-studio-flows list
.\.venv\Scripts\agent-flow-studio-flows validate <flow>
.\.venv\Scripts\agent-flow-studio-flows show <flow>
.\.venv\Scripts\agent-flow-studio-flows catalog            # tools and contracts
.\.venv\Scripts\agent-flow-studio-flows templates
.\.venv\Scripts\agent-flow-studio-flows agents             # agent library
.\.venv\Scripts\agent-flow-studio-flows new <flow> --template <id> --set <param>=<value>
.\.venv\Scripts\agent-flow-studio-flows providers          # model providers (providers.yaml)
.\.venv\Scripts\agent-flow-studio-flows provider-key <name> # save a provider key in the keyring
.\.venv\Scripts\agent-flow-studio <input> --flow <flow> [--workspace <ws>] [--env <env>]
```
From the command line, writes that need an approval are denied (nobody can approve them).

## Claude Code
Sign in for the agents (they run through the Claude Code CLI): `claude`, then `/login`. Or set
`ANTHROPIC_API_KEY` before you start the editor (`.env` is not read).

The studio inside Claude Code (the editor must run). Claude Code starts the command itself, so
give the full path of the venv's program:
```powershell
claude mcp add agent-flow-studio -- "$PWD\.venv\Scripts\agent-flow-studio-mcp.exe"
claude mcp add agent-flow-studio -- "$PWD\.venv\Scripts\agent-flow-studio-mcp.exe" --url http://127.0.0.1:8765 --workspace <ws>
claude mcp list
claude mcp remove agent-flow-studio
```
Go on with a step's session (Run panel → step ⋯ → Copy the Claude Code command gives it):
```powershell
Set-Location -LiteralPath '<folder of the step>'; claude --resume <session id>
```

## Git
```powershell
git status
git log --oneline -10
git add -A; git commit -m "<message>"; git push
```
Before you push: nothing internal to companies or clients (see `CLAUDE.md`, rule 8), no secrets.

## Where things are
- Runs: `runs\<run>\` (studio) or `runs\@<workspace>\<run>\` — `events.jsonl`, `files\`,
  `outputs\`, `decisions.jsonl`; the queue in `runs\.queue.json`; the audit in `runs\audit.jsonl`.
- Workspaces registry: `~\.agent-flow-studio\workspaces.json`; browser profiles of website
  connectors: `~\.agent-flow-studio\browser\`.
- Claude Code sessions: `~\.claude\projects\` (or `CLAUDE_CONFIG_DIR`).
- Model providers: `providers.yaml` (copy `providers.example.yaml`; keys only `${VAR}` or keyring).
- Hand-over between machines: `STATUS.md`.

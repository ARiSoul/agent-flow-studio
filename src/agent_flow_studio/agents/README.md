# Core agent library

Generic agents that the editor (palette → "Agent library") copies into a flow, where they can be
tuned. One folder per agent: `agent.yaml` (an agent as in a flow, plus `title`; inputs without
`from`) and `prompt.md`. See `builder/library.py`. Keep them generic: company agents go in
`extensions/<name>/agents/`, and agents saved from the editor go in the local `agents/` folder
(outside git).

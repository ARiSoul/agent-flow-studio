# Local extensions

Each subfolder with an `__init__.py` is loaded at startup (`AFS_EXTENSIONS_DIR` changes the folder;
`AFS_NO_EXTENSIONS=1` turns them off). The contents of this folder **do not go into git** (only this
README): it holds the internal tools and rules of a company or client.

An extension package can expose:

```python
# extensions/my_company/__init__.py
from pydantic import TypeAdapter
from .tools import crm            # @tool tools from claude_agent_sdk
from . import models

TOOL_GROUPS = {"crm": [crm.get_customer]}             # "server" -> tools (ids "crm.get_customer")
CONTRACTS = {"Customer": TypeAdapter(models.Customer)}  # contracts for JSON artifacts
REPORTS = {"my-flow": lambda run_id: ...}               # optional report at the end of a run (CLI)
```

A `templates/` folder inside the extension adds flow templates (same format as
`src/agent_flow_studio/templates/<id>/`); their ids must not collide with the core ones.

An extension is also a **place to keep things outside the public repository** — the editor offers
it in "Where to keep it" when you create a flow, save an agent to the library or edit a connector:

```
extensions/my_company/
  flows/<flow>/            flows (flow.yaml + prompts/), next to the ones in flows/
  agents/<id>/             library agents (agent.yaml + prompt.md)
  connections.yaml         connector definitions, no secrets (each machine sets its own)
  private_terms.txt        words that must not reach the public repo (one per line)
```

Make the extension folder **its own private git repository** to take these to other machines
without going through the public one. Names are unique across all places (a flow or a connector
cannot exist both in `flows/` and in an extension).

Rules: server names must not collide with the core ones (`artifacts`, `connections`,
`flow_control`, `workspace`) or between extensions; tools return `ok(...)`/`err(...)` from
`agent_flow_studio.tools.common`; tests go in `extensions/<name>/tests/` (the root `pytest` picks
them up).

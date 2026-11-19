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

Rules: server names must not collide with the core ones (`artifacts`, `connections`,
`flow_control`, `workspace`) or between extensions; tools return `ok(...)`/`err(...)` from
`agent_flow_studio.tools.common`; tests go in `extensions/<name>/tests/` (the root `pytest` picks
them up).

# Extensões locais

Cada subpasta com `__init__.py` é carregada no arranque (`AFS_EXTENSIONS_DIR` muda a pasta;
`AFS_NO_EXTENSIONS=1` desliga). O conteúdo desta pasta **não vai para o git** (só este README):
serve para ferramentas e regras internas de uma empresa ou cliente.

Um pacote de extensão pode expor:

```python
# extensions/minha_empresa/__init__.py
from pydantic import TypeAdapter
from .tools import crm            # ferramentas @tool do claude_agent_sdk
from . import models

TOOL_GROUPS = {"crm": [crm.get_customer]}             # "servidor" -> ferramentas (ids "crm.get_customer")
CONTRACTS = {"Customer": TypeAdapter(models.Customer)}  # contratos para artefactos JSON
REPORTS = {"o-meu-flow": lambda run_id: ...}            # relatório opcional no fim de uma execução (CLI)
```

Regras: nomes de servidores não podem colidir com os do núcleo (`artifacts`, `connections`,
`flow_control`, `workspace`) nem entre extensões; ferramentas devolvem `ok(...)`/`err(...)` de
`agent_flow_studio.tools.common`; testes em `extensions/<nome>/tests/` (o `pytest` da raiz apanha-os).

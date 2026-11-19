# CLAUDE.md — Agent Flow Studio

## Antes de tudo: STATUS.md
Este projeto é trabalhado em várias máquinas e contas (a memória do Claude Code é por conta e não
passa entre elas). **No início de cada sessão lê `STATUS.md`**: diz em que ponto o trabalho ficou e o
que vem a seguir. **No fim de cada tarefa atualiza `STATUS.md`** (estado atual, próximos passos e uma
linha no registo) e propõe commit e push, para a outra máquina continuar a partir daí.
Se existir `CLAUDE.local.md` (fora do git), segue também essas instruções locais.

## Objetivo
Estúdio para montar, testar e correr **fluxos de agentes** (Claude Agent SDK, Python): flows em YAML,
editor visual (React Flow + FastAPI), assistente de construção por chat, execução com trace,
aprovações humanas, conectores (Azure, Azure DevOps, GitHub, APIs) e escrita com autonomia controlada.
Ver `README.md`.

## Regras invioláveis
1. **Só leitura por defeito.** Escrever só é possível com ferramentas de escrita declaradas no flow —
   `workspace.edit|write|bash` (dentro do workspace) e `connections.http_write` (conectores com
   `access: write` atribuídos ao agente) — e segue a **autonomia** do agente/flow (`approve` ·
   `approve_prod` · `auto`), aplicada por `policy.py` no hook. O orquestrador nunca escreve. Sem quem
   aprove (CLI), escritas que precisem de aprovação são recusadas. Rascunhos ficam em `runs/<id>/`.
2. **Segredos** só em variáveis de ambiente/`.env` ou no keyring do sistema (conectores) — nunca no
   código, nos flows, em `connections.yaml`, na UI nem em logs. Num flow, segredos são sempre `${VAR}`.
   Não registar PII nem conteúdos de ficheiros em `runs/audit.jsonl`.
3. **Toda a conclusão tem evidência**: cada agente cita a fonte (ficheiro:linha, pedido, registo).
4. Ferramentas internas seguem o padrão de `src/agent_flow_studio/tools/` e são registadas em
   `registry.py`; ferramentas de um domínio/empresa vão para uma **extensão** (`extensions/<nome>/`,
   fora do git — ver `extensions.py`).
5. **MCPs externos** só entram num flow com `approved: true`, `read_only: true` e lista fechada de
   ferramentas (sem curingas). O hook aplica a lista por flow e por subagente.
6. **Autonomia `auto`** só é escolhida por uma pessoa no editor (o assistente só usa `approve` /
   `approve_prod`). Mesmo em `auto`, o hook recusa caminhos fora do workspace, push forçado ou para
   branches protegidas e comandos destrutivos; Bash não é sandbox (por isso é tratado como escrita).
7. Não remover o hook de guarda, `dontAsk`, `setting_sources=[]` nem a validação de contratos.
8. **Repositório público:** nada interno de empresas/clientes entra no git (código, flows, nomes de
   sistemas, dados). Isso vive em `extensions/`, `flows/` ignorados localmente e `CLAUDE.local.md`.

## Arquitetura
- `builder/`: `spec.py` (formato do flow + validação), `catalog.py` (ferramentas, contratos),
  `compile.py` (flow → `ClaudeAgentOptions`; secção "Fluxo" do orquestrador gerada do grafo).
- `hooks.py` (guarda + auditoria) e `policy.py` (escrita, workspace, Bash, autonomia, aprovações,
  limite de delegações para testes passo a passo).
- `connections/`: perfis em `connections.yaml` (fora do git), segredos `keyring`/`${VAR}`,
  `http_get` (leitura) e `http_write` (escrita, só com `access: write`).
- `editor/`: `server.py` (API local, 127.0.0.1, Hosts locais, cabeçalho `X-Flow-Editor` em escritas),
  `assistant.py` (chat: só altera um rascunho em memória), `runner.py` (execuções full/step/until/agent,
  gates, aprovações ao vivo, decisões em `runs/<id>/decisions.jsonl`), `yaml_io.py`, `validation.py`.
- `extensions.py`: carrega `extensions/<nome>/` (TOOL_GROUPS, CONTRACTS, REPORTS).
- Frontend em `editor/` (Vite + React + @xyflow/react) → build em `src/agent_flow_studio/editor/static/`.

## Convenções
- Python ≥ 3.10, tipagem, `ruff` (linha 100). Prompts em PT-PT em `flows/<flow>/prompts/*.md`.
- Ferramentas devolvem `ok(...)`/`err(...)` de `tools/common.py`.
- Testes do núcleo usam o flow genérico `tests/fixtures/flows/pipeline` e têm de passar sem extensões
  (`AFS_NO_EXTENSIONS=1 pytest tests`). Testes de extensões ficam em `extensions/<nome>/tests`.

## SDK (verificado contra claude-agent-sdk 0.2.185 / CLI 2.1.328)
Delegação: ferramenta `Agent` (`Task` é alias). `@tool` devolve `{"content": [...], "is_error"?}`.
Subagentes aceitam ferramentas MCP pelo nome completo `mcp__servidor__ferramenta`.

## Comandos
- `pytest` · `AFS_NO_EXTENSIONS=1 pytest tests` · `ruff check .`
- `agent-flow-studio <entrada> --flow <flow> [--resume <session_id>]`
- `agent-flow-studio-flows list|validate <flow>|show <flow>|catalog`
- `agent-flow-studio-editor [--port 8765] [--flows <pasta>]` · frontend: `cd editor && npm run build`

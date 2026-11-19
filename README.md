# Agent Flow Studio

Estúdio local para **montar, testar e correr fluxos de agentes** com o
[Claude Agent SDK](https://docs.claude.com/en/api/agent-sdk/overview) (Python): flows em YAML, editor
visual estilo n8n/Langflow, assistente de construção por chat, execução com trace, aprovações
humanas, conectores e escrita com autonomia controlada.

> Ponto de situação e próximos passos: [`STATUS.md`](STATUS.md).

## Conceitos
- **Flow** (`flows/<nome>/flow.yaml` + `prompts/*.md`): um grafo de **agentes** (subagentes com prompt,
  ferramentas, modelo, skills, artefactos que lêem/escrevem), **gates** (aprovação humana ou paragem)
  e **ligações** com condição em linguagem natural. Um orquestrador recebe o grafo como instruções.
- **Ferramentas**: `artifacts.*` (resultados intermédios em `runs/<id>/`, com contratos opcionais),
  `connections.http_get|http_write` (conectores), `workspace.read|glob|grep|edit|write|bash`
  (ficheiros/comandos dentro do workspace do flow), MCPs externos aprovados e ferramentas de extensões.
- **Segurança** (não depende do modelo): lista fechada de ferramentas por flow e por subagente no
  hook, sem ferramentas nativas fora do workspace, `dontAsk`, sem settings do utilizador, segredos
  fora dos ficheiros, auditoria em `runs/audit.jsonl` (sem conteúdos de ficheiros).

## Editor visual
`agent-flow-studio-editor` abre http://127.0.0.1:8765:
- **Canvas**: arrastar agentes e gates, ligar nós, organizar, desfazer/refazer; validação ao vivo com
  as mesmas regras do runtime; só grava flows válidos (cópia `flow.yaml.bak`; apagar → `flows/.trash/`).
- **Propriedades**: prompts (em linha ou ficheiro), ferramentas, artefactos e contratos, modelo,
  conectores, autonomia, workspace, MCPs externos.
- **Assistente**: chat que cria/altera agentes, ligações, prompts, MCPs… num rascunho; tu revês e
  gravas. Não aprova MCPs nem escolhe autonomia `auto`.
- **Executar**: corre o flow gravado com uma entrada e mostra o agente ativo, os concluídos e o gate
  onde parou, o trace, o custo e os artefactos. Modos: *flow completo*, *passo a passo* (pausa depois
  de cada agente), *até ao agente…* e *só um agente (isolado)*. Gates e escritas pedem decisão ao vivo.
- **Conectores**: API HTTP (sem auth, chave, bearer, basic, client credentials), Azure Resource
  Manager (az login, client credentials), Azure DevOps (PAT, az login, client credentials) e GitHub
  (PAT, gh auth login), com ambiente, caminhos permitidos, acesso leitura/escrita e "Testar ligação".
  Segredos no Gestor de Credenciais do sistema (keyring) ou como `${VAR}`.

## Escrita com autonomia
No flow defines a **autonomia** (aprovar cada escrita · aprovar só em produção · autónomo) e o
**workspace** (pasta/repositório, ambiente, branches protegidas); cada agente pode sobrepor a
autonomia. Quando uma escrita precisa de aprovação, a execução pára e o editor mostra o ficheiro e
conteúdo, o comando ou o pedido HTTP. O hook recusa sempre caminhos fora do workspace, push forçado
ou para branches protegidas e comandos destrutivos.

## Extensões locais
Ferramentas, contratos e relatórios de um domínio/empresa ficam em `extensions/<nome>/` (fora do
git), num pacote Python que expõe `TOOL_GROUPS`, `CONTRACTS` e `REPORTS` (ver
[`extensions/README.md`](extensions/README.md) e `src/agent_flow_studio/extensions.py`).

## Arranque
```bash
python -m venv .venv && source .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -e ".[dev]"
cd editor && npm install && npm run build && cd ..      # frontend (Node 20+)
pytest
agent-flow-studio-editor                                # editor em http://127.0.0.1:8765
agent-flow-studio-flows list | validate <flow> | show <flow> | catalog
agent-flow-studio teste-001 --flow repo-review          # pela CLI (escritas com aprovação são recusadas)
```
Autenticação: login do Claude Code na máquina ou `ANTHROPIC_API_KEY`. Desenvolvimento do frontend:
`cd editor && npm run dev` (porta 5173, encaminha `/api` para 8765).

## Estrutura
```
src/agent_flow_studio/
  builder/        spec.py (formato + validação), catalog.py, compile.py
  editor/         server.py (API), assistant.py, runner.py, validation.py, yaml_io.py, static/ (build)
  connections/    model.py, store.py (keyring/${VAR}), client.py
  tools/          artifacts.py, connections.py, guards.py, common.py
  hooks.py        guarda (lista fechada) + auditoria · policy.py  escrita, workspace, autonomia
  extensions.py   carregamento de extensões locais · registry.py  servidores MCP em processo
  main.py · orchestrator.py · flows_cli.py
editor/           frontend (Vite + React + React Flow)
flows/            flows (exemplo: repo-review)
extensions/       extensões locais (fora do git, exceto o README)
tests/            testes do núcleo (flow genérico em tests/fixtures/flows/pipeline)
```

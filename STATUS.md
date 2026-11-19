# STATUS — ponto de situação partilhado

Ficheiro de passagem entre máquinas/contas. **Ao começar:** lê isto. **Ao terminar uma tarefa:**
atualiza "Estado atual" e "Próximos passos", acrescenta uma linha ao "Registo" (data, máquina/conta
em termos genéricos — sem emails —, o que foi feito) e faz commit + push.

## Estado atual (2026-11-19)
Funciona e está testado (`pytest`: núcleo 118 testes, sem extensões):
- **Flows em YAML** (`flows/<nome>/flow.yaml`): agentes, gates (aprovação humana / paragem),
  ligações com condições, artefactos com contratos, MCPs externos aprovados, workspace e autonomia.
- **Editor visual** (`agent-flow-studio-editor`, http://127.0.0.1:8765): canvas React Flow, paleta,
  painel de propriedades, validação ao vivo, criar/duplicar/apagar flows, desfazer/refazer.
- **Assistente por chat** no editor: constrói/altera o flow num rascunho (nunca grava sozinho).
- **Executar** no editor: trace ao vivo no canvas, artefactos, gates com decisão, aprovações de
  escrita ao vivo, e modos de teste: flow completo, passo a passo, até um agente, só um agente.
- **Conectores** (botão Conectores): API HTTP, Azure ARM, Azure DevOps, GitHub; segredos no keyring
  ou `${VAR}`; testar ligação; `http_get` por agente; `http_write` com `access: write`.
- **Escrita com autonomia** (`approve` / `approve_prod` / `auto`) e workspace confinado; hook recusa
  caminhos fora do workspace, push forçado/para branches protegidas e comandos destrutivos.
- **Extensões locais** (`extensions/<nome>/`, fora do git) para ferramentas/contratos de um domínio.
- Flow de exemplo público: `flows/repo-review` (revisão de repositório, só leitura).

## Próximos passos (backlog, por ordem sugerida)
1. [ ] Interface multilíngue: pt, en, es (para começar).
2. [ ] Escolha de tema (claro/escuro/sistema, e outros).
3. [ ] Conectores fase 2: login numa página (sessão de browser), managed identity, mais tipos.
4. [ ] Templates de flows de código (revisão, testes, PR) usando workspace + conectores.
5. [ ] Escrita via MCPs externos (hoje só leitura).
6. [ ] Vários flows/templates e skills (fase 4 do construtor).

## Como arrancar numa máquina nova
```powershell
git clone <url> agent-flow-studio; cd agent-flow-studio
python -m venv .venv; .\.venv\Scripts\python -m pip install -e ".[dev]"
cd editor; npm install; npm run build; cd ..
.\.venv\Scripts\pytest
.\.venv\Scripts\agent-flow-studio-editor
```
Autenticação do SDK: login do Claude Code nesta máquina (ou `ANTHROPIC_API_KEY` no `.env`).

## Registo
- 2026-11-19 · máquina de trabalho (conta A) · Projeto renomeado e reorganizado como
  agent-flow-studio; parte específica de empresa movida para uma extensão local fora do git; criado
  este STATUS.md e o flow de exemplo `repo-review`; repositório iniciado.

"""Assistente de construção de flows (chat no editor).

O modelo só tem as ferramentas `flow_builder`, que alteram um **rascunho em memória** do flow aberto
(nunca o disco). O editor recebe o rascunho no fim de cada mensagem; a pessoa revê e grava.
Mesma rede de segurança do runtime: sem ferramentas nativas, `dontAsk`, `setting_sources=[]` e
hook com lista fechada. O assistente não aprova MCPs externos: qualquer MCP que crie ou altere
fica com `approved: false` e `read_only: false` até a pessoa os marcar no painel.
"""
from __future__ import annotations

import copy
import json
import re
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ResultMessage,
    TextBlock,
    ToolUseBlock,
    create_sdk_mcp_server,
    query,
    tool,
)

from ..builder.catalog import BUILTIN_TOOLS, CONTRACTS
from ..builder.compile import DISALLOWED
from ..config import settings
from ..hooks import DELEGATION_TOOLS, make_guard, make_hooks
from ..tools.common import err, ok
from .validation import _validate

SERVER = "flow_builder"
PROMPT = (Path(__file__).parent / "assistant.md").read_text(encoding="utf-8")
SLUG = re.compile(r"^[a-z][a-z0-9_]{0,39}$")
MAX_TURNS = 40

_STR = {"type": "string"}
_STRS = {"type": "array", "items": _STR}
_WRITES = {"type": "array", "items": {"type": "object", "properties": {
    "name": _STR, "contract": _STR, "draft": {"type": "boolean"}}, "required": ["name"]}}


def _schema(props: dict[str, Any], required: list[str] | None = None) -> dict[str, Any]:
    return {"type": "object", "properties": props, "required": required or []}


def _contract_fields(name: str) -> dict[str, Any]:
    s = CONTRACTS[name].json_schema()
    if s.get("type") == "array":
        ref = s["items"].get("$ref", "").rsplit("/", 1)[-1]
        s = s.get("$defs", {}).get(ref, s["items"])
        return {"lista_de": sorted(s.get("properties", {}))}
    return {"campos": sorted(s.get("properties", {}))}


def _connections_info() -> dict[str, Any]:
    from ..connections import store

    try:
        return {n: f"{c.type} · {c.environment} · {c.description}".strip(" ·")
                for n, c in store.load().items()}
    except store.ConnectorConfigError as e:
        return {"erro": str(e)}


class Draft:
    """Rascunho do flow ({spec, prompts}) e as ferramentas que o alteram."""

    def __init__(self, spec: dict[str, Any], prompts: dict[str, str]):
        self.spec = copy.deepcopy(spec)
        self.prompts = dict(prompts)
        self.changed = False

    # --- utilitários
    def doc(self) -> dict[str, Any]:
        return {"spec": self.spec, "prompts": self.prompts}

    def validation(self) -> dict[str, Any]:
        r = _validate(self.spec, self.prompts)
        return {"ok": r["ok"], "errors": r["errors"], "fluxo": r.get("fluxo"),
                "allowed_tools": r.get("allowed_tools")}

    def _summary(self, what: str) -> dict:
        self.changed = True
        v = self.validation()
        state = "flow válido" if v["ok"] else "problemas: " + "; ".join(e["msg"] for e in v["errors"])
        return ok(f"{what}. Estado: {state}")

    def _nodes(self) -> set[str]:
        return set(self.spec.get("agents") or {}) | set(self.spec.get("gates") or {})

    def _edges(self) -> list[dict[str, Any]]:
        return self.spec.setdefault("edges", [])

    # --- ferramentas
    def tools(self) -> list[Any]:
        d = self

        @tool("ver_flow", "Mostra o flow aberto (rascunho atual) e os problemas de validação.",
              _schema({}))
        async def ver_flow(_: dict) -> dict:
            v = d.validation()
            return ok({"flow": d.spec, "ficheiros_de_prompt": sorted(d.prompts),
                       "valido": v["ok"], "problemas": [e["msg"] for e in v["errors"]]})

        @tool("ver_catalogo", "Ferramentas internas, contratos (com campos) e modelos disponíveis.",
              _schema({}))
        async def ver_catalogo(_: dict) -> dict:
            return ok({
                "ferramentas": {t.id: t.description for t in BUILTIN_TOOLS.values()},
                "contratos": {c: _contract_fields(c) for c in CONTRACTS},
                "modelos": ["inherit", "sonnet", "opus", "haiku"],
                "conectores_configurados": _connections_info(),
                "mcps_externos_do_flow": {n: m.get("tools", [])
                                          for n, m in (d.spec.get("mcp_servers") or {}).items()},
            })

        @tool("ver_prompt", "Lê o prompt de um agente ou do orquestrador (node='orchestrator').",
              _schema({"node": _STR}, ["node"]))
        async def ver_prompt(a: dict) -> dict:
            owner = (d.spec.get("orchestrator") or {}) if a["node"] == "orchestrator" \
                else (d.spec.get("agents") or {}).get(a["node"])
            if owner is None:
                return err(f"agente {a['node']!r} não existe")
            if owner.get("prompt_file"):
                return ok(d.prompts.get(owner["prompt_file"], "(ficheiro vazio)"))
            return ok(owner.get("prompt") or "(sem prompt)")

        @tool("alterar_flow", "Altera campos gerais do flow: title, description, model, max_turns, "
              "task (tem de conter {input}), input_label, input_pattern, start.",
              _schema({"title": _STR, "description": _STR, "model": _STR,
                       "max_turns": {"type": "integer"}, "task": _STR, "input_label": _STR,
                       "input_pattern": _STR, "start": _STR,
                       "autonomy": {"type": "string", "enum": ["approve", "approve_prod"]}}))
        async def alterar_flow(a: dict) -> dict:
            for k in ("title", "description", "model", "max_turns", "task", "start", "autonomy"):
                if k in a:
                    d.spec[k] = a[k]
            if "input_label" in a or "input_pattern" in a:
                inp = d.spec.setdefault("input", {})
                if "input_label" in a:
                    inp["label"] = a["input_label"]
                if "input_pattern" in a:
                    inp["pattern"] = a["input_pattern"]
            return d._summary("Flow atualizado")

        @tool("definir_agente",
              "Cria ou atualiza um agente. Só os campos indicados mudam. tools: ids do catálogo "
              "('servidor.ferramenta'); writes: [{name, contract?, draft?}]; reads: nomes de "
              "artefactos escritos por outros agentes; connections: conectores configurados que o "
              "agente pode ler (GET). O prompt de um agente novo fica em prompts/<nome>.md.",
              _schema({"name": _STR, "description": _STR, "prompt": _STR, "tools": _STRS,
                       "model": _STR, "max_turns": {"type": "integer"}, "skills": _STRS,
                       "reads": _STRS, "writes": _WRITES, "connections": _STRS,
                       "write_connections": _STRS,
                       "autonomy": {"type": "string", "enum": ["approve", "approve_prod"]}},
                      ["name"]))
        async def definir_agente(a: dict) -> dict:
            name = a["name"]
            if not SLUG.fullmatch(name):
                return err("nome inválido: minúsculas, dígitos e _, a começar por letra")
            agents = d.spec.setdefault("agents", {})
            if name in (d.spec.get("gates") or {}):
                return err(f"já existe um gate {name!r}")
            new = name not in agents
            if new and not a.get("description"):
                return err("um agente novo precisa de description")
            ag = agents.setdefault(name, {})
            if new:
                ag["prompt_file"] = f"prompts/{name}.md"
                d.prompts.setdefault(ag["prompt_file"], a.get("prompt") or "")
            if "connections" in a:  # conectores implicam a ferramenta de leitura
                tools = list(a.get("tools", ag.get("tools", [])))
                if a["connections"] and "connections.http_get" not in tools:
                    tools.append("connections.http_get")
                if not a["connections"]:
                    tools = [t for t in tools if t != "connections.http_get"]
                a = {**a, "tools": tools}
            if "write_connections" in a:  # escrita implica a ferramenta http_write
                tools = list(a.get("tools", ag.get("tools", [])))
                if a["write_connections"] and "connections.http_write" not in tools:
                    tools.append("connections.http_write")
                if not a["write_connections"]:
                    tools = [t for t in tools if t != "connections.http_write"]
                a = {**a, "tools": tools}
            for k in ("description", "tools", "model", "max_turns", "skills", "reads", "writes",
                      "connections", "write_connections", "autonomy"):
                if k in a:
                    ag[k] = a[k]
            if "prompt" in a:
                if ag.get("prompt_file"):
                    d.prompts[ag["prompt_file"]] = a["prompt"]
                else:
                    ag["prompt"] = a["prompt"]
            return d._summary(f"Agente {name} {'criado' if new else 'atualizado'}")

        @tool("definir_gate", "Cria ou atualiza um gate: kind 'human' (aprovação humana, retomável) "
              "ou 'stop' (paragem); label é o texto mostrado.",
              _schema({"name": _STR, "kind": {"type": "string", "enum": ["human", "stop"]},
                       "label": _STR}, ["name", "kind", "label"]))
        async def definir_gate(a: dict) -> dict:
            if not SLUG.fullmatch(a["name"]):
                return err("nome inválido: minúsculas, dígitos e _, a começar por letra")
            if a["name"] in (d.spec.get("agents") or {}):
                return err(f"já existe um agente {a['name']!r}")
            d.spec.setdefault("gates", {})[a["name"]] = {"kind": a["kind"], "label": a["label"]}
            return d._summary(f"Gate {a['name']} definido")

        @tool("ligar", "Liga dois nós (agente ou gate). 'when' é a condição em linguagem natural; "
              "se a ligação já existir, atualiza a condição.",
              _schema({"from": _STR, "to": _STR, "when": _STR}, ["from", "to"]))
        async def ligar(a: dict) -> dict:
            missing = [n for n in (a["from"], a["to"]) if n not in d._nodes()]
            if missing:
                return err(f"nós inexistentes: {missing}")
            for e in d._edges():
                if e["from"] == a["from"] and e["to"] == a["to"]:
                    e["when"] = a.get("when", e.get("when", ""))
                    return d._summary("Condição atualizada")
            d._edges().append({"from": a["from"], "to": a["to"], "when": a.get("when", "")})
            return d._summary(f"Ligação {a['from']} → {a['to']} criada")

        @tool("desligar", "Remove a ligação entre dois nós.",
              _schema({"from": _STR, "to": _STR}, ["from", "to"]))
        async def desligar(a: dict) -> dict:
            before = len(d._edges())
            d.spec["edges"] = [e for e in d._edges()
                               if not (e["from"] == a["from"] and e["to"] == a["to"])]
            if len(d.spec["edges"]) == before:
                return err("essa ligação não existe")
            return d._summary("Ligação removida")

        @tool("renomear_no", "Renomeia um agente ou gate (atualiza ligações, início e posições).",
              _schema({"old": _STR, "new": _STR}, ["old", "new"]))
        async def renomear_no(a: dict) -> dict:
            old, new = a["old"], a["new"]
            if old not in d._nodes():
                return err(f"{old!r} não existe")
            if not SLUG.fullmatch(new) or new in d._nodes():
                return err(f"nome {new!r} inválido ou já usado")
            for key in ("agents", "gates", "layout"):
                if d.spec.get(key) and old in d.spec[key]:
                    d.spec[key] = {(new if k == old else k): v for k, v in d.spec[key].items()}
            if d.spec.get("start") == old:
                d.spec["start"] = new
            for e in d._edges():
                e["from"] = new if e["from"] == old else e["from"]
                e["to"] = new if e["to"] == old else e["to"]
            return d._summary(f"{old} renomeado para {new}")

        @tool("remover_no", "Remove um agente ou gate e as suas ligações.",
              _schema({"name": _STR}, ["name"]))
        async def remover_no(a: dict) -> dict:
            n = a["name"]
            if n not in d._nodes():
                return err(f"{n!r} não existe")
            if n == d.spec.get("start"):
                return err("é o nó de início; muda o início (alterar_flow start) primeiro")
            for key in ("agents", "gates", "layout"):
                (d.spec.get(key) or {}).pop(n, None)
            d.spec["edges"] = [e for e in d._edges() if n not in (e["from"], e["to"])]
            return d._summary(f"{n} removido")

        @tool("definir_prompt_orquestrador", "Substitui o prompt do orquestrador (papel e regras; "
              "a secção Fluxo é gerada automaticamente).", _schema({"prompt": _STR}, ["prompt"]))
        async def definir_prompt_orquestrador(a: dict) -> dict:
            orch = d.spec.setdefault("orchestrator", {})
            if orch.get("prompt_file"):
                d.prompts[orch["prompt_file"]] = a["prompt"]
            else:
                orch["prompt"] = a["prompt"]
            return d._summary("Prompt do orquestrador atualizado")

        @tool("definir_mcp",
              "Declara ou altera um MCP externo. type stdio (command, args, env) ou http/sse (url "
              "https, headers). env/headers só com ${VAR}. tools: nomes exatos das ferramentas de "
              "leitura permitidas. Fica por aprovar: a pessoa marca Aprovado e Só leitura no painel.",
              _schema({"name": _STR, "type": {"type": "string", "enum": ["stdio", "http", "sse"]},
                       "description": _STR, "command": _STR, "args": _STRS,
                       "env": {"type": "object", "additionalProperties": _STR}, "url": _STR,
                       "headers": {"type": "object", "additionalProperties": _STR},
                       "tools": _STRS}, ["name", "type", "tools"]))
        async def definir_mcp(a: dict) -> dict:
            if not SLUG.fullmatch(a["name"]):
                return err("nome inválido: minúsculas, dígitos e _, a começar por letra")
            cfg = {k: a[k] for k in ("type", "description", "command", "args", "env", "url",
                                     "headers", "tools") if k in a}
            # aprovação é sempre humana: qualquer alteração volta a exigir revisão
            cfg |= {"approved": False, "read_only": False}
            d.spec.setdefault("mcp_servers", {})[a["name"]] = cfg
            return d._summary(f"MCP {a['name']} declarado (por aprovar no painel do flow)")

        @tool("validar", "Valida o rascunho com as regras do runtime e devolve os problemas.",
              _schema({}))
        async def validar(_: dict) -> dict:
            v = d.validation()
            return ok({"valido": v["ok"], "problemas": [e["msg"] for e in v["errors"]]})

        return [ver_flow, ver_catalogo, ver_prompt, alterar_flow, definir_agente, definir_gate,
                ligar, desligar, renomear_no, remover_no, definir_prompt_orquestrador,
                definir_mcp, validar]


READ_ONLY_TOOLS = {"ver_flow", "ver_catalogo", "ver_prompt", "validar"}


def build_options(draft: Draft, session_id: str | None) -> ClaudeAgentOptions:
    tools = draft.tools()
    allowed = [f"mcp__{SERVER}__{t.name}" for t in tools]
    return ClaudeAgentOptions(
        system_prompt=PROMPT,
        tools=[],  # nenhuma ferramenta nativa (nem delegação)
        mcp_servers={SERVER: create_sdk_mcp_server(name=SERVER, version="0.1.0", tools=tools)},
        allowed_tools=allowed,
        disallowed_tools=[*DISALLOWED, *DELEGATION_TOOLS],
        permission_mode="dontAsk",
        setting_sources=[],
        hooks=make_hooks(make_guard(allowed)),
        max_turns=MAX_TURNS,
        model=settings.model,
        resume=session_id,
    )


async def chat(message: str, flow: str, spec: dict[str, Any], prompts: dict[str, str],
               session_id: str | None = None) -> AsyncIterator[dict[str, Any]]:
    """Eventos: text, tool, done (com o rascunho e a validação) ou error."""
    draft = Draft(spec, prompts)
    prompt = f"[Flow aberto no editor: {flow}]\n\n{message}"
    try:
        async for msg in query(prompt=prompt, options=build_options(draft, session_id)):
            if isinstance(msg, AssistantMessage):
                for block in msg.content:
                    if isinstance(block, TextBlock) and block.text.strip():
                        yield {"type": "text", "text": block.text}
                    elif isinstance(block, ToolUseBlock):
                        name = block.name.removeprefix(f"mcp__{SERVER}__")
                        arg = block.input.get("name") or block.input.get("node") or (
                            f"{block.input.get('from')} → {block.input.get('to')}"
                            if "from" in block.input else "")
                        yield {"type": "tool", "name": name, "arg": str(arg)[:60],
                               "edit": name not in READ_ONLY_TOOLS}
            elif isinstance(msg, ResultMessage):
                yield {"type": "done", "session_id": msg.session_id, "cost": msg.total_cost_usd,
                       "turns": msg.num_turns, "is_error": msg.is_error,
                       "changed": draft.changed, "doc": draft.doc() if draft.changed else None,
                       "validation": draft.validation()}
    except Exception as e:  # noqa: BLE001 — o editor mostra o erro; não deixar o stream pendurado
        yield {"type": "error", "message": f"{type(e).__name__}: {e}"}


def encode(event: dict[str, Any]) -> bytes:
    return (json.dumps(event, ensure_ascii=False) + "\n").encode("utf-8")

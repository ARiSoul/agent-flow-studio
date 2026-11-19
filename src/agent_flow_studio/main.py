"""agent-flow-studio: executa um flow pela linha de comandos (sem aprovações interativas)."""
import argparse
import asyncio

from claude_agent_sdk import AssistantMessage, ResultMessage, TextBlock, ToolUseBlock

from .builder import FlowError
from .extensions import LOADED
from .hooks import DELEGATION_TOOLS
from .orchestrator import run_flow


async def _run(flow: str, run_input: str, resume: str | None) -> None:
    async for msg in run_flow(flow, run_input, resume):
        if isinstance(msg, AssistantMessage):
            for block in msg.content:
                if isinstance(block, TextBlock):
                    print(block.text)
                elif isinstance(block, ToolUseBlock):
                    sub = block.input.get("subagent_type") if block.name in DELEGATION_TOOLS else None
                    print(f"  · {block.name}" + (f" → {sub}" if sub else ""))
        elif isinstance(msg, ResultMessage):
            cost = f"{msg.total_cost_usd:.4f}" if msg.total_cost_usd is not None else "?"
            print(f"\n[{msg.subtype}] session_id={msg.session_id} turnos={msg.num_turns} "
                  f"custo≈{cost} USD")
            if msg.is_error:
                print(f"ERRO: {msg.errors or msg.result or msg.terminal_reason}")
            if msg.permission_denials:
                print(f"Recusas de permissão: {len(msg.permission_denials)}")
    report = LOADED.reports.get(flow)  # relatório opcional fornecido por uma extensão
    if report is not None:
        try:
            print(f"Relatório: {report(run_input).resolve()}")
        except (FileNotFoundError, ValueError) as e:
            print(f"Relatório não gerado: {e}")


def main() -> None:
    p = argparse.ArgumentParser(prog="agent-flow-studio")
    p.add_argument("input", help="entrada do flow (é também o id da execução em runs/)")
    p.add_argument("--flow", required=True, help="flow em flows/<nome>/flow.yaml")
    p.add_argument("--resume", help="session_id para retomar uma execução")
    args = p.parse_args()
    try:
        asyncio.run(_run(args.flow, args.input, args.resume))
    except FlowError as e:
        raise SystemExit(f"Flow inválido: {e}") from e


if __name__ == "__main__":
    main()

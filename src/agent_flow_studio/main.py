"""agent-flow-studio: runs a flow from the command line (no interactive approvals)."""
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
            print(f"\n[{msg.subtype}] session_id={msg.session_id} turns={msg.num_turns} "
                  f"cost≈{cost} USD")
            if msg.is_error:
                print(f"ERROR: {msg.errors or msg.result or msg.terminal_reason}")
            if msg.permission_denials:
                print(f"Permission denials: {len(msg.permission_denials)}")
    report = LOADED.reports.get(flow)  # optional report provided by an extension
    if report is not None:
        try:
            print(f"Report: {report(run_input).resolve()}")
        except (FileNotFoundError, ValueError) as e:
            print(f"Report not generated: {e}")


def main() -> None:
    p = argparse.ArgumentParser(prog="agent-flow-studio")
    p.add_argument("input", help="flow input (also the run id in runs/)")
    p.add_argument("--flow", required=True, help="flow in flows/<name>/flow.yaml")
    p.add_argument("--resume", help="session_id to resume a run")
    args = p.parse_args()
    try:
        asyncio.run(_run(args.flow, args.input, args.resume))
    except FlowError as e:
        raise SystemExit(f"Invalid flow: {e}") from e


if __name__ == "__main__":
    main()

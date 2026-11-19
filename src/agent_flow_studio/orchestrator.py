"""Executa um flow (flows/<nome>/flow.yaml) compilado para o Claude Agent SDK."""
from collections.abc import AsyncIterator

from claude_agent_sdk import ClaudeAgentOptions, Message, query

from .builder import load_flow


def build_options(flow: str, resume: str | None = None) -> ClaudeAgentOptions:
    return load_flow(flow, resume=resume).options


async def run_flow(flow: str, run_input: str, resume: str | None = None) -> AsyncIterator[Message]:
    compiled = load_flow(flow, resume=resume)
    async for message in query(prompt=compiled.task(run_input), options=compiled.options):
        yield message

"""Model prices, only for a live cost ESTIMATE while a step runs (the run panel marks it "≈").

The real cost of a step always comes from the SDK's session result (`total_cost_usd`), which
replaces the estimate when the step ends. Prices are US dollars per million tokens (Anthropic API
list prices); cache reads cost `cache_read` (default 0.1 × input) and cache writes 1.25 × input.
A model that is not in the table gives no estimate (never a guess).
"""
from __future__ import annotations

import re
from typing import Any

# model id: (input, output, cache read or None for 0.1 × input)
PRICES: dict[str, tuple[float, float, float | None]] = {
    "claude-fable-5-2": (8.0, 40.0, 0.20),
    "claude-mythos-5-2": (8.0, 40.0, 0.20),
    "claude-fable-5-1": (10.0, 50.0, None),
    "claude-mythos-5-1": (10.0, 50.0, None),
    "claude-fable-5": (10.0, 50.0, None),
    "claude-opus-5-5": (4.0, 20.0, 0.20),
    "claude-opus-5": (5.0, 25.0, None),
    "claude-opus-4-8": (5.0, 25.0, None),
    "claude-opus-4-7": (5.0, 25.0, None),
    "claude-opus-4-6": (5.0, 25.0, None),
    "claude-sonnet-5-5": (2.0, 10.0, 0.10),
    "claude-sonnet-5": (2.0, 10.0, None),
    "claude-sonnet-4-6": (3.0, 15.0, None),
    "claude-haiku-5-5": (0.10, 0.50, None),
    "claude-haiku-4-5": (1.0, 5.0, None),
}

_ID = re.compile(r"(claude-[a-z]+-\d+(?:-\d)?)(?:-\d{8})?(?:\[[^\]]*\])?")


def price(model: str | None) -> tuple[float, float, float, float] | None:
    """(input, output, cache read, cache write) per million tokens, or None when unknown. An
    unknown model (even a newer one of a known family) gives None."""
    if not model:
        return None
    found = _ID.fullmatch(model)  # an id, maybe with a date and a [context] suffix
    if not found or found.group(1) not in PRICES:
        return None
    i, o, cr = PRICES[found.group(1)]
    return i, o, cr if cr is not None else i * 0.1, i * 1.25


def estimate(model: str | None, tokens: dict[str, Any]) -> float | None:
    """The estimated cost in dollars of one API message's usage (`engine.usage_tokens` keys)."""
    p = price(model)
    if p is None:
        return None
    i, o, cr, cw = p
    cost = (tokens.get("in", 0) * i + tokens.get("out", 0) * o
            + tokens.get("cache_read", 0) * cr + tokens.get("cache_write", 0) * cw) / 1e6
    return round(cost, 6)

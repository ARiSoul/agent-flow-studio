"""End-to-end tests: the real editor (the built React app) served by the real API, driven in a
headless browser with Playwright. The model is a fake SDK (no session, no cost, no secrets) and
every folder is temporary (the safety nets of tests/conftest.py apply here too).

They need Playwright (`pip install -e ".[web]"`), a Chromium browser (an installed Chrome or Edge,
or `python -m playwright install chromium`) and the built editor (`cd editor && npm run build`);
without them they are skipped — or fail, with `AFS_E2E_REQUIRED=1` (CI). Run only these:
`pytest tests/e2e`.
"""
from __future__ import annotations

import asyncio
import os
import re
import shutil
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from claude_agent_sdk import AssistantMessage, ResultMessage, TextBlock

from agent_flow_studio.editor.api import common as api_common
from agent_flow_studio.tools import io as io_tool

FIX = Path(__file__).parents[1] / "fixtures" / "flows"
AGENT = re.compile(r"agent `([a-z][a-z0-9_]*)`")
RUN_ID = re.compile(r"[Rr]un[ _]id(?: is)?:? `([^`]+)`")
# what each agent of the `parallel` test flow saves (as `set_output` would)
OUTPUTS: dict[str, dict[str, Any]] = {
    "planner": {"question_a": "What is A?", "question_b": "What is B?", "size": 2},
    "researcher_a": {"answer": "**A** is the first answer.", "confidence": 0.9},
    "researcher_b": {"answer": "**B** is the second answer.", "confidence": 0.8},
    "writer": {"report": "# Report\n\n| part | answer |\n|---|---|\n| A | first |\n| B | second |"},
}

pytestmark = pytest.mark.e2e


def missing(reason: str) -> None:
    """Skips the test, or fails it where the E2E tests must run (AFS_E2E_REQUIRED=1, in CI)."""
    if os.getenv("AFS_E2E_REQUIRED") == "1":
        pytest.fail(f"E2E required but not possible: {reason}")
    pytest.skip(reason)


class FakeModel:
    """The SDK's `query`: one session per call, the agent and run id found in the prompt. An
    agent listed in `slow` works that many seconds first (a pause or stop can cut it there)."""

    def __init__(self) -> None:
        self.slow: dict[str, float] = {}
        self.calls: list[dict[str, Any]] = []

    async def __call__(self, prompt: str, options: Any):
        found = AGENT.search(prompt)
        agent = found.group(1) if found else "planner"
        rid = RUN_ID.search(prompt).group(1)
        self.calls.append({"agent": agent, "resume": options.resume})
        sid = options.resume or f"s-{agent}-{len(self.calls)}"
        yield AssistantMessage(content=[TextBlock(f"{agent} is working")], model="m",
                               session_id=sid)
        if self.slow.get(agent) and not options.resume:
            await asyncio.sleep(self.slow[agent])
        for k, v in OUTPUTS.get(agent, {}).items():
            io_tool.store_output(io_tool.spec_for(rid), rid, agent, k, v)
        yield ResultMessage(subtype="success", duration_ms=5, duration_api_ms=5, is_error=False,
                            num_turns=1, session_id=sid, total_cost_usd=0.01)


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item: pytest.Item, call: Any) -> Iterator[None]:
    """Keeps each phase's result on the test, so the page fixture knows when the test failed."""
    outcome = yield
    setattr(item, f"rep_{call.when}", outcome.get_result())


@pytest.fixture
def model() -> FakeModel:
    return FakeModel()


@pytest.fixture
def studio(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, model: FakeModel) -> Iterator[str]:
    """The editor on a free local port, with the `parallel` test flow; yields its URL."""
    try:
        import uvicorn
    except ImportError:
        missing("uvicorn is not installed")
    if not (api_common.STATIC / "index.html").is_file():
        missing("the editor is not built (cd editor && npm run build)")
    from agent_flow_studio.editor import server as server_mod
    from agent_flow_studio.editor.runner import Runner

    flows = tmp_path / "flows"
    shutil.copytree(FIX / "parallel", flows / "parallel")
    monkeypatch.setenv("PROVIDERS_FILE", str(tmp_path / "providers.yaml"))
    monkeypatch.setattr(server_mod, "Runner", lambda base, **k: Runner(base, query_fn=model))
    app = server_mod.create_app(flows, allowed_hosts=["127.0.0.1", "localhost"])
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=0, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    for _ in range(200):
        if server.started:
            break
        time.sleep(0.05)
    else:
        pytest.fail("the editor did not start")
    port = server.servers[0].sockets[0].getsockname()[1]
    yield f"http://127.0.0.1:{port}"
    server.should_exit = True
    thread.join(timeout=10)


@pytest.fixture(scope="module")  # not session: Playwright keeps an event loop open while it runs
def browser() -> Iterator[Any]:
    try:
        from playwright import sync_api
    except ImportError:
        missing("Playwright is not installed (pip install -e \".[web]\")")
    with sync_api.sync_playwright() as pw:
        tried = []
        for channel in (None, "chrome", "msedge"):  # Playwright's Chromium, then the installed
            try:
                b = pw.chromium.launch(headless=True, **({"channel": channel} if channel else {}))
                break
            except Exception as e:  # noqa: BLE001 — try the next one
                tried.append(f"{channel or 'chromium'}: {str(e).splitlines()[0][:80]}")
        else:
            missing("no Chromium browser for Playwright: " + "; ".join(tried))
        yield b
        b.close()


@pytest.fixture
def page(browser: Any, studio: str, request: pytest.FixtureRequest) -> Iterator[Any]:
    """A fresh page on the editor, in English and the light theme. When the test fails, a
    screenshot goes to `AFS_E2E_SHOTS` (CI keeps that folder as an artifact)."""
    ctx = browser.new_context(viewport={"width": 1400, "height": 900})
    ctx.add_init_script("localStorage.setItem('afs.lang', 'en');"
                        "localStorage.setItem('afs.theme', 'light');")
    ctx.set_default_timeout(10_000)
    p = ctx.new_page()
    errors: list[str] = []
    p.on("pageerror", lambda e: errors.append(str(e)))
    p.goto(studio + "/#flow=parallel")
    p.errors = errors  # checked by the tests: no script error on the page
    yield p
    shots = os.getenv("AFS_E2E_SHOTS")
    rep = getattr(request.node, "rep_call", None)
    if shots and rep is not None and rep.failed:
        Path(shots).mkdir(parents=True, exist_ok=True)
        p.screenshot(path=str(Path(shots) / f"{request.node.name}.png"), full_page=True)
    ctx.close()

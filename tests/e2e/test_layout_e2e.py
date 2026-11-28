"""The editor's layout in a real browser: the top bar at a small width, every theme, and a big
flow (how long it takes to open and to edit)."""
from __future__ import annotations

import time
from pathlib import Path

import pytest
import yaml

try:
    from playwright.sync_api import expect
except ImportError:  # the fixtures skip (or fail, in CI) with the reason
    expect = None

pytestmark = pytest.mark.e2e


def bar_height(page) -> float:
    return page.locator("header.topbar").evaluate("e => e.getBoundingClientRect().height")


def test_a_small_window_keeps_one_bar_row_and_labelled_menus(page):
    page.set_viewport_size({"width": 1180, "height": 760})
    assert bar_height(page) <= 50  # one row
    expect(page.get_by_role("button", name="Connectors")).to_be_hidden()  # folded into one menu
    page.get_by_role("button", name="Connections").click()
    page.get_by_role("menuitem", name="Providers…").click()
    expect(page.get_by_role("dialog", name="Model providers")).to_be_visible()
    page.keyboard.press("Escape")
    page.get_by_role("button", name="Flow actions").click()
    expect(page.get_by_role("menuitem", name="Location: Local — Move…")).to_be_visible()
    page.keyboard.press("Escape")
    assert page.errors == []


@pytest.mark.parametrize("theme", ["light", "dark", "sepia", "ocean", "contrast"])
def test_every_theme_draws_the_editor(page, theme):
    page.get_by_role("button", name="View", exact=True).click()
    page.get_by_role("combobox", name="Theme").select_option(theme)
    page.keyboard.press("Escape")
    assert page.evaluate("document.documentElement.dataset.theme") == theme
    assert bar_height(page) <= 50
    # every color comes from the theme's tokens: none of them is empty
    empty = page.evaluate("""() => ['--bg', '--panel', '--text', '--accent', '--ok', '--bad', '--human', '--border']
      .filter(k => !getComputedStyle(document.documentElement).getPropertyValue(k).trim())""")
    assert empty == []
    expect(page.locator(".react-flow__node[data-id='planner']")).to_be_visible()
    assert page.errors == []


def big_flow(n: int) -> dict:
    """n agents in a chain, each with two typed inputs and outputs (a busy canvas)."""
    agents = {}
    for i in range(n):
        a = {"description": f"Step {i}.", "prompt": f"Do step {i}.",
             "outputs": {"result": {"type": "markdown"}, "score": {"type": "number"}}}
        if i:
            a["inputs"] = {"result": {"from": f"a{i - 1}.result"}, "score": {"from": f"a{i - 1}.score"}}
        agents[f"a{i}"] = a
    return {"version": 1, "name": "big", "title": "Big (tests)", "execution": "graph", "start": "a0",
            "agents": agents, "edges": [{"from": f"a{i}", "to": f"a{i + 1}"} for i in range(n - 1)]}


def test_a_big_flow_opens_and_edits_quickly(page, tmp_path: Path):
    n = 40
    (tmp_path / "flows" / "big").mkdir()
    (tmp_path / "flows" / "big" / "flow.yaml").write_text(yaml.safe_dump(big_flow(n)), "utf-8")
    page.reload()
    t0 = time.perf_counter()
    page.get_by_role("combobox", name="Flow").select_option("big")
    page.locator(".react-flow__node[data-id='a0']").wait_for()
    opened = time.perf_counter() - t0
    page.locator(".react-flow__node[data-id='a0']").click()
    t1 = time.perf_counter()
    page.keyboard.press("Control+d")  # an edit: every node's data is rebuilt
    page.locator(".react-flow__node[data-id='a0_2']").wait_for()
    edited = time.perf_counter() - t1
    print(f"big flow ({n} agents): opened in {opened * 1000:.0f} ms, an edit drawn in {edited * 1000:.0f} ms")
    assert opened < 5 and edited < 1.5  # generous bounds for CI machines
    assert page.errors == []


def test_the_first_view_in_a_small_window_shows_the_whole_width(page):
    page.set_viewport_size({"width": 1180, "height": 760})
    page.reload()  # the first view is computed for this window
    page.locator(".react-flow__node[data-id='planner']").wait_for()
    page.wait_for_timeout(300)
    outside = page.evaluate("""() => {
      const c = document.querySelector('.react-flow').getBoundingClientRect();
      return [...document.querySelectorAll('.react-flow__node')].filter(n => {
        const r = n.getBoundingClientRect(); return r.left < c.left - 1 || r.right > c.right + 1;
      }).map(n => n.dataset.id);
    }""")
    assert outside == []
    assert page.errors == []

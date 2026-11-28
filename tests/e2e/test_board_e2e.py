"""The workspace Board in a real browser (the `game` workspace of conftest.py): the cards, their
Open and Run buttons, the keyboard, the card menu, the layout and a flow that does not load."""
from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

try:
    from playwright.sync_api import expect
except ImportError:  # the fixtures skip (or fail, in CI) with the reason
    expect = None

pytestmark = pytest.mark.e2e


def board(page):
    page.get_by_role("button", name="Board", exact=True).click()
    page.locator(".react-flow__node[data-id='implement']").wait_for()


def card(page, title: str):
    return page.get_by_role("group", name=re.compile(rf"^{re.escape(title)}, last run"))


def test_every_flow_has_a_card_with_its_last_run_and_the_links(page, game):
    board(page)
    for title in ("Plan a feature", "Implement the feature", "Code review", "Release notes"):
        expect(card(page, title)).to_be_visible()
        expect(card(page, title)).to_contain_text("Never run")
    # the subflow link goes from the caller to the flow it runs, with words
    expect(page.locator(".react-flow__edge.board-sub")).to_have_count(1)
    expect(page.get_by_role("note", name="Links between flows")).to_contain_text("calls another flow as a subflow")
    assert page.errors == []


def test_enter_on_a_card_opens_the_flow(page, game):
    board(page)
    page.locator(".react-flow__node[data-id='code-review']").focus()
    page.keyboard.press("Enter")
    expect(page.get_by_role("combobox", name="Flow")).to_have_value("code-review")
    expect(page.get_by_role("button", name="Flow editor")).to_have_attribute("aria-pressed", "true")
    assert page.errors == []


def test_the_run_button_opens_the_run_panel_and_the_card_shows_the_run(page, game, model):
    board(page)
    page.get_by_role("button", name="Run Plan a feature").click()
    expect(page.get_by_role("combobox", name="Flow")).to_have_value("plan-feature")
    page.get_by_role("textbox", name="Idea *").fill("a ladybug")
    page.get_by_role("button", name="▶ Run", exact=True).click()
    expect(page.locator(".tb-run")).not_to_have_class(re.compile(r"\brunning\b"), timeout=15000)
    board(page)
    expect(card(page, "Plan a feature")).to_contain_text("✓ Done")
    expect(card(page, "Plan a feature")).not_to_contain_text("Never run")
    assert page.errors == []


def test_the_card_menu_from_the_keyboard_duplicates_a_flow(page, game):
    board(page)
    page.locator(".react-flow__node[data-id='code-review']").focus()
    page.keyboard.press("Shift+F10")
    menu = page.get_by_role("menu")
    expect(menu.get_by_role("menuitem", name="Delete flow…")).to_be_visible()
    menu.get_by_role("menuitem", name="Duplicate in this workspace…").click()
    form = page.get_by_role("dialog")
    expect(form.get_by_role("textbox", name=re.compile(r"^Name"))).to_have_value("code-review-copy")
    form.get_by_role("button", name="Create").click()
    expect(page.locator(".react-flow__node[data-id='code-review-copy']")).to_be_visible()
    assert page.errors == []


def test_no_card_covers_another(page, game, tmp_path: Path):
    # a tall card (many outputs) next to the others
    flows = tmp_path / "workspaces" / "game" / "flows"
    (flows / "tall").mkdir()
    outs = {f"o{i}": {"type": "markdown"} for i in range(9)}
    (flows / "tall" / "flow.yaml").write_text(yaml.safe_dump({
        "version": 1, "name": "tall", "title": "Tall", "execution": "graph", "description": "Many outputs. " * 8,
        "start": "a", "agents": {"a": {"description": "x", "prompt": "x", "outputs": outs}}, "edges": []}), "utf-8")
    board(page)
    page.wait_for_timeout(300)
    boxes = page.evaluate("""() => [...document.querySelectorAll('.react-flow__node-flowcard')].map(n => {
      const r = n.getBoundingClientRect(); return [n.dataset.id, r.left, r.top, r.right, r.bottom]; })""")
    assert len(boxes) == 5
    for i, a in enumerate(boxes):
        for b in boxes[i + 1:]:
            apart = a[3] <= b[1] or b[3] <= a[1] or a[4] <= b[2] or b[4] <= a[2]
            assert apart, f"{a[0]} covers {b[0]}"
    assert page.errors == []


def test_a_flow_that_does_not_load_says_why(page, game, tmp_path: Path):
    broken = tmp_path / "workspaces" / "game" / "flows" / "broken"
    broken.mkdir()
    (broken / "flow.yaml").write_text(yaml.safe_dump({
        "version": 1, "name": "broken", "title": "Broken", "execution": "graph", "start": "a",
        "agents": {"a": {"description": "x", "prompt": "x"}}, "edges": [{"from": "a", "to": "ghost"}]}), "utf-8")
    board(page)
    c = card(page, "Broken")
    expect(c).to_contain_text("Doesn't load:")
    expect(c).to_contain_text("ghost")
    expect(c.get_by_role("button", name="Run Broken")).to_have_count(0)
    # it is listed under "Needs attention"; Fix in editor opens it with its problems
    attention = page.get_by_role("list", name="Needs attention")
    expect(attention).to_contain_text("Broken")
    c.get_by_role("button", name="Fix Broken in the editor").click()
    expect(page.get_by_role("combobox", name="Flow")).to_have_value("broken")
    expect(page.locator(".tb-status")).to_have_attribute("data-status", "bad")
    assert page.errors == []


@pytest.mark.parametrize("lang", ["en", "pt", "es"])
def test_the_bar_stays_one_row_in_a_production_workspace(page, game, lang):
    """The widest bar: a workspace, prod, a flow with unsaved changes, in every language."""
    page.context.add_init_script(f"localStorage.setItem('afs.lang', '{lang}');")
    page.reload()
    page.locator("#flow-select").select_option("implement")
    page.locator(".react-flow__node[data-id='coder']").wait_for()
    page.locator(".ws-bar select.env").select_option("prod")
    page.locator(".react-flow__node[data-id='coder']").click()
    page.keyboard.press("Control+d")
    for width in (1400, 1280, 1180):
        page.set_viewport_size({"width": width, "height": 800})
        page.wait_for_timeout(150)
        bar = page.locator("header.topbar")
        assert bar.evaluate("e => e.getBoundingClientRect().height") <= 50, (lang, width)
        assert bar.evaluate("e => e.scrollWidth - e.clientWidth") <= 0, (lang, width)
        tall = page.evaluate("""() => [...document.querySelectorAll('header.topbar button')]
          .filter(b => b.offsetParent && b.getBoundingClientRect().height > 36).map(b => b.textContent)""")
        assert tall == [], (lang, width, tall)
    assert page.errors == []


def test_a_run_at_a_gate_needs_you_and_answer_opens_that_run(page, game, model, tmp_path: Path):
    flows = tmp_path / "workspaces" / "game" / "flows"
    (flows / "ship").mkdir()
    (flows / "ship" / "flow.yaml").write_text(yaml.safe_dump({
        "version": 1, "name": "ship", "title": "Ship it", "execution": "graph",
        "inputs": {"what": {"type": "text", "label": "What"}}, "start": "coder",
        "agents": {"coder": {"description": "x", "prompt": "x", "inputs": {"what": {"from": "input.what"}},
                             "outputs": {"summary": {"type": "text"}}}},
        "gates": {"ok": {"kind": "human", "label": "Ship this?"}},
        "edges": [{"from": "coder", "to": "ok"}]}), "utf-8")
    board(page)
    page.get_by_role("button", name="Run Ship it").click()
    page.get_by_role("textbox", name="What *").fill("v1")
    page.get_by_role("button", name="▶ Run", exact=True).click()
    expect(page.get_by_role("group", name="Human approval: Ship this?")).to_be_visible(timeout=15000)
    board(page)
    c = card(page, "Ship it")
    expect(c).to_have_attribute("aria-label", "Ship it, last run: Needs you")
    c.get_by_role("button", name="Ship it needs you: open its run").click()
    expect(page.get_by_role("group", name="Human approval: Ship this?")).to_be_visible()
    assert page.errors == []


def test_an_unknown_variable_makes_the_card_say_so(page, game, tmp_path: Path):
    flows = tmp_path / "workspaces" / "game" / "flows"
    (flows / "vars").mkdir()
    (flows / "vars" / "flow.yaml").write_text(yaml.safe_dump({
        "version": 1, "name": "vars", "title": "Uses a variable", "execution": "graph",
        "description": "Deploys to {vars.nowhere}.", "start": "a",
        "agents": {"a": {"description": "x", "prompt": "x"}}, "edges": []}), "utf-8")
    board(page)
    expect(card(page, "Uses a variable")).to_contain_text("vars.nowhere")
    assert page.errors == []


def test_the_legend_names_only_the_links_on_the_board(page, game):
    board(page)
    legend = page.get_by_role("note", name="Links between flows")
    expect(legend).to_contain_text("usual source of an input")
    expect(legend).not_to_contain_text("value passed in a run")  # no run passed a value yet
    assert page.errors == []


def test_a_failed_run_says_so_and_view_run_opens_it(page, game, model, tmp_path: Path):
    cr = tmp_path / "workspaces" / "game" / "flows" / "code-review" / "flow.yaml"
    spec = yaml.safe_load(cr.read_text("utf-8"))
    spec["retry"] = {"transient": 0}  # the first temporary error ends the run
    cr.write_text(yaml.safe_dump(spec), "utf-8")
    model.fail["reviewer"] = 1
    board(page)
    page.get_by_role("button", name="Run Code review").click()
    page.get_by_role("textbox", name="Changes *").fill("a diff")
    page.get_by_role("button", name="▶ Run", exact=True).click()
    expect(page.locator(".tb-run")).not_to_have_class(re.compile(r"\brunning\b"), timeout=15000)
    board(page)
    c = card(page, "Code review")
    expect(c).to_contain_text("Failed")
    expect(page.get_by_role("list", name="Needs attention")).to_contain_text("Code review")
    c.get_by_role("button", name="View the failed run of Code review").click()
    expect(page.get_by_role("combobox", name="Flow")).to_have_value("code-review")
    expect(page.locator("aside.right")).to_contain_text("reviewer")
    assert page.errors == []


def test_the_dots_button_opens_the_card_menu(page, game):
    board(page)
    page.get_by_role("button", name="More actions for Release notes").click()
    expect(page.get_by_role("menu").get_by_role("menuitem", name="Copy to workspace…")).to_be_visible()
    assert page.errors == []


def test_in_prod_the_run_buttons_say_so(page, game):
    page.locator(".ws-bar select.env").select_option("prod")
    board(page)
    expect(card(page, "Code review").get_by_role("button", name="Run Code review in prod")).to_be_visible()
    page.get_by_role("button", name="Flow editor").click()
    expect(page.locator(".tb-run")).to_contain_text("Run in prod")
    assert page.errors == []


def test_a_card_moved_by_hand_never_covers_another(page, game):
    # code-review moved by hand onto the place of the chain's first card (plan-feature)
    page.evaluate("localStorage.setItem('afs.board.game', JSON.stringify({'code-review': {x: 0, y: 0}}))")
    board(page)
    page.wait_for_timeout(300)
    boxes = page.evaluate("""() => [...document.querySelectorAll('.react-flow__node-flowcard')].map(n => {
      const r = n.getBoundingClientRect(); return [n.dataset.id, r.left, r.top, r.right, r.bottom]; })""")
    for i, a in enumerate(boxes):
        for b in boxes[i + 1:]:
            assert a[3] <= b[1] or b[3] <= a[1] or a[4] <= b[2] or b[4] <= a[2], f"{a[0]} covers {b[0]}"
    assert page.errors == []


def test_the_cards_of_a_chain_line_up_and_tags_are_whole(page, game):
    board(page)
    page.wait_for_timeout(300)
    tops = page.evaluate("""() => ['plan-feature', 'implement', 'code-review'].map(id =>
      document.querySelector(`.react-flow__node[data-id="${id}"] .board-card`).getBoundingClientRect().top)""")
    assert max(tops) - min(tops) <= 1, tops
    cut = page.evaluate("""() => [...document.querySelectorAll('.board-card .port-tag')]
      .filter(t => t.scrollWidth > t.clientWidth + 1).map(t => t.textContent)""")
    assert cut == []
    assert page.errors == []

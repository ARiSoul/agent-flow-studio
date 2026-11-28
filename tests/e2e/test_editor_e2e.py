"""The main paths of the editor in a real browser (see conftest.py: fake model, temporary folders)."""
from __future__ import annotations

import re

import pytest

try:
    from playwright.sync_api import expect
except ImportError:  # the fixtures skip (or fail, in CI) with the reason
    expect = None

pytestmark = pytest.mark.e2e


def start_run(page, topic: str = "agents") -> None:
    page.get_by_role("button", name="Run", exact=True).click()
    page.get_by_role("textbox", name="Topic *").fill(topic)
    page.get_by_role("button", name="▶ Run").click()


def test_the_flow_opens_valid_with_its_nodes(page):
    expect(page.get_by_role("combobox", name="Flow")).to_have_value("parallel")
    expect(page.get_by_text("✓ valid")).to_be_visible()
    canvas = page.get_by_role("application")
    for node in ("planner", "researcher_a", "researcher_b", "writer", "Check the report"):
        expect(canvas.get_by_text(node, exact=False).first).to_be_visible()
    assert page.errors == []


def test_a_run_reaches_its_gate_shows_the_report_and_ends_when_approved(page, model):
    start_run(page)
    gate = page.get_by_role("group", name="Human approval: Check the report")
    expect(gate).to_be_visible()
    # the draft report, Markdown table included, is on the gate card
    expect(gate.get_by_role("heading", name="Report")).to_be_visible()
    expect(gate.get_by_role("cell", name="first")).to_be_visible()
    steps = page.get_by_role("list", name="Steps")
    for agent in ("planner", "researcher_a", "researcher_b", "writer"):
        expect(steps.get_by_role("button", name=f"{agent} done")).to_be_visible()
    expect(steps.get_by_role("button", name="too_small stop skipped")).to_be_visible()
    gate.get_by_role("textbox", name="Comment or answer").fill("looks good")
    gate.get_by_role("button", name="Approve").click()
    expect(gate).to_be_hidden()
    expect(page.get_by_role("button", name="approve approval")).to_contain_text("done")
    assert sorted(c["agent"] for c in model.calls) == ["planner", "researcher_a", "researcher_b",
                                                        "writer"]
    # the run is in the history and opens again
    page.get_by_role("tab", name="History 1").click()
    expect(page.get_by_role("tabpanel")).to_contain_text("done")
    assert page.errors == []


def test_pause_a_running_step_and_go_on_in_its_session(page, model):
    model.slow["planner"] = 60  # it works until the pause
    start_run(page)
    pause = page.get_by_role("button", name="⏸ Pause")
    expect(pause).to_be_enabled()
    pause.click()
    card = page.get_by_role("group", name="Paused by you: planner")
    expect(card).to_be_visible()
    expect(page.get_by_role("list", name="Steps").get_by_role("button", name="planner paused")
           ).to_be_visible()
    card.get_by_role("button", name="Continue").click()
    expect(page.get_by_role("group", name="Human approval: Check the report")).to_be_visible()
    planner = [c for c in model.calls if c["agent"] == "planner"]
    assert planner[0]["resume"] is None and planner[1]["resume"] == "s-planner-1"
    assert page.errors == []


def test_stop_ends_a_run_and_new_run_is_ready_again(page, model):
    model.slow["planner"] = 60
    start_run(page)
    page.get_by_role("button", name="Stop", exact=True).click()
    expect(page.get_by_role("list", name="Steps").get_by_role("button", name="planner")
           ).to_contain_text("interrupted")
    page.get_by_role("tab", name="New run").click()
    expect(page.get_by_role("button", name="▶ Run")).to_be_enabled()
    assert page.errors == []


def test_a_provider_added_from_the_model_field_reaches_the_model_picker(page):
    page.get_by_role("button", name="Properties", exact=True).click()
    page.get_by_role("button", name="Providers…").first.click()
    window = page.get_by_role("dialog", name="Model providers")
    window.get_by_role("button", name="+ Gateway (other models)").click()
    window.get_by_role("textbox", name=re.compile(r"^Name ")).fill("e2e")  # label + hint
    window.get_by_role("textbox", name="Base URL").fill("http://localhost:4999")
    window.get_by_role("textbox", name=re.compile(r"^Models ")).fill("gpt-5")
    window.get_by_role("button", name="Save").click()
    expect(window.get_by_role("status")).to_contain_text("Saved in providers.yaml")
    window.get_by_role("textbox", name="Or read it from an environment variable").fill("E2E_KEY")
    window.get_by_role("button", name="Use this variable").click()
    expect(window).to_contain_text("The key is not set")
    window.get_by_role("button", name="Close").click()
    expect(window).to_be_hidden()
    expect(page.locator("datalist#models option[value='e2e/gpt-5']")).to_have_count(1)
    model_field = page.get_by_role("combobox", name="Model")
    model_field.fill("e2e/gpt-5")
    expect(page.get_by_role("button", name="Runs on e2e (gateway)")).to_be_visible()
    expect(page.get_by_text("— its key is not set")).to_be_visible()
    assert page.errors == []


def test_the_interface_language_changes(page):
    language = page.locator("select:has(option[value='pt'])")  # its label changes with it
    language.select_option("pt")
    expect(page.get_by_role("button", name="Propriedades", exact=True)).to_be_visible()
    language.select_option("es")
    expect(page.get_by_role("button", name="Propiedades", exact=True)).to_be_visible()
    assert page.errors == []


def test_the_node_menu_duplicates_and_renames_and_undo_goes_back(page):
    canvas = page.get_by_role("application")
    canvas.locator(".react-flow__node[data-id='writer']").click(button="right")
    menu = page.get_by_role("menu")
    expect(menu.get_by_role("menuitem", name="Properties")).to_be_visible()
    menu.get_by_role("menuitem", name="Duplicate").click()
    copy = canvas.locator(".react-flow__node[data-id='writer_2']")
    expect(copy).to_be_visible()
    copy.click(button="right")
    page.get_by_role("menu").get_by_role("menuitem", name="Rename…").click()
    form = page.get_by_role("dialog", name="Rename writer_2")
    form.get_by_role("textbox").fill("editor")
    form.get_by_role("button", name="Rename").click()
    expect(canvas.locator(".react-flow__node[data-id='editor']")).to_be_visible()
    expect(page.get_by_role("button", name="Save", exact=True)).to_be_enabled()  # changed, not saved
    page.get_by_role("button", name="Undo").click()
    page.get_by_role("button", name="Undo").click()
    expect(canvas.locator(".react-flow__node[data-id='editor']")).to_have_count(0)
    expect(canvas.locator(".react-flow__node[data-id='writer_2']")).to_have_count(0)
    assert page.errors == []


def test_a_library_agent_is_copied_into_the_flow(page):
    page.locator(".pal.lib").filter(has_text="Summarizer").click()  # the palette entry
    canvas = page.get_by_role("application")
    expect(canvas.locator(".react-flow__node[data-id='summarizer']")).to_be_visible()
    expect(page.get_by_text("Summarizer copied from the library.")).to_be_visible()
    assert page.errors == []


def test_a_new_blank_flow_is_created_and_opened(page):
    page.get_by_role("button", name="New", exact=True).click()
    form = page.get_by_role("dialog", name="New flow")
    form.get_by_role("textbox", name=re.compile(r"^Name")).fill("e2e-flow")
    form.get_by_role("textbox", name=re.compile(r"^Title")).fill("E2E flow")
    form.get_by_role("button", name="Next").click()
    expect(page.get_by_text("Flow e2e-flow created.")).to_be_visible()
    expect(page.get_by_role("combobox", name="Flow")).to_have_value("e2e-flow")
    # a new flow runs on the graph engine (the recommended mode)
    page.get_by_role("button", name="Properties", exact=True).click()
    expect(page.get_by_role("combobox", name="Execution")).to_have_value("graph")
    assert page.errors == []


def test_a_long_orchestrator_flow_is_converted_to_the_graph_engine(page):
    page.get_by_role("combobox", name="Flow").select_option("legacy")
    expect(page.get_by_role("combobox", name="Flow")).to_have_value("legacy")
    # the warning: longer than the orchestrator is for
    expect(page.get_by_role("contentinfo")).to_contain_text("the orchestrator is for short flows")
    page.get_by_role("button", name="Properties", exact=True).click()
    page.get_by_role("button", name="Convert to the graph engine…").click()
    dialog = page.get_by_role("dialog", name="Convert to the graph engine")
    expect(dialog).to_contain_text("Ready to convert. 4 agents need inputs or outputs afterwards.")
    agents = dialog.get_by_role("region", name="Agents to check")
    expect(agents).to_contain_text("a2")
    expect(agents).to_contain_text("Gets only the flow task.")
    expect(dialog.get_by_role("region", name="Conditions removed")).to_contain_text("was: finished")
    dialog.get_by_role("button", name="Convert draft").click()
    expect(dialog).to_be_hidden()
    expect(page.get_by_role("combobox", name="Execution")).to_have_value("graph")
    expect(page.get_by_role("button", name="Convert to the graph engine…")).to_have_count(0)
    expect(page.get_by_role("button", name="Save", exact=True)).to_be_enabled()  # not saved yet
    expect(page.get_by_role("contentinfo")).not_to_contain_text("the orchestrator is for short flows")
    page.get_by_role("button", name="Undo").click()
    expect(page.get_by_role("combobox", name="Execution")).to_have_value("orchestrator")
    assert page.errors == []


def test_a_temporary_api_error_retries_by_itself_or_now(page, model):
    model.fail["planner"] = 1  # its first session ends with "API Error: 529 overloaded"
    start_run(page)
    card = page.get_by_role("group", name="planner failed, retrying")
    expect(card).to_be_visible()
    expect(card).to_contain_text("The model API is overloaded (529).")
    expect(card).to_contain_text("you don't need to act")
    card.get_by_role("button", name="Retry now").click()  # no need to wait for the countdown
    expect(page.get_by_role("group", name="Human approval: Check the report")).to_be_visible()
    assert [c["agent"] for c in model.calls].count("planner") == 2
    assert page.errors == []

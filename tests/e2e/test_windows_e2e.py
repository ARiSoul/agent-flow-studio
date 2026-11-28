"""The Connectors, Agent library and Run history windows in a real browser: the shared window
shell (focus, Escape, unsaved changes), the connector status card, the library check and the
history rows in plain words."""
from __future__ import annotations

import re
import time
from pathlib import Path

import pytest
import yaml

try:
    from playwright.sync_api import expect
except ImportError:  # the fixtures skip (or fail, in CI) with the reason
    expect = None

pytestmark = pytest.mark.e2e
H = {"X-Flow-Editor": "1"}


def connectors(page):
    page.set_viewport_size({"width": 1500, "height": 900})  # Connectors has its own bar button
    page.get_by_role("button", name="Connectors").click()
    return page.get_by_role("dialog", name="Connectors")


def test_unsaved_connector_edits_are_never_lost_silently(page, studio):
    r = page.request.put(f"{studio}/api/connections/issues_api", headers=H, data={
        "type": "http_api", "base_url": "https://api.example.invalid", "auth": {"method": "bearer"}})
    assert r.ok, r.text()
    win = connectors(page)
    win.get_by_role("button", name=re.compile("^issues_api")).click()
    expect(win.get_by_role("button", name="Save")).to_be_disabled()  # nothing to save yet
    win.get_by_role("textbox", name="Description").fill("The team's tracker.")
    expect(win).to_contain_text("unsaved changes")
    page.keyboard.press("Escape")
    ask = page.get_by_role("dialog", name="Discard your changes?")
    expect(ask).to_be_visible()
    ask.get_by_role("button", name="Cancel").click()
    expect(win).to_be_visible()
    expect(win.get_by_role("textbox", name="Description")).to_have_value("The team's tracker.")
    win.get_by_role("button", name="Save").click()
    expect(win.get_by_role("button", name="Save")).to_have_attribute("aria-disabled", "true")  # saved
    page.keyboard.press("Escape")  # nothing unsaved: it closes at once
    expect(win).to_be_hidden()
    assert page.errors == []


def test_the_status_card_sets_a_secret_and_tests_the_connector(page, studio, monkeypatch):
    monkeypatch.setenv("E2E_TRACKER_TOKEN", "not-a-real-token")  # the server runs in this process
    assert page.request.put(f"{studio}/api/connections/issues_api", headers=H, data={
        "type": "http_api", "base_url": "https://127.0.0.1:9", "auth": {"method": "bearer"}}).ok
    win = connectors(page)
    item = win.get_by_role("button", name=re.compile("^issues_api"))
    expect(item).to_contain_text("no secret")
    item.click()
    status = win.get_by_role("group", name="Secret, sign-in and test")
    expect(status.get_by_role("button", name="▶ Test connection")).to_be_disabled()  # no secret yet
    status.get_by_role("textbox", name=re.compile("environment variable")).fill("E2E_TRACKER_TOKEN")
    status.get_by_role("button", name="Use").click()
    expect(win.locator(".modal-foot .msg")).to_contain_text("linked to the environment variable")
    expect(status).not_to_contain_text("missing")
    status.get_by_role("button", name="▶ Test connection").click()
    expect(status.locator(".conn-test-line")).to_contain_text(re.compile("It works|It failed"), timeout=15000)
    expect(win.get_by_role("status")).to_contain_text(re.compile("It works|It failed"))  # announced
    assert page.errors == []


def test_a_new_connector_starts_from_a_type_tile(page, studio):
    win = connectors(page)
    win.get_by_role("button", name="+ New connector…").click()
    win.get_by_role("button", name=re.compile("^GitHub")).click()
    win.get_by_role("textbox", name="Name").fill("repos")
    win.get_by_role("button", name="Save").click()
    expect(win.get_by_role("button", name=re.compile("^repos"))).to_be_visible()
    assert page.errors == []


def test_the_library_checks_the_definition_before_save(page):
    page.get_by_role("button", name="Manage").click()
    win = page.get_by_role("dialog", name="Agent library")
    win.get_by_role("button", name="+ New agent").click()
    win.get_by_role("textbox", name="Library id").fill("notes")
    win.get_by_role("textbox", name="Prompt", exact=True).fill("Write notes.")
    definition = win.get_by_role("textbox", name="Definition (YAML)")
    definition.fill("description: [unclosed")
    expect(win.get_by_role("alert")).to_contain_text("invalid YAML")
    expect(win.get_by_role("button", name="Save")).to_be_disabled()
    definition.fill("description: Takes notes.\n")
    expect(win.get_by_role("alert")).to_have_count(0)
    win.get_by_role("button", name="Save").click()
    expect(win.get_by_role("listitem").filter(has_text="notes · Local")).to_be_visible()
    assert page.errors == []


def _runs(page, studio, model, tmp_path: Path):
    """Two runs of the parallel flow: one failed with a busy model API (529), one at its gate."""
    fp = tmp_path / "flows" / "parallel" / "flow.yaml"
    spec = yaml.safe_load(fp.read_text("utf-8"))
    spec["retry"] = {"transient": 0}
    fp.write_text(yaml.safe_dump(spec), "utf-8")
    for i, fail in enumerate((9, 0)):
        model.fail["writer"] = fail
        assert page.request.post(studio + "/api/runs", headers=H,
                                 data={"flow": "parallel", "run_id": f"research-{i}", "inputs": {"topic": f"t{i}"}}).ok
        for _ in range(200):
            if not any(x.get("status") == "running" for x in page.request.get(studio + "/api/runs").json()):
                break
            time.sleep(0.05)
    page.reload()
    page.locator("aside.right .right-tabs button", has_text="Run").first.click()
    page.locator("aside.right").get_by_role("tab", name=re.compile("^History")).click()


def test_history_says_why_a_run_failed_in_plain_words(page, studio, model, tmp_path):
    _runs(page, studio, model, tmp_path)
    side = page.locator("aside.right")
    expect(side.get_by_role("heading", name="Today")).to_be_visible()  # the day is a real heading
    row = side.get_by_role("button", name=re.compile(r"^research-0, error, "))
    expect(row).to_be_visible()
    expect(side.get_by_text(re.compile("model API is overloaded"))).to_be_visible()
    expect(side.get_by_text('{"type"')).to_have_count(0)  # the raw API text stays in the tooltip
    # a next step, and why it did not retry by itself
    expect(side.get_by_role("button", name="Resume research-0")).to_be_visible()
    expect(side.get_by_text("Automatic retry is off for this flow.").first).to_be_visible()
    side.get_by_role("button", name="Resume research-0").click()
    expect(side.get_by_role("tab", name=re.compile("^Current"))).to_have_attribute("aria-selected", "true")
    assert page.errors == []


def test_compare_keeps_the_focus_and_closes_with_escape(page, studio, model, tmp_path):
    _runs(page, studio, model, tmp_path)
    page.locator("aside.right").get_by_role("button", name="Actions for research-1").click()
    page.get_by_role("menuitem", name="Compare with…").click()
    win = page.get_by_role("dialog", name="Compare")
    expect(win).to_be_visible()
    pick = win.get_by_role("combobox", name="Compare with")
    pick.click()
    pick.fill("research-0")
    expect(win.get_by_role("option")).to_have_count(1)
    page.keyboard.press("Enter")
    expect(pick).to_have_value(re.compile("^research-0 · "))
    expect(win.get_by_role("columnheader", name="research-0")).to_be_visible()
    for _ in range(8):  # Tab never leaves the window
        page.keyboard.press("Tab")
        assert page.evaluate("document.activeElement.closest('[role=dialog]') !== null")
    page.keyboard.press("Escape")
    expect(win).to_be_hidden()
    assert page.errors == []


def test_a_built_in_agent_says_why_it_cannot_be_deleted(page):
    page.get_by_role("button", name="Manage").click()
    win = page.get_by_role("dialog", name="Agent library")
    win.get_by_role("listitem").filter(has_text="Code reviewer").click()
    expect(win).to_contain_text("Edits here change the agent for everyone")
    win.get_by_role("button", name="More actions for code_reviewer").click()
    item = page.get_by_role("menuitem", name=re.compile("^Delete agent"))
    expect(item).to_be_disabled()
    expect(item).to_contain_text("Built-in agents can't be deleted")
    move = page.get_by_role("menuitem", name=re.compile("^Move to workspace"))
    expect(move).to_be_disabled()  # a built-in agent is only ever copied
    assert page.errors == []

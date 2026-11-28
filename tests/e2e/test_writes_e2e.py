"""Writes with autonomy in a real browser: the agent's Write goes through the studio's real guard
hook (see FakeModel.writes); the person approves or rejects it on the write card, and a change of
autonomy saved in the editor reaches the waiting write."""
from __future__ import annotations

from pathlib import Path

import pytest

try:
    from playwright.sync_api import expect
except ImportError:  # the fixtures skip (or fail, in CI) with the reason
    expect = None

pytestmark = pytest.mark.e2e

CONTENT = "def hello():\n    return 'hi'\n"


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    return tmp_path / "repo"  # the workspace of the `dev` flow (made by the studio fixture)


def start_dev_run(page, model) -> None:
    model.writes["coder"] = ("hello.py", CONTENT)
    page.get_by_role("combobox", name="Flow").select_option("dev")
    expect(page.get_by_role("combobox", name="Flow")).to_have_value("dev")
    page.get_by_role("button", name="Run", exact=True).click()
    page.get_by_role("textbox", name="Task *").fill("add a hello function")
    page.get_by_role("button", name="▶ Run").click()


def write_card(page):
    return page.get_by_role("group", name="Write requested by coder · dev")


def test_a_write_waits_for_the_person_and_is_written_when_approved(page, model, repo):
    start_dev_run(page, model)
    card = write_card(page)
    expect(card).to_be_visible()
    expect(card).to_contain_text("hello.py")  # what and where, before anything is written
    expect(card).to_contain_text("def hello():")
    assert not (repo / "hello.py").exists()
    card.get_by_role("button", name="Approve write").click()
    expect(card).to_be_hidden()
    expect(page.get_by_role("list", name="Steps").get_by_role("button", name="coder")
           ).to_contain_text("done")
    assert (repo / "hello.py").read_text(encoding="utf-8") == CONTENT
    assert page.errors == []


def test_a_rejected_write_is_never_written(page, model, repo):
    start_dev_run(page, model)
    card = write_card(page)
    expect(card).to_be_visible()
    card.get_by_role("textbox", name="Comment (optional; if you reject, explain why)"
                     ).fill("not in this file")
    card.get_by_role("button", name="Reject").click()
    page.get_by_role("dialog").get_by_role("button", name="Reject").click()  # it asks first
    expect(card).to_be_hidden()
    expect(page.get_by_role("list", name="Steps").get_by_role("button", name="coder")
           ).to_contain_text("done")
    assert not (repo / "hello.py").exists()
    denied = model.decisions[-1]["hookSpecificOutput"]
    assert denied["permissionDecision"] == "deny" and "not in this file" in str(denied)
    assert page.errors == []


def test_autonomy_saved_while_a_write_waits_lets_it_through(page, model, repo):
    start_dev_run(page, model)
    expect(write_card(page)).to_be_visible()
    page.get_by_role("button", name="Properties", exact=True).click()
    page.get_by_role("combobox", name="Autonomy").select_option("auto")
    page.get_by_role("button", name="Save", exact=True).click()
    expect(page.locator(".tb-toast")).to_contain_text("Saved to flows/dev/flow.yaml")  # the visible toast
    page.get_by_role("button", name="Run", exact=True).click()
    expect(write_card(page)).to_have_count(0)  # approved by the new autonomy, no click
    expect(page.get_by_role("list", name="Steps").get_by_role("button", name="coder")
           ).to_contain_text("done")
    assert (repo / "hello.py").read_text(encoding="utf-8") == CONTENT
    assert page.errors == []

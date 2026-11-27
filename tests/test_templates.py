"""Flow templates (render, API, CLI, extensions) and the read-only workspace.git tool."""
import asyncio
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient

from agent_flow_studio import flows_cli
from agent_flow_studio.builder import templates as tpl
from agent_flow_studio.builder.compile import compile_flow
from agent_flow_studio.connections import store
from agent_flow_studio.connections.model import Connection
from agent_flow_studio.editor.server import create_app
from agent_flow_studio.tools import git as git_tool

H = {"X-Flow-Editor": "1"}
needs_git = pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    r = tmp_path / "repo"
    r.mkdir()
    if shutil.which("git"):
        g = ["git", "-c", "user.name=t", "-c", "user.email=t@example.com", "-c",
             "init.defaultBranch=main"]
        subprocess.run([*g, "init", "-q"], cwd=r, check=True)
        (r / "app.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
        subprocess.run([*g, "add", "."], cwd=r, check=True)
        subprocess.run([*g, "commit", "-q", "-m", "first"], cwd=r, check=True)
        subprocess.run([*g, "switch", "-q", "-c", "feature"], cwd=r, check=True)
        (r / "app.py").write_text("def add(a, b):\n    return a - b\n", encoding="utf-8")
        subprocess.run([*g, "commit", "-q", "-am", "change add"], cwd=r, check=True)
    return r


@pytest.fixture
def gh_connector(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("CONNECTIONS_FILE", str(tmp_path / "connections.yaml"))
    store.save({"gh": Connection.model_validate(
        {"type": "github", "access": "write", "auth": {"method": "pat"},
         "secrets": {"token": "${GH_TOKEN}"}})})


PARAMS = {"code-review": {}, "test-runner": {"test_command": "pytest -q"},
          "fix-and-pr": {"connector": "gh", "repository": "me/app"}}


# --- render ----------------------------------------------------------------------------------
@pytest.mark.parametrize("tid", sorted(PARAMS))
def test_core_templates_render_and_compile(tid, repo, gh_connector, tmp_path):
    spec, prompts = tpl.render(tid, f"my-{tid}", "Mine", {"workspace": str(repo), **PARAMS[tid]})
    assert spec.name == f"my-{tid}" and spec.title == "Mine"
    assert spec.workspace is not None and spec.workspace.path == str(repo)
    assert spec.autonomy == "approve"  # templates never ship "auto"
    assert spec.execution == "graph" and spec.inputs  # typed inputs, run by the graph engine
    assert all(a.outputs for a in spec.agents.values())
    for text in [*prompts.values(), yaml.safe_dump(spec.model_dump(mode="json"))]:
        assert "{{" not in text
    d = tmp_path / "flows" / spec.name
    for rel, text in prompts.items():
        (d / rel).parent.mkdir(parents=True, exist_ok=True)
        (d / rel).write_text(text, encoding="utf-8")
    compiled = compile_flow(spec, d, resolve_secrets=False, configure_artifacts=False)
    assert "mcp__workspace__git" in {t for ts in compiled.agent_tools.values() for t in ts}


def test_defaults_and_values_fill_prompts(repo):
    _, prompts = tpl.render("code-review", "cr", "CR", {"workspace": str(repo)})
    assert "against `main`" in prompts["prompts/reviewer.md"]
    _, prompts = tpl.render("code-review", "cr", "CR", {"workspace": str(repo),
                                                        "base_branch": "develop"})
    assert "against `develop`" in prompts["prompts/reviewer.md"]


def test_values_never_change_the_yaml_structure(repo):
    spec, _ = tpl.render("test-runner", "tr", "TR", {
        "workspace": str(repo), "test_command": "pytest -q -k 'a or b' # {x: y}, z: w"})
    assert spec.description is not None and "# {x: y}, z: w" in spec.description
    assert set(spec.agents) == {"test_planner", "test_runner", "diagnoser"}


@pytest.mark.parametrize("params,needle", [
    ({}, "Repository folder: required"),
    ({"workspace": "/does/not/exist/anywhere"}, "folder not found"),
    ({"base_branch": "-x"}, "invalid branch"),
    ({"base_branch": "a..b"}, "invalid branch"),
    ({"nope": "1"}, "unknown parameter"),
    ({"base_branch": "line\nbreak"}, "one line"),
])
def test_param_checks(params, needle, repo):
    p = {"workspace": str(repo), **params} if "workspace" not in params and params != {} else params
    with pytest.raises(tpl.TemplateError, match=needle):
        tpl.render("code-review", "cr", "CR", p)


def test_fix_and_pr_connector_param(repo):
    with pytest.raises(tpl.TemplateError, match="invalid connector"):
        tpl.render("fix-and-pr", "f", "F", {"workspace": str(repo), "connector": "Bad Name",
                                            "repository": "me/app"})
    spec, _ = tpl.render("fix-and-pr", "f", "F", {"workspace": str(repo), "connector": "gh",
                                                  "repository": "me/app"})
    assert spec.agents["analyst"].connections == ["gh"]
    assert spec.agents["pr_opener"].write_connections == ["gh"]


def test_extension_templates_and_duplicates(tmp_path, monkeypatch, repo):
    ext = tmp_path / "ext_templates"
    shutil.copytree(tpl.CORE_DIR / "code-review", ext / "my-review")
    monkeypatch.setattr(tpl.LOADED, "template_dirs", [("acme", ext)])
    ids = {t["id"]: t["source"] for t in tpl.templates_dict()}
    assert ids["my-review"] == "acme" and ids["code-review"] == "core"
    shutil.copytree(tpl.CORE_DIR / "code-review", ext / "code-review")
    with pytest.raises(tpl.TemplateError, match="exists twice"):
        tpl.list_templates()


# --- editor API and CLI ----------------------------------------------------------------------
def test_editor_creates_a_flow_from_a_template(tmp_path, repo):
    flows = tmp_path / "flows"
    flows.mkdir()
    c = TestClient(create_app(flows, allowed_hosts=["testserver"]))
    listed = {t["id"]: t for t in c.get("/api/templates").json()}
    assert {"code-review", "test-runner", "fix-and-pr"} <= set(listed)
    assert listed["fix-and-pr"]["params"]["connector"]["connector_access"] == "write"
    body = {"name": "review-app", "title": "Review app", "template": "code-review",
            "params": {"workspace": str(repo), "base_branch": "main"}}
    assert c.post("/api/flows", json=body).status_code == 403
    assert c.post("/api/flows", json=body, headers=H).status_code == 201
    assert (flows / "review-app/prompts/reviewer.md").is_file()
    loaded = c.get("/api/flows/review-app").json()
    assert loaded["ok"] and loaded["spec"]["title"] == "Review app"
    bad = {**body, "name": "other", "params": {"workspace": str(repo / "missing")}}
    r = c.post("/api/flows", json=bad, headers=H)
    assert r.status_code == 422 and "folder not found" in r.json()["detail"]
    assert not (flows / "other").exists()
    both = {**body, "name": "both", "copy_from": "review-app"}
    assert c.post("/api/flows", json=both, headers=H).status_code == 400


def test_cli_templates_and_new(tmp_path, repo, monkeypatch, capsys):
    monkeypatch.setenv("FLOWS_DIR", str(tmp_path / "flows"))
    monkeypatch.setattr(sys, "argv", ["agent-flow-studio-flows", "templates"])
    flows_cli.main()
    assert "test-runner" in capsys.readouterr().out
    monkeypatch.setattr(sys, "argv", ["agent-flow-studio-flows", "new", "tests-app",
                                      "--template", "test-runner", "--set", f"workspace={repo}",
                                      "--set", "test_command=pytest -q"])
    flows_cli.main()
    assert (tmp_path / "flows/tests-app/flow.yaml").is_file()
    monkeypatch.setattr(sys, "argv", ["agent-flow-studio-flows", "new", "tests-app",
                                      "--template", "test-runner"])
    with pytest.raises(SystemExit, match="already exists"):
        flows_cli.main()


# --- workspace.git ---------------------------------------------------------------------------
@pytest.mark.parametrize("command,args,needle", [
    ("commit", [], "not allowed"),
    ("push", ["origin"], "not allowed"),
    ("diff", ["--output=x.txt"], "option '--output=x.txt' not allowed"),
    ("diff", ["--ext-diff"], "not allowed"),
    ("log", ["--exec=rm"], "not allowed"),
    ("diff", ["-c"], "not allowed"),
    ("show", ["HEAD", "--", "../secret"], "inside the workspace"),
    ("blame", ["--", "C:/Windows/win.ini"], "inside the workspace"),
    ("log", ["a\nb"], "single-line"),
])
def test_git_args_are_checked(command, args, needle):
    with pytest.raises(ValueError, match=needle):
        git_tool.check_args(command, args)


def test_git_allowed_args():
    assert git_tool.check_args("log", ["main..HEAD", "--oneline", "-n", "5", "-20"])
    assert git_tool.check_args("diff", ["main...HEAD", "--stat", "--unified=3", "--", "app.py"])
    assert git_tool.check_args("blame", ["-L10,20", "--", "app.py"])


@needs_git
def test_git_runs_read_only_in_the_workspace(repo):
    git_tool.configure(repo)
    try:
        r = asyncio.run(git_tool.run_git("diff", ["main...HEAD"]))
        assert r["exit_code"] == 0 and "-    return a + b" in r["output"]
        log = asyncio.run(git_tool.run_git("log", ["--oneline", "main..HEAD"]))
        assert "change add" in log["output"]
        cur = asyncio.run(git_tool.run_git("branch", ["--show-current"]))
        assert cur["output"].strip() == "feature"
    finally:
        git_tool.configure(None)
    with pytest.raises(ValueError, match="no workspace"):
        asyncio.run(git_tool.run_git("status", []))


@needs_git
def test_git_tool_reports_errors_as_tool_errors(repo):
    git_tool.configure(repo)
    try:
        out = asyncio.run(git_tool.git.handler({"command": "diff", "args": ["--output=x"]}))
        assert out["is_error"] and "not allowed" in out["content"][0]["text"]
    finally:
        git_tool.configure(None)
    assert not (repo / "x").exists()


@needs_git
def test_git_finds_the_repos_below_a_folder_that_is_not_one(tmp_path):
    """A workspace folder holding several repositories: with one the tool uses it; with several,
    `repo` chooses and the error lists them; a folder outside the workspace is refused."""
    def init(d):
        d.mkdir()
        g = ["git", "-c", "user.name=t", "-c", "user.email=t@example.com"]
        subprocess.run(["git", "init", "-q"], cwd=d, check=True)
        (d / "a.txt").write_text(d.name, encoding="utf-8")
        subprocess.run(["git", "add", "."], cwd=d, check=True)
        subprocess.run([*g, "commit", "-q", "-m", f"first in {d.name}"], cwd=d, check=True)

    top = tmp_path / "work"
    top.mkdir()
    init(top / "apps")
    (top / "docs").mkdir()  # not a repository
    git_tool.configure(top)
    try:
        assert set(git_tool.repos()) == {"apps"}
        r = asyncio.run(git_tool.run_git("log", ["--oneline"]))  # the only one
        assert "first in apps" in r["output"]
        init(top / "srv")
        with pytest.raises(ValueError, match="several git repositories: pass repo, one of apps, srv"):
            asyncio.run(git_tool.run_git("log", ["--oneline"]))
        r = asyncio.run(git_tool.run_git("log", ["--oneline"], repo="srv"))
        assert "first in srv" in r["output"]
        for bad in ("../work/apps", "docs", str(top / "apps")):
            with pytest.raises(ValueError):
                asyncio.run(git_tool.run_git("status", [], repo=bad))
    finally:
        git_tool.configure(None)

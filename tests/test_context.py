"""The workspace context: sections in context.md (roots and notes), its state per root
(context.json with the git commit), what the agents get in their prompts, the context keeper
(explore / update, read-only, the studio stores its section), the step after a run (always /
suggest / never) and the editor API. No real model is called."""
import asyncio
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from agent_flow_studio import context, workspaces
from agent_flow_studio.builder.compile import compile_agent
from agent_flow_studio.builder.spec import FlowSpec
from agent_flow_studio.editor.runner import Runner
from agent_flow_studio.editor.server import create_app
from agent_flow_studio.messages import MsgError
from agent_flow_studio.tools import io as io_tool

sys.path.insert(0, str(Path(__file__).parent))  # the fake SDK helpers of the engine tests
from test_engine import agent_query, ran, types, write_flow

H = {"X-Flow-Editor": "1"}
needs_git = pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@example.com", *args],
                          cwd=repo, check=True, capture_output=True, text=True).stdout.strip()


@pytest.fixture
def repo(tmp_path) -> Path:
    r = tmp_path / "game"
    r.mkdir()
    (r / "README.md").write_text("# Game\n", encoding="utf-8")
    if shutil.which("git"):
        _git(r, "init", "-q")
        _git(r, "add", ".")
        _git(r, "commit", "-q", "-m", "first")
    return r


@pytest.fixture
def ws(repo) -> workspaces.Workspace:
    w = workspaces.create("game", "repo", repo, title="Game")
    assert w.spec is not None
    w.spec = workspaces.WorkspaceSpec.model_validate({
        **w.spec.model_dump(mode="json"), "roots": {"app": {"path": "."},
                                                    "docs": {"path": "docs"}}})
    workspaces.save(w)
    (repo / "docs").mkdir()
    return w


FLOW = {"version": 1, "name": "fix", "title": "Fix", "execution": "graph", "task": "Fix it.",
        "start": "coder", "inputs": {"bug": {"type": "text"}},
        "agents": {"coder": {"description": "Codes.", "prompt": "Fix the bug.",
                             "outputs": {"done": {"type": "text"}}}}}


# --- sections --------------------------------------------------------------------------------
def test_sections_state_and_text(ws):
    context.set_section(ws, "app", "## Stack\nPython 3.12.\n# Layout\nsrc/", commit="abc",
                        by="r1")
    context.set_section(ws, context.NOTES, "Use the dev branch.")
    secs = context.sections(ws)
    assert secs["app"] == "### Stack\nPython 3.12.\n### Layout\nsrc/"  # own headings demoted
    assert secs["notes"] == "Use the dev branch."
    assert context.state(ws)["app"]["commit"] == "abc"
    text = context.context_text(ws)
    assert text.index("### Notes") < text.index("### Root `app`") and "Python 3.12." in text
    md = (ws.folder / "context.md").read_text(encoding="utf-8")
    assert md.startswith("# Workspace context — Game") and "<!-- afs:begin app -->" in md
    # cut at the limit, with where to find the rest
    context.set_section(ws, "docs", "x " * 5000)
    assert "(cut here" in context.context_text(ws, limit=500)
    # an empty text removes a section
    context.set_section(ws, "docs", "")
    assert "docs" not in context.sections(ws)


@pytest.mark.parametrize("name, body, code", [
    ("app", "token ghp_" + "a" * 36, "ctx.secret"),
    ("app", "x" * (context.MAX_SECTION + 1), "ctx.too_long"),
    ("nope", "text", "ctx.unknown_root"),
])
def test_section_rules(ws, name, body, code):
    with pytest.raises(MsgError) as e:
        context.set_section(ws, name, body)
    assert e.value.msgs[0].code == code


def test_the_studio_has_no_context():
    assert context.context_text(workspaces.STUDIO_WS) == ""
    with pytest.raises(MsgError):
        context.set_section(workspaces.STUDIO_WS, "notes", "x")


def test_agents_read_the_context(ws, tmp_path):
    context.set_section(ws, "app", "The game engine is in src/engine.")
    spec = FlowSpec.model_validate(FLOW)
    with workspaces.using(ws):
        prompt = compile_agent(spec, tmp_path, "coder").system_prompt
        assert "## Workspace context" in prompt and "src/engine" in prompt
        off = FlowSpec.model_validate({**FLOW, "use_context": False})
        assert "Workspace context" not in compile_agent(off, tmp_path, "coder").system_prompt
    assert "Workspace context" not in compile_agent(spec, tmp_path, "coder").system_prompt


def test_keeper_flow_is_read_only():
    spec = FlowSpec.model_validate(context.keeper_flow("app"))
    a = spec.agents[context.KEEPER_AGENT]
    assert not [t for t in a.tools if t.split(".")[1] in ("edit", "write", "bash")]
    assert spec.workspace.root == "app" and not spec.use_context


# --- the context keeper ------------------------------------------------------------------------
def _go(rn: Runner, ws, fn):
    async def go():
        with workspaces.using(ws):
            r = fn()
        while r.task and not r.task.done():
            await asyncio.sleep(0.01)
        return r
    return asyncio.run(go())


@needs_git
def test_explore_then_update(ws, repo, tmp_path):
    seen = []

    def keeper(rid, n):
        values, _ = io_tool.resolve_inputs(io_tool.spec_for(rid), rid, context.KEEPER_AGENT)
        seen.append(values)
        return {"section": f"### Layout\nREADME at the top (pass {n}).", "summary": "ok"}

    q = agent_query({context.KEEPER_AGENT: keeper})
    rn = Runner(tmp_path / "flows", q)
    r = _go(rn, ws, lambda: rn.update_context("explore", ["app"]))
    assert r.status == "done", [e.get("message") for e in r.events if e["type"] in ("context", "agent_done")]
    assert seen[0] == {"mode": "explore", "root": "app"}
    head = _git(repo, "rev-parse", "HEAD")
    assert context.state(ws)["app"]["commit"] == head
    assert "pass 1" in context.sections(ws)["app"]
    assert [e["status"] for e in types(r, "context")] == ["running", "done"]
    assert all(e.get("sub") == "context:app" for e in types(r, "agent_done"))
    # the keeper ran in the root's own folder, with read-only tools only
    opts = q.log[0]["options"]
    assert Path(opts.cwd) == repo.resolve() and set(opts.tools) <= {"Read", "Glob", "Grep"}
    # a new commit, then an update: it gets the current text and the last commit
    (repo / "main.py").write_text("print(1)\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "main")
    assert context.roots_state(ws)["app"]["commits_since"] == 1
    r = _go(rn, ws, lambda: rn.update_context("update", ["app"]))
    assert r.status == "done"
    assert seen[1]["current"].startswith("### Layout") and seen[1]["since"] == head
    assert context.roots_state(ws)["app"]["commits_since"] == 0
    audit = (tmp_path / "runs/audit.jsonl").read_text(encoding="utf-8")
    assert '"event": "workspace_context"' in audit


def test_a_failed_root_does_not_stop_the_others(ws, tmp_path):
    q = agent_query({context.KEEPER_AGENT: lambda rid, n: {"section": "" if n == 1 else "Docs.",
                                                           "summary": "s"}})
    rn = Runner(tmp_path / "flows", q)
    r = _go(rn, ws, lambda: rn.update_context("explore"))
    assert r.status == "error"
    assert [(e["root"], e["status"]) for e in types(r, "context") if e["status"] != "running"] \
        == [("app", "error"), ("docs", "done")]
    assert context.sections(ws) == {"docs": "Docs."}


@pytest.mark.parametrize("how, mode, ran_keeper, mark", [
    ("always", "full", True, "updated"),
    ("suggest", "full", False, "suggest"),
    ("never", "full", False, None),
    ("always", "agent", False, None),
])
def test_after_a_run(ws, tmp_path, how, mode, ran_keeper, mark):
    write_flow(ws.flows_dir, {**FLOW, "context_update": how})
    q = agent_query({"coder": lambda rid, n: {"done": "fixed the jump"},
                     context.KEEPER_AGENT: lambda rid, n: {"section": "Updated.", "summary": "s"}})
    rn = Runner(tmp_path / "flows", q)
    kw = {"target": "coder"} if mode == "agent" else {}
    r = _go(rn, ws, lambda: rn.start("fix", run_id="f1", inputs={"bug": "jump"}, mode=mode,
                                     **kw))
    assert r.status == "done", r.events[-3:]
    assert (context.KEEPER_AGENT in ran(q)) is ran_keeper
    assert types(r, "end")[-1].get("context") == mark
    if ran_keeper:
        keeper = next(c for c in q.log if c["agent"] == context.KEEPER_AGENT)
        assert "fixed the jump" in keeper["prompt"] or "fixed the jump" in json.dumps(
            io_tool.flow_inputs("f1--app"))


def test_context_api(ws, tmp_path):
    (tmp_path / "flows").mkdir()
    c = TestClient(create_app(tmp_path / "flows", allowed_hosts=["testserver"]))
    assert c.get("/api/context").json()["studio"] is True
    c.cookies.set("afs_ws", "game")
    got = c.get("/api/context").json()
    assert got["studio"] is False and set(got["roots"]) == {"app", "docs"}
    assert got["roots"]["app"]["explored"] is False
    r = c.put("/api/context/notes", headers=H, json={"text": "Ask Ana before a release."})
    assert r.status_code == 200 and r.json()["sections"]["notes"].startswith("Ask")
    r = c.put("/api/context/app", headers=H, json={"text": "pwd ghp_" + "a" * 36})
    assert r.status_code == 422 and r.json()["detail"]["code"] == "ctx.secret"
    assert c.post("/api/context/update", headers=H, json={"mode": "bad"}).status_code == 400
    assert c.post("/api/context/update", headers=H,
                  json={"roots": ["nope"]}).status_code == 400


def test_context_update_through_the_api(ws, tmp_path, monkeypatch):
    import time

    from agent_flow_studio.editor import runner as runner_mod

    q = agent_query({context.KEEPER_AGENT: lambda rid, n: {"section": "Docs here.", "summary": "s"}})
    monkeypatch.setattr(runner_mod.Runner.__init__, "__defaults__", (None, q, None))
    (tmp_path / "flows").mkdir()
    with TestClient(create_app(tmp_path / "flows", allowed_hosts=["testserver"])) as c:
        c.cookies.set("afs_ws", "game")
        r = c.post("/api/context/update", headers=H, json={"mode": "explore", "roots": ["docs"]})
        assert r.status_code == 200, r.text
        rid = r.json()["id"]
        for _ in range(100):
            run = next(x for x in c.get("/api/runs").json() if x["id"] == rid)
            if run["status"] != "running":
                break
            time.sleep(0.05)
        assert run["status"] == "done"
        assert c.get("/api/context").json()["sections"]["docs"] == "Docs here."

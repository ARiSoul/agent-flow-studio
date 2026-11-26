"""Learning the history of a workspace root from past Claude Code sessions and memory notes:
reading the sources (fake CLAUDE_CONFIG_DIR with hand-written session files), the digest (text
only, reminders stripped, secrets masked), the readers and the history keeper (fake model), the
draft the person reviews, the history section and the API. No real session, no real model."""
import json
import re
import sys
import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from agent_flow_studio import claude_sessions as cs
from agent_flow_studio import context, workspaces
from agent_flow_studio.editor.runner import Runner
from agent_flow_studio.editor.server import create_app
from agent_flow_studio.messages import MsgError
from agent_flow_studio.tools import io as io_tool

sys.path.insert(0, str(Path(__file__).parent))
from test_context import _go
from test_engine import agent_query, types

H = {"X-Flow-Editor": "1"}
TOKEN = "ghp_" + "a" * 36


@pytest.fixture
def ws(tmp_path) -> workspaces.Workspace:
    """A repository workspace with two roots: `app` (the repository) and `docs`."""
    repo = tmp_path / "game"
    (repo / "docs").mkdir(parents=True)
    w = workspaces.create("game", "repo", repo, title="Game")
    w.spec = workspaces.WorkspaceSpec.model_validate({
        **w.spec.model_dump(mode="json"), "roots": {"app": {"path": "."},
                                                    "docs": {"path": "docs"}}})
    workspaces.save(w)
    return w


def _folder(ws_) -> str:
    return ws_.resolve(None)[1]["app"]


def _pdir(folder: str) -> Path:
    d = cs.config_dir() / "projects" / re.sub(r"[^a-zA-Z0-9]", "-", folder)
    d.mkdir(parents=True, exist_ok=True)
    return d


def write_session(folder: str, msgs: list[tuple[str, object]], *, entry: str = "claude-desktop",
                  title: str | None = None, tag: str | None = None) -> str:
    """A session file as Claude Code writes it (only the fields the SDK reads)."""
    sid, parent, lines = str(uuid.uuid4()), None, []
    for role, content in msgs:
        u = str(uuid.uuid4())
        message = {"role": role, "content": content}
        if role == "assistant":
            message |= {"type": "message", "model": "m", "id": u}
        lines.append(_j({"type": role, "uuid": u, "parentUuid": parent, "sessionId": sid,
                                 "cwd": folder, "timestamp": "2026-11-20T10:00:00.000Z",
                                 "gitBranch": "main", "entrypoint": entry, "isSidechain": False,
                                 "userType": "external", "message": message}))
        parent = u
    if title:
        lines.append(_j({"type": "custom-title", "customTitle": title, "sessionId": sid}))
    if tag:
        lines.append(_j({"type": "tag", "tag": tag, "sessionId": sid}))
    path = _pdir(folder) / f"{sid}.jsonl"
    if sys.platform == "win32" and len(str(path)) >= 260:
        # Claude Code names the project folder after the whole path, so a deep temporary folder
        # goes over the 260 characters of a Windows path (unless long paths are enabled)
        pytest.skip(f"Windows path too long ({len(str(path))}): run with a short --basetemp")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return sid


def _j(v: object) -> str:
    return json.dumps(v, separators=(",", ":"))  # compact, as Claude Code writes


def say(text: str) -> list[dict[str, str]]:
    return [{"type": "text", "text": text}]


@pytest.fixture
def sources(ws):
    folder = _folder(ws)
    person = write_session(folder, [
        ("user", "Tests go in tests/. <system-reminder>hidden reminder</system-reminder>"),
        ("assistant", [*say("Noted: pytest in tests/."),
                       {"type": "tool_use", "id": "t1", "name": "Bash", "input": {"c": "ls"}}]),
        ("user", [{"type": "tool_result", "tool_use_id": "t1", "content": "TOOL OUTPUT"}]),
        ("assistant", say(f"The token was {TOKEN}, never commit it.")),
    ], title="Testing setup")
    studio = write_session(folder, [("user", "You are the agent `x` of the flow `y`."),
                                    ("assistant", say("ok"))], entry="sdk-py")
    program = write_session(folder, [("user", "Summarize the repo"), ("assistant", say("ok"))],
                            entry="sdk-ts")
    tagged = write_session(folder, [("user", "Custom task"), ("assistant", say("ok"))],
                           tag=cs.STUDIO_TAG)
    empty = write_session(folder, [("user", "<system-reminder>only a reminder</system-reminder>"),
                                   ("assistant", [{"type": "tool_use", "id": "t", "name": "Bash",
                                                   "input": {}}])])
    mem = _pdir(folder) / "memory"
    mem.mkdir()
    (mem / "MEMORY.md").write_text("- [Branches](branches.md)\n", encoding="utf-8")
    (mem / "branches.md").write_text("---\nname: branches\ndescription: Work on dev.\n"
                                     "metadata:\n  type: project\n---\nAlways branch from dev.\n",
                                     encoding="utf-8")
    return {"folder": folder, "person": person, "studio": studio, "program": program,
            "tagged": tagged, "empty": empty}


# --- reading the sources -----------------------------------------------------------------------
def test_list_sources(sources):
    got = {s["id"]: s for s in cs.list_sessions(sources["folder"])}
    assert got[sources["person"]]["kind"] == "person"
    assert got[sources["person"]]["title"] == "Testing setup"
    assert "hidden reminder" not in got[sources["person"]]["first_prompt"]
    assert got[sources["studio"]]["kind"] == "studio"
    assert got[sources["tagged"]]["kind"] == "studio"
    assert got[sources["program"]]["kind"] == "program"
    assert all(s["state"] == "new" for s in got.values())
    mod = got[sources["person"]]["modified"]
    again = {s["id"]: s for s in cs.list_sessions(sources["folder"],
                                                  {sources["person"]: mod,
                                                   sources["program"]: mod - 100})}
    assert again[sources["person"]]["state"] == "learned"
    assert again[sources["program"]]["state"] == "changed"
    notes = cs.list_memory(sources["folder"])
    assert [(n["file"], n["name"], n["description"]) for n in notes] \
        == [("branches.md", "branches", "Work on dev.")]
    assert cs.list_sessions(str(Path(sources["folder"]) / "nowhere")) == []


def test_digest_keeps_only_the_conversation(sources):
    text = cs.digest(sources["person"], sources["folder"])
    assert "**Person:** Tests go in tests/." in text and "**Claude:** Noted" in text
    assert "hidden reminder" not in text and "TOOL OUTPUT" not in text and "Bash" not in text
    assert TOKEN not in text and "[secret removed]" in text
    assert cs.digest(sources["empty"], sources["folder"]) == ""
    mem = cs.memory_digest(sources["folder"], ["branches.md", "../x.md", "MEMORY.md"])
    assert "Always branch from dev." in mem and "../" not in mem
    est = cs.estimate(sources["folder"], [{"id": sources["person"]}], ["branches.md"])
    assert est["chars"] > 0 and est["tokens"] == est["chars"] // 4


def test_cut_keeps_start_and_end():
    text = "SUMMARY\n" + "\n".join(f"line {i}" for i in range(5000)) + "\nLATEST"
    out = cs.cut(text, 2000)
    assert len(out) <= 2100 and out.startswith("SUMMARY") and out.endswith("LATEST")
    assert "was cut" in out


def test_compaction_summary_is_kept(ws):
    folder = _folder(ws)
    sid = write_session(folder, [
        ("user", ("This session is being continued from a previous conversation. Summary: "
                  "we moved the build to Vite.")), ("assistant", say("Continuing."))])
    assert "Summary of earlier work in this session:**\nThis session" in cs.digest(sid, folder)


# --- the history section ---------------------------------------------------------------------
def test_history_section(ws):
    context.set_section(ws, "app", "The engine is in src/.")
    context.set_section(ws, "app.history", "### Decisions\nVite, not webpack. [s1]")
    text = context.context_text(ws)
    assert text.index("### Root `app` — ") < text.index("history (from past sessions")
    assert "Vite, not webpack" in text
    md = (ws.folder / "context.md").read_text(encoding="utf-8")
    assert "<!-- afs:begin app.history -->" in md and "## app — history" in md
    assert context.sections(ws)["app.history"].startswith("### Decisions")
    assert context.roots_state(ws)["app"]["history"]["exists"] is True
    # a cut drops the history first
    context.set_section(ws, "docs", "d " * 7400)
    assert "Vite" not in context.context_text(ws, limit=14_500)
    with pytest.raises(MsgError) as e:
        context.set_section(ws, "app.history", "x" * (context.MAX_HISTORY + 1))
    assert e.value.msgs[0].code == "ctx.too_long"
    with pytest.raises(MsgError) as e:
        context.set_section(ws, "nope.history", "x")
    assert e.value.msgs[0].code == "ctx.unknown_root"


def test_a_renamed_root_keeps_its_history(ws):
    context.set_section(ws, "docs.history", "Docs were moved. [s2]")
    before = {k: r.path for k, r in ws.spec.roots.items()}
    ws.spec = workspaces.WorkspaceSpec.model_validate({
        **ws.spec.model_dump(mode="json"), "roots": {"app": {"path": "."},
                                                    "manual": {"path": "docs"}}})
    workspaces.save(ws)
    assert context.follow_renames(ws, before) == {"docs": "manual"}
    assert context.sections(ws) == {"manual.history": "Docs were moved. [s2]"}


# --- the learn run -----------------------------------------------------------------------------
def _learn_query(seen):
    def reader(rid, n):
        values, _ = io_tool.resolve_inputs(io_tool.spec_for(rid), rid, context.READER_AGENT)
        seen.setdefault("reader", []).append(values)
        return {"notes": f"### Conventions\n- From {values['kind']} {n}."}

    def historian(rid, n):
        values, _ = io_tool.resolve_inputs(io_tool.spec_for(rid), rid, context.HISTORIAN_AGENT)
        seen["historian"] = values
        return {"section": "### Conventions\n- Tests in tests/. [Testing setup]",
                "summary": "Added the test convention."}
    return agent_query({context.READER_AGENT: reader, context.HISTORIAN_AGENT: historian})


def test_learn_makes_a_draft_then_apply(ws, sources, tmp_path):
    context.set_section(ws, "app", "Main context.")
    context.set_section(ws, "app.history", "### Decisions\n- Old. [s0]")
    seen: dict = {}
    q = _learn_query(seen)
    rn = Runner(tmp_path / "flows", q)
    r = _go(rn, ws, lambda: rn.learn_context("app", [sources["person"], sources["empty"]],
                                             ["branches.md"]))
    assert r.status == "done", [e.get("message") for e in types(r, "context")]
    kinds = [v["kind"] for v in seen["reader"]]
    assert kinds == ["memory", "session"]  # the empty session failed, the others went on
    assert "Always branch from dev." in seen["reader"][0]["source"]
    assert "TOOL OUTPUT" not in seen["reader"][1]["source"]
    statuses = [(e.get("source"), e["status"]) for e in types(r, "context")
                if e["status"] != "running"]
    assert ("Memory notes", "done") in statuses and ("Testing setup", "done") in statuses
    assert any(s == "error" for _, s in statuses)
    h = seen["historian"]
    assert h["current"].startswith("### Decisions") and h["context"] == "Main context."
    assert "From memory 1" in h["notes"] and "From session" in h["notes"]
    # the historian ran on the root with read-only tools
    opts = next(c for c in q.log if c["agent"] == context.HISTORIAN_AGENT)["options"]
    assert set(opts.tools) <= {"Read", "Glob", "Grep"}
    # nothing saved yet: a draft
    with workspaces.using(ws):
        assert context.sections(ws)["app.history"] == "### Decisions\n- Old. [s0]"
        draft = rn.drafts()["app"]
        assert draft["summary"] == "Added the test convention."
        assert set(draft["sessions"]) == {sources["person"]} and set(draft["memory"]) == {"branches.md"}
        rn.apply_draft("app", draft["section"] + "\n- Edited by the person.")
        assert context.sections(ws)["app.history"].endswith("Edited by the person.")
        assert rn.drafts() == {}
        src = {s["id"]: s for s in rn.sources("app")["sessions"]}
        assert src[sources["person"]]["state"] == "learned"
        assert src[sources["empty"]]["state"] == "new"
        assert rn.sources("app")["memory"][0]["state"] == "learned"
    audit = (tmp_path / "runs/audit.jsonl").read_text(encoding="utf-8")
    assert '"event": "workspace_context_learn"' in audit and "Testing setup" not in audit


def test_learn_with_no_usable_source_fails(ws, sources, tmp_path):
    rn = Runner(tmp_path / "flows", _learn_query({}))
    r = _go(rn, ws, lambda: rn.learn_context("app", [sources["empty"]], []))
    assert r.status == "error"
    with workspaces.using(ws):
        assert rn.drafts() == {}


# --- the API -----------------------------------------------------------------------------------
def test_learn_api(ws, sources, tmp_path, monkeypatch):
    import time

    from agent_flow_studio.editor import runner as runner_mod

    monkeypatch.setattr(runner_mod.Runner.__init__, "__defaults__", (None, _learn_query({}), None))
    (tmp_path / "flows").mkdir()
    with TestClient(create_app(tmp_path / "flows", allowed_hosts=["testserver"])) as c:
        assert c.get("/api/context/sources/app").status_code == 400  # the studio
        c.cookies.set("afs_ws", "game")
        got = c.get("/api/context/sources/app").json()
        assert {s["kind"] for s in got["sessions"]} == {"person", "studio", "program"}
        assert got["memory"][0]["file"] == "branches.md"
        assert c.get("/api/context/sources/nope").status_code == 400
        est = c.post("/api/context/estimate", headers=H,
                     json={"root": "app", "sessions": [sources["person"]], "memory": []}).json()
        assert est["chars"] > 0
        assert c.post("/api/context/learn", headers=H,
                      json={"root": "app", "sessions": [], "memory": []}).status_code == 400
        assert c.post("/api/context/learn", headers=H,
                      json={"root": "app", "sessions": "x"}).status_code == 400
        assert c.post("/api/context/learn", headers=H,
                      json={"root": "app", "sessions": ["nope"]}).status_code == 400
        r = c.post("/api/context/learn", headers=H,
                   json={"root": "app", "sessions": [sources["person"]], "memory": []})
        assert r.status_code == 200, r.text
        rid = r.json()["id"]
        for _ in range(100):
            run = next(x for x in c.get("/api/runs").json() if x["id"] == rid)
            if run["status"] != "running":
                break
            time.sleep(0.05)
        assert run["status"] == "done"
        ctx = c.get("/api/context").json()
        assert ctx["drafts"]["app"]["section"].startswith("### Conventions")
        bad = c.post("/api/context/draft/app", headers=H, json={"section": f"token: {TOKEN}"})
        assert bad.status_code == 422 and bad.json()["detail"]["code"] == "ctx.secret"
        ok = c.post("/api/context/draft/app", headers=H, json={"section": "### Conventions\n- x"})
        assert ok.status_code == 200 and ok.json()["sections"]["app.history"].endswith("- x")
        assert ok.json()["drafts"] == {}
        assert c.post("/api/context/draft/app", headers=H, json={"section": "y"}).status_code == 404
        assert c.delete("/api/context/draft/app", headers=H).status_code == 200


def test_studio_sessions_are_tagged(tmp_path):
    folder = str(tmp_path / "proj")
    sid = write_session(folder, [("user", "A custom orchestrator task"), ("assistant", say("ok"))],
                        entry="cli")
    assert cs.list_sessions(folder)[0]["kind"] == "person"
    cs.mark_ours(sid, folder)
    cs.mark_ours("not-a-uuid", folder)  # never raises
    cs.mark_ours(None)
    assert cs.list_sessions(folder)[0]["kind"] == "studio"

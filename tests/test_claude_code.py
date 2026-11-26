"""The studio and Claude Code, both ways: a step's session goes on in Claude Code (its folder from
the session file, the command, a terminal), and Claude Code drives the studio through the studio
MCP server (list, inputs, start or queue, status with the link; no tool decides for the person)."""
import asyncio
import json
import shutil
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from agent_flow_studio import claude_sessions, studio_mcp
from agent_flow_studio.editor.runner import RunError, Runner
from agent_flow_studio.editor.server import create_app

sys.path.insert(0, str(Path(__file__).parent))
from test_engine import FIX, PAR, agent_query, run, wait

SID = "3f2b8c1e-0000-4000-8000-00000000abcd"


@pytest.fixture
def flows(tmp_path: Path) -> Path:
    shutil.copytree(FIX / "parallel", tmp_path / "flows/parallel")
    return tmp_path / "flows"


@pytest.fixture
def sessions(tmp_path: Path, monkeypatch) -> Path:
    cfg = tmp_path / "claude"
    d = cfg / "projects" / "G--game"
    d.mkdir(parents=True)
    game = tmp_path / "game"
    game.mkdir()
    lines = [{"type": "user", "uuid": "u1", "sessionId": SID, "cwd": str(game), "message": {"role": "user", "content": "go"}}]
    (d / f"{SID}.jsonl").write_text("\n".join(json.dumps(x) for x in lines) + "\n", encoding="utf-8")
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(cfg))
    return game


def test_the_folder_and_command_of_a_session(sessions):
    assert claude_sessions.session_cwd(SID) == str(sessions)
    assert claude_sessions.session_cwd("not a session id!") is None
    assert claude_sessions.session_cwd("00000000-1111-2222-3333-444444444444") is None
    assert claude_sessions.resume_command(SID, "C:\\it's here", True) == \
        f"Set-Location -LiteralPath 'C:\\it''s here'; claude --resume {SID}"
    assert claude_sessions.resume_command(SID, "/a b", False) == f"cd '/a b' && claude --resume {SID}"


def test_a_step_goes_on_in_claude_code(flows, sessions, monkeypatch):
    async def q(prompt, options):
        async for m in agent_query()(prompt, options):
            if type(m).__name__ == "ResultMessage" and "planner" in prompt:
                m.session_id = SID
            yield m

    rn = Runner(flows, q)

    async def go():
        r = rn.start("parallel", inputs={"topic": "bugs"}, run_id="cc1")
        await wait(r)

    run(go())
    got = rn.step_session("cc1", "planner")
    assert got["session_id"] == SID and got["cwd"] == str(sessions) and SID in got["command"]
    with pytest.raises(RunError):
        rn.step_session("cc1", "nobody")
    opened = []
    import subprocess  # open_terminal starts the terminal with subprocess.Popen: record it instead
    monkeypatch.setattr(subprocess, "Popen", lambda args, **kw: opened.append((args, kw)))
    claude_sessions.open_terminal(SID, str(sessions))
    assert opened and any(f"claude --resume {SID}" in str(a) for a, _ in opened)
    with pytest.raises(ValueError):
        claude_sessions.open_terminal("bad id", str(sessions))


def test_claude_code_drives_the_studio_without_deciding(flows, monkeypatch):
    import agent_flow_studio.editor.server as server_mod
    monkeypatch.setattr(server_mod, "Runner", lambda *a, **k: Runner(
        *a, query_fn=agent_query({"planner": lambda rid, n: asyncio.sleep(0.5, PAR["planner"])}),
        **{x: y for x, y in k.items() if x != "query_fn"}), raising=False)
    app = create_app(flows, allowed_hosts=["testserver"])
    with TestClient(app) as c:
        st = studio_mcp.Studio("http://testserver", client=c)
        assert [f["name"] for f in st.list_flows()] == ["parallel"]
        assert st.list_flows()[0]["link"] == "http://testserver/#flow=parallel"
        ins = st.flow_inputs("parallel")
        assert ins["inputs"]["topic"]["type"] == "text" and ins["execution"] == "graph"
        first = st.start_run("parallel", {"topic": "bugs"}, "m1")
        assert first["started"] and first["link"].endswith("#flow=parallel&run=m1")
        status = st.run_status("m1")
        assert status["flow"] == "parallel" and status["link"].endswith("run=m1")
        with pytest.raises(studio_mcp.StudioError):
            st.flow_inputs("../etc")
    tools = asyncio.run(studio_mcp.build_server(st).list_tools())
    names = {t.name for t in tools}
    assert names == {"list_flows", "flow_inputs", "start_run", "run_status", "run_outputs", "run_changes"}
    assert not any(w in n for n in names for w in ("decide", "approve", "reject", "answer"))

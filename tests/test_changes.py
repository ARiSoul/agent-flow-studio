"""What a run changed in its git roots: a snapshot at the start (nothing written to the repo), then
the files that differ — modified, added, deleted; files changed before the run only when the run
changed them again; binary files without a diff; the HTTP endpoint."""
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from agent_flow_studio import changes
from agent_flow_studio.editor.runner import Runner

sys.path.insert(0, str(Path(__file__).parent))
from test_engine import FIX, agent_query, run, wait

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, text=True).stdout


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    r = tmp_path / "game"
    r.mkdir()
    _git(r, "init", "-q")
    _git(r, "config", "user.email", "test@example.com")
    _git(r, "config", "user.name", "Test")
    (r / "bugs.json").write_text('{"bugs": ["ant"]}\n', encoding="utf-8")
    (r / "old.txt").write_text("old\n", encoding="utf-8")
    (r / "mine.md").write_text("draft\n", encoding="utf-8")
    _git(r, "add", ".")
    _git(r, "commit", "-q", "-m", "start")
    return r


def test_the_run_changes_and_nothing_else(repo):
    (repo / "mine.md").write_text("my own edit before the run\n", encoding="utf-8")  # the person's
    (repo / "notes.txt").write_text("untracked before\n", encoding="utf-8")
    objects = sorted(p.name for p in (repo / ".git/objects").iterdir())
    snap = changes.snapshot(repo)
    assert sorted(p.name for p in (repo / ".git/objects").iterdir()) == objects  # nothing written
    assert snap["branch"] and set(snap["dirty"]) == {"mine.md", "notes.txt"}
    # the run
    (repo / "bugs.json").write_text('{"bugs": ["ant", "ladybug"]}\n', encoding="utf-8")
    (repo / "species").mkdir()
    (repo / "species/ladybug.json").write_text('{"legs": 6}\n', encoding="utf-8")
    (repo / "sprite.png").write_bytes(b"\x89PNG\r\n\x1a\n\0\0\0binary")
    (repo / "old.txt").unlink()
    got = {f["path"]: f for f in changes.changes(repo, snap)}
    assert set(got) == {"bugs.json", "species/ladybug.json", "sprite.png", "old.txt"}
    assert got["bugs.json"]["status"] == "modified" and got["bugs.json"]["added"] == 1
    assert '+{"bugs": ["ant", "ladybug"]}' in got["bugs.json"]["diff"]
    assert got["species/ladybug.json"]["status"] == "added" and "+{\"legs\": 6}" in got["species/ladybug.json"]["diff"]
    assert got["sprite.png"]["binary"] and got["sprite.png"]["diff"] == ""
    assert got["old.txt"]["status"] == "deleted"
    # a file the person changed before: shown only once the run changes it again
    (repo / "mine.md").write_text("the run's edit\n", encoding="utf-8")
    again = {f["path"]: f for f in changes.changes(repo, snap)}
    assert again["mine.md"]["before"] is True and "notes.txt" not in again


def test_not_a_repository(tmp_path):
    assert changes.snapshot(tmp_path) is None


def test_a_run_records_its_baseline_and_the_api_gives_the_changes(tmp_path, repo):
    shutil.copytree(FIX / "parallel", tmp_path / "flows/parallel")
    f = tmp_path / "flows/parallel/flow.yaml"
    data = yaml.safe_load(f.read_text(encoding="utf-8"))
    data["workspace"] = {"path": str(repo)}
    f.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")

    def planner(rid, n):  # the planner "writes" in the game repo (as an approved write would)
        (repo / "bugs.json").write_text('{"bugs": ["ant", "beetle"]}\n', encoding="utf-8")
        return {"question_a": "qa", "question_b": "qb", "size": 2}

    rn = Runner(tmp_path / "flows", agent_query({"planner": planner}))

    async def go():
        r = rn.start("parallel", inputs={"topic": "bugs"}, run_id="ch1")
        await wait(r)
        return r

    r = run(go())
    start = next(e for e in r.events if e["type"] == "start")
    assert start["git"]["workspace"]["head"]
    got = rn.run_changes("ch1")
    assert got["available"] and [x["path"] for x in got["roots"][0]["files"]] == ["bugs.json"]

    from fastapi.testclient import TestClient

    from agent_flow_studio.editor import server
    app = server.create_app(tmp_path / "flows", allowed_hosts=["testserver"])
    with TestClient(app) as c:
        body = c.get("/api/runs/ch1/changes").json()
        assert body["roots"][0]["files"][0]["status"] == "modified"
        assert c.get("/api/runs/..%2Fx/changes").status_code in (400, 404)

"""Images and videos of a run: what the agents see (`view_image`, `view_video`), the format taken
from the content and never from the name, which tools an agent gets, and the staged uploads for
large files. Uses the real ffmpeg when it is installed; no real model is called."""
import asyncio
import base64
import json
import shutil
import struct
import subprocess
import time
import zlib
from pathlib import Path

import pytest
from claude_agent_sdk import _convert_tool_content
from fastapi.testclient import TestClient

from agent_flow_studio import main as cli
from agent_flow_studio.builder.compile import MAX_MESSAGE, compile_agent, compile_flow, io_tools
from agent_flow_studio.builder.spec import FlowSpec
from agent_flow_studio.editor import uploads
from agent_flow_studio.editor.runner import RunError, prepare_inputs
from agent_flow_studio.editor.server import create_app
from agent_flow_studio.tools import io as io_tool
from agent_flow_studio.tools import media

H = {"X-Flow-Editor": "1"}
needs_ffmpeg = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg is not installed")


def png(w: int, h: int) -> bytes:
    def chunk(kind: bytes, data: bytes) -> bytes:
        return (struct.pack(">I", len(data)) + kind + data
                + struct.pack(">I", zlib.crc32(kind + data)))
    rows = b"".join(b"\x00" + b"\x40\x80\xc0" * w for _ in range(h))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(rows)) + chunk(b"IEND", b""))


def make(path: Path, *args: str) -> Path:
    subprocess.run(["ffmpeg", "-v", "error", "-y", *args, str(path)], check=True,
                   stdin=subprocess.DEVNULL)
    return path


def video(path: Path, seconds: int = 4, sound: bool = False) -> Path:
    """Red in the first half, blue in the second."""
    half = seconds / 2
    audio = ["-f", "lavfi", "-i", f"sine=d={seconds}"] if sound else []
    return make(path, "-f", "lavfi", "-i", f"color=red:s=320x180:d={half}:r=10",
                "-f", "lavfi", "-i", f"color=blue:s=320x180:d={half}:r=10", *audio,
                "-filter_complex", "[0][1]concat=n=2:v=1[v]", "-map", "[v]",
                *(["-map", "2"] if sound else []))


def colour(block: dict, tmp_path: Path) -> str:
    """"red" or "blue": the main colour of a frame, read back with ffmpeg."""
    f = tmp_path / "frame.jpg"
    f.write_bytes(base64.b64decode(block["data"]))
    raw = subprocess.run(["ffmpeg", "-v", "error", "-i", str(f), "-vf", "scale=1:1", "-f",
                          "rawvideo", "-pix_fmt", "rgb24", "pipe:1"], capture_output=True,
                         check=True, stdin=subprocess.DEVNULL).stdout
    return "red" if raw[0] > raw[2] else "blue"


def in_run(name: str, data: bytes = b"", run: str = "r1") -> Path:
    p = io_tool.run_dir(run) / "inputs" / "f" / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(data)
    return p


def call(tool, **args):
    return asyncio.run(tool.handler({"run_id": "r1", "agent": "a", **args}))


def images(r):
    return [b for b in r["content"] if b["type"] == "image"]


# --- images ------------------------------------------------------------------------------------
def test_a_small_image_goes_as_it_is_without_ffmpeg(monkeypatch):
    monkeypatch.setattr(media, "ffmpeg_exe", lambda: None)
    data = png(40, 30)
    in_run("a.png", data)
    r = call(io_tool.view_image, path="inputs/f/a.png")
    assert r["content"][0]["text"] == "a.png: 40x30"
    block = images(r)[0]
    assert block["mimeType"] == "image/png" and base64.b64decode(block["data"]) == data
    assert _convert_tool_content(r["content"])[1]["type"] == "image"  # the SDK passes it on
    in_run("big.png", png(1600, 10))  # a large one needs ffmpeg, and says so
    assert "ffmpeg is not installed" in call(io_tool.view_image, path="inputs/f/big.png"
                                             )["content"][0]["text"]


@needs_ffmpeg
@pytest.mark.parametrize("ext", ["png", "jpg", "webp", "gif"])
def test_large_images_are_scaled_down(ext):
    p = in_run(f"big.{ext}")
    make(p, "-f", "lavfi", "-i", "testsrc2=s=2400x1200:d=0.1", "-frames:v", "1")
    assert media.image_size(p.read_bytes()) == (2400, 1200)
    r = call(io_tool.view_image, path=f"inputs/f/big.{ext}")
    out = base64.b64decode(images(r)[0]["data"])
    assert media.image_size(out) == (1568, 784)
    assert media.image_type(out) == images(r)[0]["mimeType"]
    assert "scaled from 2400x1200" in r["content"][0]["text"]


def test_files_that_are_not_images_are_refused():
    in_run("fake.png", b"just text")
    assert "is not a PNG" in call(io_tool.view_image, path="inputs/f/fake.png")["content"][0]["text"]
    in_run("shot.png", png(4, 4))
    assert "use view_image" in call(io_tool.read_file, path="inputs/f/shot.png"
                                    )["content"][0]["text"]
    for bad in ("../../secret.png", "inputs/f/none.png"):
        r = call(io_tool.view_image, path=bad)
        assert r["is_error"] and "file not found in the run folder" in r["content"][0]["text"]


# --- videos ------------------------------------------------------------------------------------
@needs_ffmpeg
@pytest.mark.parametrize("ext", ["mp4", "mov", "webm", "mkv", "avi"])
def test_frames_are_spread_over_the_video(ext, tmp_path):
    video(in_run(f"clip.{ext}"))
    r = call(io_tool.view_video, path=f"inputs/f/clip.{ext}", frames=4)
    assert not r.get("is_error"), r
    assert r["content"][0]["text"].startswith(f"clip.{ext}: 4.0 s, 320x180, no sound. 4 frames")
    assert [b["text"] for b in r["content"][1:] if b["type"] == "text"] == [
        "t=0.50s", "t=1.50s", "t=2.50s", "t=3.50s"]
    assert [colour(b, tmp_path) for b in images(r)] == ["red", "red", "blue", "blue"]


@needs_ffmpeg
def test_a_part_of_the_video_and_its_limits(tmp_path):
    video(in_run("clip.mp4"), sound=True)
    r = call(io_tool.view_video, path="inputs/f/clip.mp4", start=2.5, end=99, frames=2)
    assert "it has sound, which you cannot hear" in r["content"][0]["text"]
    assert [colour(b, tmp_path) for b in images(r)] == ["blue", "blue"]
    assert len(images(call(io_tool.view_video, path="inputs/f/clip.mp4", frames=500))) \
        == media.MAX_FRAMES
    r = call(io_tool.view_video, path="inputs/f/clip.mp4", start=50)
    assert r["is_error"] and "inside the video" in r["content"][0]["text"]
    assert "use view_video" in call(io_tool.view_image, path="inputs/f/clip.mp4"
                                    )["content"][0]["text"]
    assert "use view_video" in call(io_tool.read_file, path="inputs/f/clip.mp4"
                                    )["content"][0]["text"]


@needs_ffmpeg
def test_a_playlist_named_mp4_never_opens_other_files(tmp_path):
    other = video(tmp_path / "private.mp4")
    for text in (f"#EXTM3U\n#EXTINF:4,\n{other.as_posix()}\n#EXT-X-ENDLIST\n",
                 f"ffconcat version 1.0\nfile '{other.as_posix()}'\n"):
        in_run("list.mp4", text.encode())
        r = call(io_tool.view_video, path="inputs/f/list.mp4")
        assert r["is_error"] and "is not a video" in r["content"][0]["text"]
        # a fake header is not enough either: the format is forced
        in_run("list.mp4", b"\x00\x00\x00\x18ftyp\n" + text.encode())
        assert call(io_tool.view_video, path="inputs/f/list.mp4")["is_error"]


# --- which tools an agent gets -----------------------------------------------------------------
def flow(accept: list[str]) -> FlowSpec:
    return FlowSpec.model_validate({
        "version": 1, "name": "look", "title": "Look", "execution": "graph", "task": "Look.",
        "start": "eye", "inputs": {"f": {"type": "file", "accept": accept}},
        "agents": {"eye": {"description": "Looks.", "prompt": "Look.",
                           "inputs": {"f": {"from": "input.f"}}}}})


@pytest.mark.parametrize("accept, tools", [
    ([], {"read_file", "view_image", "view_video"}),
    ([".md", ".txt"], {"read_file"}),
    ([".PNG", ".jpg"], {"view_image"}),
    ([".mp4"], {"view_video"}),
    ([".png", ".mp4", ".txt"], {"read_file", "view_image", "view_video"}),
])
def test_tools_follow_the_accepted_files(accept, tools, tmp_path):
    spec = flow(accept)
    got = {t.removeprefix("mcp__io__") for t in io_tools(spec, "eye")} - {"get_inputs"}
    assert got == tools
    o = compile_agent(spec, tmp_path, "eye")
    for t in ("read_file", "view_image", "view_video"):
        assert (f"`{t}`" in o.system_prompt) is (t in tools)
    # images make large messages: the SDK's 1 MB limit would end the session
    assert o.max_buffer_size == MAX_MESSAGE
    assert compile_flow(spec, tmp_path).options.max_buffer_size == MAX_MESSAGE


def test_the_guard_lets_an_agent_call_the_file_tools_as_itself(tmp_path):
    o = compile_agent(flow([]), tmp_path, "eye")
    guard = o.hooks["PreToolUse"][0].hooks[0]
    for t in (io_tool.read_file, io_tool.view_image, io_tool.view_video):
        schema = t.input_schema
        names = schema.get("properties", schema)
        assert "agent" in names  # the guard checks it: the model must know it is there
        for who, ok in (("eye", True), ("other", False)):
            r = asyncio.run(guard({"tool_name": f"mcp__io__{t.name}", "session_id": "s",
                                   "tool_input": {"run_id": "r1", "agent": who, "path": "x"}},
                                  "id", {}))
            assert (r == {}) is ok


# --- large files -------------------------------------------------------------------------------
def test_staged_uploads_reach_the_run_and_can_be_used_again(tmp_path):
    (tmp_path / "flows").mkdir()
    c = TestClient(create_app(tmp_path / "flows", allowed_hosts=["testserver"]))
    assert c.put("/api/uploads", content=b"x").status_code == 403  # no X-Flow-Editor header
    r = c.put("/api/uploads", content=b"v" * 100_000, headers=H)
    assert r.status_code == 201 and r.json()["size"] == 100_000
    ref = {"name": "clip.mp4", "upload": r.json()["upload"]}
    for run in ("r1", "r2"):
        v = prepare_inputs(flow([".mp4"]), run, {"f": ref})
        assert v["f"] == {"name": "clip.mp4", "path": "inputs/f/clip.mp4", "size": 100_000}
        assert (io_tool.run_dir(run) / "inputs/f/clip.mp4").read_bytes() == b"v" * 100_000
    assert c.put("/api/uploads", content=b"", headers=H).status_code == 422
    for bad in ("../../x", "0" * 32, 5):
        with pytest.raises(RunError) as e:
            prepare_inputs(flow([]), "r3", {"f": {"name": "a.mp4", "upload": bad}})
        assert "choose it again" in json.dumps(e.value.detail)


def test_upload_limit_leaves_nothing_and_old_files_go(tmp_path):
    async def chunks():
        for _ in range(4):
            yield b"x" * 10

    with pytest.raises(uploads.UploadError, match="larger"):
        asyncio.run(uploads.receive(chunks(), limit=25))
    assert list(uploads.uploads_dir().iterdir()) == []
    asyncio.run(uploads.receive(chunks()))
    uploads.clean(now=time.time() + uploads.KEEP_SECONDS + 60)
    assert list(uploads.uploads_dir().iterdir()) == []


def test_the_command_line_stages_large_files(tmp_path, monkeypatch):
    f = tmp_path / "clip.mp4"
    f.write_bytes(b"v" * 100)
    assert "content_b64" in cli.parse_sets([f"f=@{f}"])["f"]
    monkeypatch.setattr(cli, "STAGE_OVER", 10)
    v = cli.parse_sets([f"f=@{f}"])["f"]
    assert set(v) == {"name", "upload"}
    assert prepare_inputs(flow([]), "r1", {"f": v})["f"]["size"] == 100

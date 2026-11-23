"""Images and video frames for the agents (used by the `io` tools `view_image` and `view_video`).

The format comes from the first bytes of the file, never from its name, and it is forced on ffmpeg
(`-f`) with only the `file` and `pipe` protocols: a playlist renamed to .mp4 cannot make ffmpeg open
other files or the network. Images go to the model as they are when they fit, else scaled down.
ffmpeg (on PATH, or the `media` extra) is needed for videos and to scale images; small images need
nothing. Videos have no sound here: only frames.
"""
from __future__ import annotations

import asyncio
import base64
import contextlib
import re
import shutil
import struct
from pathlib import Path
from typing import Any

MAX_EDGE = 1568  # longest side of an image the model sees at full detail
FRAME_EDGE = 768  # video frames: enough to see what happens, and cheap
MAX_IMAGE_BYTES = 3_500_000  # the API takes 5 MB of base64 per image
MAX_FRAMES = 12
DEFAULT_FRAMES = 6
TIMEOUT = 30.0

IMAGE_EXTS = frozenset({".png", ".jpg", ".jpeg", ".gif", ".webp"})
VIDEO_EXTS = frozenset({".mp4", ".m4v", ".mov", ".webm", ".mkv", ".avi"})
_PIPE = {"image/png": "png_pipe", "image/jpeg": "jpeg_pipe", "image/webp": "webp_pipe",
         "image/gif": "gif"}
NO_FFMPEG = ('ffmpeg is not installed on this machine: install it, or run '
             'pip install -e ".[media]", and restart the studio')


class MediaError(ValueError):
    pass


def image_type(head: bytes) -> str | None:
    """The image media type from the first bytes (PNG, JPEG, GIF, WebP), or None."""
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if head.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if head[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "image/webp"
    return None


def video_format(head: bytes) -> str | None:
    """The ffmpeg input format of a video from its first bytes, or None."""
    if head[4:8] in (b"ftyp", b"moov", b"mdat", b"wide"):
        return "mov"
    if head.startswith(b"\x1a\x45\xdf\xa3"):
        return "matroska"  # .mkv and .webm
    if head[:4] == b"RIFF" and head[8:12] == b"AVI ":
        return "avi"
    return None


def image_size(data: bytes) -> tuple[int, int] | None:
    """(width, height) from the headers, or None when they cannot be read."""
    with contextlib.suppress(struct.error, IndexError):
        kind = image_type(data[:16])
        if kind == "image/png":
            return struct.unpack(">II", data[16:24])
        if kind == "image/gif":
            return struct.unpack("<HH", data[6:10])
        if kind == "image/webp":
            if data[12:16] == b"VP8X":
                return (1 + int.from_bytes(data[24:27], "little"),
                        1 + int.from_bytes(data[27:30], "little"))
            if data[12:16] == b"VP8 ":
                w, h = struct.unpack("<HH", data[26:30])
                return w & 0x3FFF, h & 0x3FFF
            if data[12:16] == b"VP8L":
                bits = int.from_bytes(data[21:25], "little")
                return 1 + (bits & 0x3FFF), 1 + ((bits >> 14) & 0x3FFF)
        if kind == "image/jpeg":
            i = 2
            while i + 9 < len(data):
                if data[i] != 0xFF:
                    i += 1
                    continue
                marker = data[i + 1]
                if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):
                    h, w = struct.unpack(">HH", data[i + 5:i + 9])
                    return w, h
                if marker == 0xFF or 0xD0 <= marker <= 0xD9 or marker == 0x01:
                    i += 1 if marker == 0xFF else 2
                    continue
                i += 2 + struct.unpack(">H", data[i + 2:i + 4])[0]
    return None


def ffmpeg_exe() -> str | None:
    found = shutil.which("ffmpeg")
    if found:
        return found
    try:
        import imageio_ffmpeg  # the optional `media` extra
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:  # noqa: BLE001 — not installed, or no binary for this platform
        return None


async def _ffmpeg(*args: str) -> tuple[int, bytes, str]:
    exe = ffmpeg_exe()
    if exe is None:
        raise MediaError(NO_FFMPEG)
    proc = await asyncio.create_subprocess_exec(
        exe, "-hide_banner", "-nostdin", *args, stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    try:
        out, errb = await asyncio.wait_for(proc.communicate(), timeout=TIMEOUT)
    except asyncio.TimeoutError:  # TimeoutError on Python 3.11+
        raise MediaError(f"ffmpeg took longer than {TIMEOUT:.0f}s") from None
    finally:  # also when the run is stopped: nothing is left running
        if proc.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                proc.kill()
    return proc.returncode or 0, out, errb.decode("utf-8", "replace")


def _input(fmt: str, path: Path) -> list[str]:
    return ["-protocol_whitelist", "file,pipe", "-f", fmt, "-i", str(path)]


def _scale(edge: int) -> str:
    return (f"scale=w='min({edge},iw)':h='min({edge},ih)':force_original_aspect_ratio=decrease")


def _block(data: bytes, media: str) -> dict[str, Any]:
    return {"type": "image", "data": base64.b64encode(data).decode(), "mimeType": media}


def _kind(path: Path) -> bytes:
    with path.open("rb") as f:
        return f.read(16)


async def image_blocks(path: Path) -> list[dict[str, Any]]:
    """The image as MCP content: a line about it and the image (scaled down when it is large)."""
    media = image_type(_kind(path))
    if media is None:
        if video_format(_kind(path)):
            raise MediaError(f"{path.name} is a video: use view_video")
        raise MediaError(f"{path.name} is not a PNG, JPEG, GIF or WebP image")
    size_bytes = path.stat().st_size
    data = path.read_bytes() if size_bytes <= MAX_IMAGE_BYTES else b""
    dims = image_size(data) if data else None
    if dims and max(dims) <= MAX_EDGE:
        return [{"type": "text", "text": f"{path.name}: {dims[0]}x{dims[1]}"},
                _block(data, media)]
    last = ""
    # lossless first (text in screenshots); a photo that stays too big goes as JPEG
    codecs = [("mjpeg", "image/jpeg")] if media == "image/jpeg" else [
        ("png", "image/png"), ("mjpeg", "image/jpeg")]
    for codec, out_media in codecs:
        rc, out, last = await _ffmpeg(
            "-v", "error", *_input(_PIPE[media], path), "-frames:v", "1", "-vf",
            _scale(MAX_EDGE), "-c:v", codec, *(["-q:v", "3"] if codec == "mjpeg" else []),
            "-f", "image2pipe", "pipe:1")
        if rc == 0 and out and len(out) <= MAX_IMAGE_BYTES:
            new = image_size(out)
            was = f" (scaled from {dims[0]}x{dims[1]})" if dims else " (scaled down)"
            shape = f"{new[0]}x{new[1]}" if new else "image"
            return [{"type": "text", "text": f"{path.name}: {shape}{was}"},
                    _block(out, out_media)]
    raise MediaError(f"{path.name}: the image cannot be read ({last.strip()[-200:] or 'empty'})")


async def video_info(path: Path) -> dict[str, Any]:
    """{format, duration, width, height, audio} of a video."""
    fmt = video_format(_kind(path))
    if fmt is None:
        if image_type(_kind(path)):
            raise MediaError(f"{path.name} is an image: use view_image")
        raise MediaError(f"{path.name} is not a video (MP4, MOV, WebM, MKV or AVI)")
    _, _, text = await _ffmpeg(*_input(fmt, path))  # no output: prints the streams and stops
    dur = re.search(r"Duration: (\d+):(\d+):(\d+(?:\.\d+)?)", text)
    vid = re.search(r"Stream #.*?: Video: .*?, (\d{2,5})x(\d{2,5})", text)
    if not vid:
        raise MediaError(f"{path.name}: no video in this file")
    if not dur:
        raise MediaError(f"{path.name}: the length of the video is unknown")
    h, m, s = dur.groups()
    return {"format": fmt, "duration": int(h) * 3600 + int(m) * 60 + float(s),
            "width": int(vid.group(1)), "height": int(vid.group(2)),
            "audio": bool(re.search(r"Stream #.*?: Audio:", text))}


async def video_blocks(path: Path, start: float = 0.0, end: float | None = None,
                       frames: int = DEFAULT_FRAMES) -> list[dict[str, Any]]:
    """Frames of a video as MCP content, evenly spread between `start` and `end` (seconds), each
    with its time."""
    info = await video_info(path)
    length = info["duration"]
    stop = length if end is None else min(float(end), length)
    if not 0 <= start < stop:
        raise MediaError(f"start and end must be inside the video (0 to {length:.1f} s)")
    n = max(1, min(int(frames), MAX_FRAMES))
    step = (stop - start) / n
    shown: list[dict[str, Any]] = []
    last = ""
    for i in range(n):
        t = start + (i + 0.5) * step  # the middle of each part: never past the last frame
        rc, out, last = await _ffmpeg(
            "-v", "error", "-ss", f"{t:.3f}", *_input(info["format"], path), "-frames:v", "1",
            "-vf", _scale(FRAME_EDGE), "-c:v", "mjpeg", "-q:v", "4", "-f", "image2pipe", "pipe:1")
        if rc == 0 and out:
            shown += [{"type": "text", "text": f"t={t:.2f}s"}, _block(out, "image/jpeg")]
    if not shown:
        raise MediaError(f"{path.name}: no frame could be read ({last.strip()[-200:] or 'empty'})")
    sound = "it has sound, which you cannot hear" if info["audio"] else "no sound"
    head = (f"{path.name}: {length:.1f} s, {info['width']}x{info['height']}, {sound}. "
            f"{len(shown) // 2} frames between {start:.1f} and {stop:.1f} s:")
    return [{"type": "text", "text": head}, *shown]

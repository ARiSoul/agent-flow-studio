"""Large files for a run (a video…): the editor streams each one to disk (`PUT /api/uploads`) and
the run takes it by its id, so a file never sits in memory as base64. Kept in `runs/.uploads/` and
removed after a day; a run gets its own copy in `runs/<id>/inputs/`.
"""
from __future__ import annotations

import os
import re
import shutil
import time
import uuid
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

from ..config import settings

MAX_FILE = 1024 * 1024 * 1024  # one staged file
KEEP_SECONDS = 24 * 3600
_ID = re.compile(r"[0-9a-f]{32}")


class UploadError(ValueError):
    pass


def uploads_dir() -> Path:
    return settings.runs_dir / ".uploads"


def clean(now: float | None = None) -> None:
    """Removes staged files older than a day."""
    if not uploads_dir().is_dir():
        return
    limit = (now or time.time()) - KEEP_SECONDS
    for f in uploads_dir().iterdir():
        try:
            if f.is_file() and f.stat().st_mtime < limit:
                f.unlink()
        except OSError:
            continue  # in use: the next upload tries again


def _new() -> tuple[str, Path]:
    clean()
    uploads_dir().mkdir(parents=True, exist_ok=True)
    uid = uuid.uuid4().hex
    return uid, uploads_dir() / uid


async def receive(chunks: AsyncIterator[bytes], limit: int = MAX_FILE) -> dict[str, Any]:
    """Streams a file to disk; {upload, size}. Nothing is kept when it fails or is too large."""
    uid, path = _new()
    size = 0
    try:
        with path.open("wb") as f:
            async for chunk in chunks:
                size += len(chunk)
                if size > limit:
                    raise UploadError(f"file larger than {limit // (1024 * 1024)} MB")
                f.write(chunk)
        if not size:
            raise UploadError("empty file")
    except BaseException:  # also a closed connection
        path.unlink(missing_ok=True)
        raise
    return {"upload": uid, "size": size}


def stage(source: Path) -> dict[str, Any]:
    """A local file the person named on the command line; {upload, size}."""
    if source.stat().st_size > MAX_FILE:
        raise UploadError(f"file larger than {MAX_FILE // (1024 * 1024)} MB")
    uid, path = _new()
    shutil.copyfile(source, path)
    return {"upload": uid, "size": path.stat().st_size}


def take(uid: Any, target: Path) -> None:
    """Puts a staged file in a run. It stays staged, so the same form can run again."""
    path = uploads_dir() / uid if isinstance(uid, str) and _ID.fullmatch(uid) else None
    if path is None or not path.is_file():
        raise UploadError("the uploaded file is gone: choose it again")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.unlink(missing_ok=True)
    try:
        os.link(path, target)  # no second copy on the same disk
    except OSError:
        shutil.copyfile(path, target)

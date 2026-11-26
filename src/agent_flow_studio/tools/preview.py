"""`preview.screenshot`: an agent looks at the workspace's web app (a game with three.js, a site
under development) as a person would — a headless browser opens the workspace's preview URL
(workspace.yaml `preview.url`, local only), waits, takes a PNG and collects the page's console
errors. The PNG is saved in the run (`runs/<id>/files/<agent>/`), shown to the agent (an image
block) and can become a `file` output the person sees in the Run panel.

Read only: the page stays on its own origin (other requests are blocked), nothing is clicked or
typed, nothing outside the run folder is written. Needs Playwright (`pip install -e ".[web]"`) and
uses the installed Chrome or Edge (WebGL works headless).
"""
from __future__ import annotations

import re
import time
from typing import Any
from urllib.parse import urljoin, urlparse

from claude_agent_sdk import tool

from .. import workspaces
from . import io as io_tool
from . import media
from .common import err, ok

LOCAL = re.compile(r"^https?://(localhost|127\.0\.0\.1|\[::1\])(:\d{1,5})?(/.*)?$")
MAX_WAIT_MS = 20_000
_PATH = re.compile(r"^[/?#][^\s]{0,500}$")


def preview_url(path: str = "") -> str:
    """The workspace's preview URL, with `path` ("/level/2", "?species=ladybug") on it."""
    spec = workspaces.current().spec
    base = spec.preview.url if spec and spec.preview else None
    if not base:
        raise ValueError("this workspace has no preview URL (workspace.yaml: preview.url)")
    if not path:
        return base
    if not _PATH.fullmatch(path) or "://" in path:
        raise ValueError("path must start with /, ? or # and stay on the preview site")
    url = urljoin(base, path)
    if urlparse(url).netloc != urlparse(base).netloc:
        raise ValueError("path must stay on the preview site")
    return url


async def take(url: str, out: Any, *, wait_ms: int = 1500, selector: str = "",
               width: int = 1280, height: int = 800) -> list[str]:
    """Opens `url` headless, waits, writes a PNG to `out`; returns the console errors."""
    try:
        from playwright.async_api import async_playwright  # optional dependency
    except ImportError as e:
        raise ValueError('Playwright is not installed: pip install -e ".[web]"') from e
    origin = urlparse(url).netloc
    errors: list[str] = []
    async with async_playwright() as pw:
        browser, tried = None, []
        for channel in ("chrome", "msedge", None):  # the installed browsers first
            try:
                browser = await pw.chromium.launch(
                    headless=True, **({"channel": channel} if channel else {}),
                    args=["--use-angle=swiftshader", "--enable-unsafe-swiftshader"])
                break
            except Exception as e:  # noqa: BLE001 — that browser is not installed: try the next one
                tried.append(f"{channel or 'chromium'}: {str(e).splitlines()[0][:120]}")
        if browser is None:
            raise ValueError("no Chrome or Edge found for the preview (" + "; ".join(tried) + ")")
        try:
            page = await browser.new_page(viewport={"width": width, "height": height})
            page.on("console", lambda m: errors.append(m.text[:500]) if m.type == "error" else None)
            page.on("pageerror", lambda e: errors.append(str(e)[:500]))

            async def same_site(route: Any) -> None:
                u = route.request.url
                if u.startswith(("data:", "blob:")) or urlparse(u).netloc == origin:
                    await route.continue_()
                else:
                    await route.abort()

            await page.route("**/*", same_site)
            await page.goto(url, wait_until="load", timeout=MAX_WAIT_MS)
            if selector:
                await page.wait_for_selector(selector, timeout=MAX_WAIT_MS)
            await page.wait_for_timeout(min(max(wait_ms, 0), MAX_WAIT_MS))
            await page.screenshot(path=str(out), type="png")
        finally:
            await browser.close()
    return errors[:50]


@tool("screenshot", "Looks at the workspace's web app (its preview URL, e.g. the game running "
      "locally): opens it in a browser, waits, and shows you a screenshot, with the page's console "
      "errors. 'path' adds a page or a query to the preview URL (e.g. '?species=ladybug'); "
      "'selector' waits for an element; 'wait_ms' waits more for animations or WebGL. The PNG is "
      "saved in the run: put its path in a file output for the person.",
      {"type": "object", "properties": {
          "run_id": {"type": "string"}, "agent": {"type": "string"},
          "path": {"type": "string"}, "selector": {"type": "string"},
          "wait_ms": {"type": "integer", "minimum": 0, "maximum": MAX_WAIT_MS},
          "width": {"type": "integer", "minimum": 320, "maximum": 2560},
          "height": {"type": "integer", "minimum": 240, "maximum": 1600}},
       "required": ["run_id", "agent"]})
async def screenshot(args: dict[str, Any]) -> dict[str, Any]:
    try:
        url = preview_url(str(args.get("path") or ""))
        agent = str(args["agent"])
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", agent):
            raise ValueError("bad agent name")
        folder = io_tool.run_dir(str(args["run_id"])) / "files" / agent
        folder.mkdir(parents=True, exist_ok=True)
        out = folder / f"screenshot-{time.strftime('%H%M%S')}-{int(time.time() * 1000) % 1000:03d}.png"
        errors = await take(url, out, wait_ms=int(args.get("wait_ms") or 1500),
                            selector=str(args.get("selector") or ""),
                            width=int(args.get("width") or 1280), height=int(args.get("height") or 800))
        rel = out.relative_to(io_tool.run_dir(str(args["run_id"]))).as_posix()
        info = ok({"path": rel, "url": url, "console_errors": errors})
        return {"content": [*await media.image_blocks(out), *info["content"]]}
    except Exception as e:  # noqa: BLE001 — the agent gets the reason (no server, a timeout…)
        return err(f"screenshot failed: {type(e).__name__}: {e}"[:600])

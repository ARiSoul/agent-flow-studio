"""Website connectors: a real browser with YOUR sign-in, driven only by declared action steps.

- The session lives in a browser profile on this machine (`~/.agent-flow-studio/browser/<name>`,
  or `AFS_BROWSER_DIR`), never in git or connections.yaml. The studio never types passwords:
  "Sign in" opens a visible window where the person signs in (MFA, CAPTCHA included) and closes it.
- An action runs its steps (goto, click, fill, select, press, wait_for, extract…) headless. The
  model only chooses the action and its parameters; it never clicks freely.
- Read actions block every request that is not GET/HEAD (unless the action has `allow_post`), so
  they cannot change anything on the site; write actions go through the flow autonomy (policy).
- Navigation must stay on the site (base URL origin) and inside its allowed paths.
- Playwright is an optional dependency (`pip install -e ".[web]"`); it uses the installed Edge or
  Chrome (`web.browser`), so no browser download is needed.
"""
from __future__ import annotations

import asyncio
import contextlib
import html as html_mod
import json
import os
import re
import shutil
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import urlsplit

from .actions import ActionError, fill
from .model import Action, Connection

MAX_TEXT = 20_000
MAX_ROWS = 500
SIGN_IN_TIMEOUT_S = 600
_locks: dict[str, asyncio.Lock] = {}  # one browser per profile at a time


class WebError(ActionError):
    def __init__(self, *args: object, blocked: list[str] | None = None):
        super().__init__(*args)
        self.blocked = blocked or []  # requests a read action refused (e.g. POST /api/x)


class SignedOut(WebError):
    pass


def profiles_dir() -> Path:
    return Path(os.getenv("AFS_BROWSER_DIR", Path.home() / ".agent-flow-studio" / "browser"))


def profile_dir(name: str) -> Path:
    """The browser profile of a website connector; a workspace's own connectors have their own
    (two workspaces can be signed in as different people under the same name)."""
    from .. import workspaces
    from . import store
    if not workspaces.current().studio and store.location(name) == "workspace":
        return workspaces.scoped(profiles_dir()) / name
    return profiles_dir() / name


def has_profile(name: str) -> bool:
    return profile_dir(name).is_dir()


def forget_session(name: str) -> None:
    """Sign out: deletes the browser profile (cookies, storage) of this connector."""
    shutil.rmtree(profile_dir(name), ignore_errors=True)


def _origin(url: str) -> str:
    p = urlsplit(url)
    return f"{p.scheme}://{p.netloc}".lower()


def _path_allowed(conn: Connection, url: str) -> bool:
    if _origin(url) != _origin(conn.resolved_base_url()):
        return False
    base_path = urlsplit(conn.resolved_base_url()).path.rstrip("/")
    path = urlsplit(url).path
    rel = path.removeprefix(base_path)
    return not conn.allowed_paths or any(rel.startswith(p) for p in conn.allowed_paths)


class Driver(Protocol):
    """What the steps need from a browser page (Playwright in production, a fake in tests)."""
    async def goto(self, url: str) -> None: ...
    async def click(self, selector: str) -> None: ...
    async def fill(self, selector: str, value: str) -> None: ...
    async def select(self, selector: str, value: str) -> None: ...
    async def press(self, selector: str, key: str) -> None: ...
    async def wait_for(self, selector: str) -> None: ...
    async def exists(self, selector: str) -> bool: ...
    async def text(self, selector: str) -> str: ...
    async def texts(self, selector: str) -> list[str]: ...
    async def table(self, selector: str) -> list[list[str]]: ...
    async def get(self, url: str) -> tuple[int, str, str, str]: ...  # status, final URL, type, text
    def url(self) -> str: ...


def _clip(s: str) -> str:
    s = s.strip()
    return s if len(s) <= MAX_TEXT else s[:MAX_TEXT] + "…"


# --- get_json: the site's own JSON, only the listed fields --------------------------------------
_TAG = re.compile(r"<[^>]+>")
_BLOCK = re.compile(r"<\s*(br|/p|/div|/li|/tr|/h\d)\b[^>]*>", re.IGNORECASE)
_ENTITY = re.compile(r"&(#\d+|#x[0-9a-fA-F]+|[A-Za-z]+);")


def _text(v: Any) -> Any:
    """Strings that look like HTML become plain text (rich-text notes); the rest stays."""
    if isinstance(v, str):
        if _TAG.search(v) or _ENTITY.search(v):
            v = html_mod.unescape(_TAG.sub("", _BLOCK.sub("\n", v)))
            v = re.sub(r"\n\s*\n+", "\n\n", v)
        return _clip(v)
    return v


def parse_json(text: str) -> Any:
    """JSON, or JSONP (`callback({...});`) as some sites answer."""
    t = text.strip()
    m = re.match(r"^[A-Za-z_$][\w$.]*\((.*)\)\s*;?\s*$", t, re.DOTALL)
    try:
        return json.loads(m.group(1) if m else t)
    except ValueError:
        raise WebError("the site did not answer with JSON (is the session still signed in?)") from None


def _segments(path: str) -> list[str]:
    out, cur, depth = [], "", 0
    for ch in path:
        if ch == "." and depth == 0:
            out.append(cur)
            cur = ""
            continue
        depth += ch == "{"
        depth -= ch == "}"
        cur += ch
    return [*out, cur]


def pick(data: Any, path: str) -> Any:
    """`a.b` · `list[].x` (a list) · `list[].{x,y.z}` (a list of small objects); missing = None."""
    def walk(node: Any, segs: list[str]) -> Any:
        if not segs:
            return _text(node)
        s, rest = segs[0], segs[1:]
        if s.startswith("{"):
            return {k: walk(node, _segments(k)) for k in s[1:-1].split(",") if k}
        many = s.endswith("[]")
        key = s[:-2] if many else s
        val = node.get(key) if isinstance(node, dict) else None
        if many:
            return [walk(x, rest) for x in val[:MAX_ROWS]] if isinstance(val, list) else []
        return walk(val, rest) if val is not None else None
    return walk(data, _segments(path))


async def _check_session(conn: Connection, d: Driver) -> None:
    w = conn.web
    if w.signed_out_url and w.signed_out_url in d.url():
        raise SignedOut("the site asks to sign in")
    if w.signed_in_selector and not await d.exists(w.signed_in_selector):
        raise SignedOut("the signed-in element was not found")


def step_text(n: int, kind: str, arg: Any) -> str:
    """"step 5 (extract span.count)": which step, for an error message."""
    if isinstance(arg, dict):
        arg = arg.get("selector") or arg.get("path") or ", ".join(map(str, arg.values()))
    text = " ".join(str(arg).split())
    return f"step {n} ({kind} {text[:80]})"


async def run_steps(conn: Connection, action: Action, values: dict[str, Any],
                    d: Driver, progress: dict[str, Any] | None = None) -> dict[str, Any]:
    """Runs the steps on a page; returns the extracted values and the final URL. `progress`
    gets the step being run (for the error message when a step fails)."""
    base = conn.resolved_base_url()
    out: dict[str, Any] = {}
    checked = navigated = False
    for n, st in enumerate(action.steps, 1):
        (kind, arg), = st.items()
        if progress is not None:
            progress["step"] = step_text(n, kind, arg)
        if kind == "get_json":
            url = base + fill(str(arg["path"]), values, encode=True)
            if not _path_allowed(conn, url):
                raise WebError(f"path not allowed: {urlsplit(url).path}")
            status, final, ctype, text = await d.get(url)
            w = conn.web
            if status in (401, 403) or _origin(final) != _origin(base) or (
                    w.signed_out_url and w.signed_out_url in final) or "text/html" in ctype.lower():
                raise SignedOut("the site answered with its sign-in page, not with data")
            if status >= 400:
                raise WebError(f"the site answered HTTP {status} for {urlsplit(url).path}")
            data = parse_json(text)
            for k, p in arg["fields"].items():
                out[k] = pick(data, p)
            checked = True  # data came back: the session is signed in
            continue
        navigated = True
        if kind == "goto":
            url = base + fill(arg, values, encode=True)
            if not _path_allowed(conn, url):
                raise WebError(f"path not allowed: {urlsplit(url).path}")
            await d.goto(url)
        elif kind == "click":
            await d.click(fill(arg, values))
        elif kind == "wait_for":
            await d.wait_for(fill(arg, values))
        elif kind == "fill":
            await d.fill(fill(arg["selector"], values), fill(str(arg["value"]), values))
        elif kind == "select":
            await d.select(fill(arg["selector"], values), fill(str(arg["value"]), values))
        elif kind == "press":
            await d.press(fill(arg["selector"], values), str(arg["key"]))
        elif kind == "extract":
            for k, sel in arg.items():
                out[k] = _clip(await d.text(fill(sel, values)))
        elif kind == "extract_all":
            for k, sel in arg.items():
                out[k] = [_clip(x) for x in (await d.texts(fill(sel, values)))[:MAX_ROWS]]
        elif kind == "extract_table":
            out["table"] = [[_clip(c) for c in r] for r in (await d.table(fill(arg, values)))[:MAX_ROWS]]
        if kind == "goto" and _origin(d.url()) != _origin(base):  # e.g. sent to the SSO page
            raise SignedOut(f"the site sent the browser to {_origin(d.url())} (probably to sign in)")
        if not checked and kind == "goto":  # after the first page: still signed in?
            await _check_session(conn, d)
            checked = True
        if not _path_allowed(conn, d.url()):  # a click or a redirect left the site
            raise WebError(f"the page left the allowed site/paths: {_origin(d.url())}"
                           f"{urlsplit(d.url()).path}")
    if not checked:
        await _check_session(conn, d)
    return {"values": out, "url": d.url() if navigated else None}


# --- Playwright --------------------------------------------------------------------------------
def _playwright():
    try:
        from playwright.async_api import async_playwright  # optional dependency
    except ImportError:
        raise WebError("website connectors need Playwright: pip install -e \".[web]\" "
                       "(it uses the installed Edge/Chrome)") from None
    return async_playwright


# `tag:text-is("…")` (the recorder's selector for a button, link or list item by its text)
_TEXT_IS = re.compile(r'^([a-z][a-z0-9]*):text-is\(("(?:[^"\\]|\\.)*")\)$')
# the index of the first visible element whose VISIBLE text (innerText, as the recorder reads it)
# is exactly the text; :text-is also counts hidden text inside (a hidden badge, a counter "0")
_FIND_BY_TEXT = """(els, t) => els.findIndex(e => {
  const visible = e.checkVisibility ? e.checkVisibility() : e.getClientRects().length > 0;
  return visible && (e.innerText || "").trim().replace(/\\s+/g, " ") === t;
})"""


class _PwDriver:
    def __init__(self, page: Any, timeout_ms: int):
        self.p, self.t = page, timeout_ms

    def _one(self, selector: str) -> Any:
        """The first VISIBLE match: many pages keep a hidden copy of a menu (e.g. the phone
        layout) before the visible one, and the first match would wait for it forever."""
        return self.p.locator(selector).filter(visible=True).first

    async def _find(self, selector: str) -> Any:
        """The element of a step. A `tag:text-is("…")` selector also finds an element whose
        visible text is exactly that text (its full text may hold hidden parts); the page may
        draw it later, so this waits up to the timeout."""
        loc = self._one(selector)
        m = _TEXT_IS.fullmatch(selector)
        if not m:
            return loc
        tag, text = m.group(1), json.loads(m.group(2))
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self.t / 1000
        while True:
            if await loc.count():
                return loc
            i = await self.p.locator(tag).evaluate_all(_FIND_BY_TEXT, text)
            if i >= 0:
                return self.p.locator(tag).nth(i)
            if loop.time() >= deadline:
                return loc  # the action waits a little more and fails with Playwright's reason
            await asyncio.sleep(0.25)

    async def goto(self, url: str) -> None:
        await self.p.goto(url, timeout=self.t, wait_until="domcontentloaded")

    async def click(self, selector: str) -> None:
        await (await self._find(selector)).click(timeout=self.t)

    async def fill(self, selector: str, value: str) -> None:
        await (await self._find(selector)).fill(value, timeout=self.t)

    async def select(self, selector: str, value: str) -> None:
        await (await self._find(selector)).select_option(value, timeout=self.t)

    async def press(self, selector: str, key: str) -> None:
        await (await self._find(selector)).press(key, timeout=self.t)

    async def wait_for(self, selector: str) -> None:
        await (await self._find(selector)).wait_for(timeout=self.t)

    async def exists(self, selector: str) -> bool:
        try:
            await self._one(selector).wait_for(timeout=min(self.t, 5000))
            return True
        except Exception:  # noqa: BLE001 — Playwright's TimeoutError: not there
            return False

    async def text(self, selector: str) -> str:
        return await (await self._find(selector)).inner_text(timeout=self.t)

    async def texts(self, selector: str) -> list[str]:
        return await self.p.locator(selector).all_inner_texts()

    async def table(self, selector: str) -> list[list[str]]:
        return await self.p.locator(selector).first.evaluate(
            "t => [...t.querySelectorAll('tr')].map(r => [...r.querySelectorAll('th,td')]"
            ".map(c => c.innerText))", timeout=self.t)

    async def get(self, url: str) -> tuple[int, str, str, str]:
        # the browser itself opens the JSON address (one GET with the session cookies and the
        # system certificates, e.g. a company CA); a JSON answer runs no site code
        r = await self.p.goto(url, timeout=self.t, wait_until="domcontentloaded")
        if r is None:
            return 0, self.p.url, "", ""
        return r.status, self.p.url, r.headers.get("content-type", ""), await r.text()

    def url(self) -> str:
        return self.p.url


_PW_REASONS = ("resolved to", "not visible", "not enabled", "not stable", "intercepts pointer",
               "outside of the viewport", "detached", "not attached")


def _pw_error(e: Exception) -> str:
    """A Playwright error in one line: its first line and the reasons in its call log (which
    element it found and why it waited), without the retries."""
    lines = [x.strip(" -") for x in str(e).splitlines() if x.strip()]
    reasons: list[str] = []
    for x in lines[1:]:
        x = re.sub(r"^\d+\s*\W\s*", "", x)  # "2 × waiting…" → "waiting…"
        if any(r in x for r in _PW_REASONS) and x not in reasons:
            reasons.append(x)
    return " · ".join([lines[0] if lines else "", *reasons[:3]])[:600]


def _launch_args(conn: Connection, headless: bool) -> dict[str, Any]:
    args: dict[str, Any] = {"headless": headless, "accept_downloads": False}
    if not headless:  # a visible window: the page follows the window size (no fixed 1280x720)
        args["no_viewport"] = True
    if conn.web.browser != "chromium":
        args["channel"] = conn.web.browser
    return args


VISIBLE_SLOW_MO_MS = 300  # a test in a visible window: slow enough to follow each step
VISIBLE_LOOK_S = 20  # after a failed visible test, the window stays open so the person can look


async def run_action(name: str, conn: Connection, action_name: str, action: Action,
                     values: dict[str, Any], *, visible: bool = False) -> dict[str, Any]:
    """Runs an action headless; `visible`: in a window the person can watch (a test from the
    Connectors window)."""
    if not has_profile(name):
        raise SignedOut(f"connector {name!r} is not signed in: Connectors → {name} → Sign in")
    lock = _locks.setdefault(name, asyncio.Lock())
    blocked: list[str] = []  # requests a read action refused (they may be how the page loads data)
    progress: dict[str, Any] = {}
    args = _launch_args(conn, headless=not visible)
    if visible:
        args["slow_mo"] = VISIBLE_SLOW_MO_MS
    if action.locale:  # the language it was recorded in: steps find links by their texts
        args["locale"] = action.locale
    if action.viewport:  # the layout it was recorded at (menus and links change with the size)
        args.pop("no_viewport", None)
        args["viewport"] = {"width": action.viewport.width, "height": action.viewport.height}
    async with lock, _playwright()() as pw:
        ctx = await pw.chromium.launch_persistent_context(str(profile_dir(name)), **args)
        try:
            await _restore_state(name, ctx)  # the session cookies kept at sign-in
            if action.access == "read" and not action.allow_post:
                async def guard(route: Any) -> None:
                    if route.request.method in ("GET", "HEAD"):
                        await route.continue_()
                    else:  # a read action cannot change anything on the site
                        what = f"{route.request.method} {urlsplit(route.request.url).path}"
                        if what not in blocked and len(blocked) < 5:
                            blocked.append(what)
                        await route.abort()
                await ctx.route("**/*", guard)
            page = ctx.pages[0] if ctx.pages else await ctx.new_page()
            try:
                r = await run_steps(conn, action, values,
                                    _PwDriver(page, conn.web.timeout_s * 1000), progress)
            except SignedOut as e:
                raise SignedOut(f"{name}: {e}: sign in again (Connectors → {name} → Sign in)") from None
            except Exception as e:  # noqa: BLE001 — say which step, where, and what was blocked
                size = ""
                with contextlib.suppress(Exception):
                    w, h = await page.evaluate("[innerWidth, innerHeight]")
                    size = f", window {w}x{h}"
                where = f"{progress.get('step', 'a step')} on {urlsplit(page.url).path or '/'}{size}"
                text = str(e) if isinstance(e, WebError) else f"{type(e).__name__}: {_pw_error(e)}"
                hint = ""
                if blocked:
                    hint = (f" · the read action blocked {', '.join(blocked)}: if the page loads its "
                            "data this way, add allow_post: true to the action")
                elif "Timeout" in text and not action.viewport:
                    hint = (" · the element never appeared: if the site changes its layout with the "
                            "window size, record the action again (it keeps the window size) or "
                            "set viewport: {width, height}")
                if visible:  # let the person see the page where it stopped
                    with contextlib.suppress(Exception):
                        await page.wait_for_event("close", timeout=VISIBLE_LOOK_S * 1000)
                raise WebError(f"{name}.{action_name}: {where}: {text}{hint}",
                               blocked=blocked) from None
            _save_state(name, await ctx.storage_state())  # the site may renew its cookies
        finally:
            with contextlib.suppress(Exception):
                await ctx.close()
    out = {"action": action_name, **r}
    if blocked:
        out["blocked"] = blocked
    return out


async def sign_in(name: str, conn: Connection) -> dict[str, Any]:
    """Opens a visible browser window on the login page; the person signs in and closes the
    window (or it closes after 10 minutes). The session stays in the connector's profile."""
    lock = _locks.setdefault(name, asyncio.Lock())
    url = conn.web.login_url or "/"
    url = url if url.startswith("https://") else conn.resolved_base_url() + url
    async with lock, _playwright()() as pw:
        profile_dir(name).mkdir(parents=True, exist_ok=True)
        ctx = await pw.chromium.launch_persistent_context(str(profile_dir(name)),
                                                          **_launch_args(conn, headless=False))
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()
        closed = asyncio.Event()
        ctx.on("close", lambda *_: closed.set())
        page.on("close", lambda *_: closed.set() if not ctx.pages else None)
        await page.goto(url)
        # session cookies (no expiry, e.g. JSESSIONID) are dropped when the browser closes: keep a
        # copy of the session while the window is open, restored before each action
        deadline = asyncio.get_running_loop().time() + SIGN_IN_TIMEOUT_S
        while not closed.is_set() and asyncio.get_running_loop().time() < deadline:
            try:
                _save_state(name, await ctx.storage_state())
            except Exception:  # noqa: BLE001 — the window is closing
                break
            try:
                await asyncio.wait_for(closed.wait(), timeout=2)
            except asyncio.TimeoutError:
                pass
        if not closed.is_set():
            await ctx.close()
    return {"profile": True}


def _state_file(name: str) -> Path:
    return profile_dir(name) / "session-state.json"


def _save_state(name: str, state: dict[str, Any]) -> None:
    """Cookies of the signed-in session, kept next to the browser profile (never in git, never
    in logs or in what the agents see)."""
    f = _state_file(name)
    tmp = f.with_suffix(".tmp")
    tmp.write_text(json.dumps({"cookies": state.get("cookies", [])}), encoding="utf-8")
    os.replace(tmp, f)


async def _restore_state(name: str, ctx: Any) -> None:
    f = _state_file(name)
    if f.is_file():
        try:
            cookies = json.loads(f.read_text(encoding="utf-8")).get("cookies", [])
        except (OSError, ValueError):
            return
        if cookies:
            await ctx.add_cookies(cookies)


async def check_session(name: str, conn: Connection) -> dict[str, Any]:
    """Opens the site headless and says whether it still looks signed in."""
    if not has_profile(name):
        return {"ok": False, "detail": "not signed in yet"}
    if conn.web.check_path:  # a JSON GET: no page is opened
        probe = Action(description="session check", steps=[
            {"get_json": {"path": conn.web.check_path, "fields": {"answer": "id"}}}])
    else:
        start = conn.allowed_paths[0] if conn.allowed_paths else "/"
        probe = Action(description="session check", steps=[{"goto": start}])
    try:
        r = await run_action(name, conn, "session_check", probe, {})
    except SignedOut as e:
        return {"ok": False, "detail": str(e)}
    except WebError as e:
        return {"ok": False, "detail": str(e)}
    known = bool(conn.web.signed_in_selector or conn.web.signed_out_url)
    return {"ok": True, "detail": "signed in" if known else
            "the site opened (set 'signed-in element' to detect when the session expires)",
            "url": r["url"]}

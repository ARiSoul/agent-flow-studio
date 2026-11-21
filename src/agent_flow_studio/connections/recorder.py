"""Records the steps of a website action while the person clicks on the site.

"Record" opens a visible window with the connector's sign-in (like "Sign in"). The person works
on the site as usual; a small script in the page reports each click, typed field, chosen option
and Enter key, and Alt+click marks a text to extract. The result is a DRAFT action that the editor
shows; the person checks it and saves it with the other actions (the model never records).

- Each typed field becomes a {param} of the action (the agent fills it in), or, if the person
  chooses so in the editor, a fixed value (the text typed while recording). The typed text stays
  in memory and goes only to the local editor; it is written only as a chosen fixed value.
  Password fields are not recorded at all (their value never leaves the page).
- The steps go through the same checks as hand-written ones (`step_errors`, `Action`), and the
  action runs with the same guards (site, allowed paths, read actions cannot POST).
- The page script can be called by the site itself, so what it sends is only a suggestion: it is
  checked, limited in size and shown to the person before anything is saved.
"""
from __future__ import annotations

import asyncio
import json
import re
import time
import unicodedata
from typing import Any
from urllib.parse import urlsplit

import yaml

from ..messages import Msg, msg
from .model import ACTION_NAME, Action, Connection, step_errors
from .web import (
    _launch_args,
    _locks,
    _origin,
    _path_allowed,
    _playwright,
    _restore_state,
    _save_state,
    profile_dir,
)

RECORD_TIMEOUT_S = 1800
MAX_STEPS = 200
MAX_SELECTOR = 500
MAX_TYPED = 200  # a typed text kept for "fixed value"
_LOCALE = re.compile(r"^[a-z]{2,3}(-[A-Za-z0-9]{2,8}){0,2}$")
NAV_AFTER_EVENT_S = 2.5  # a navigation this soon after a click/Enter comes from it (not recorded)
NAV_AFTER_START_S = 4.0  # redirects of the first page


def _no_braces(path: str) -> str:
    """A recorded address keeps its text: braces would read as {param} placeholders."""
    return path.replace("{", "%7B").replace("}", "%7D")


def _slug(text: str, fallback: str) -> str:
    """A name from a field label: "Páginas, ações…" → paginas_acoes (accents dropped)."""
    plain = unicodedata.normalize("NFKD", str(text)).encode("ascii", "ignore").decode()
    s = re.sub(r"[^a-z0-9]+", "_", plain.lower()).strip("_")
    s = re.sub(r"^[^a-z]+", "", s)[:40].rstrip("_")
    return s if s and ACTION_NAME.fullmatch(s) else fallback


class Recording:
    """The steps recorded so far; the editor reads them while the person works."""

    def __init__(self, conn: Connection):
        self.conn = conn
        self.steps: list[dict[str, Any]] = []
        self.params: dict[str, dict[str, Any]] = {}
        self.warnings: list[Msg] = []
        self.error: str | None = None
        self.done = False
        self.stop = asyncio.Event()
        self.page: Any = None  # the recording page (tests drive it headless)
        self._t_start = 0.0
        self._t_event = 0.0
        self._fields: dict[str, str] = {}  # selector → param (one param per typed field)
        self.typed: dict[str, dict[str, str]] = {}  # param → {label, value} (memory only)
        self.posted = False  # the page sent POST/PUT… requests to the site while recording
        self.viewport: dict[str, int] | None = None  # the window size of the first step
        self.locale: str | None = None  # the browser language while recording (e.g. pt-PT)
        self._away = False  # on the sign-in page or outside the site: events are not recorded
        self._start = "/"

    # --- what the page reports ----------------------------------------------------------------
    def warn(self, m: Msg) -> None:
        if m not in self.warnings and len(self.warnings) < 20:
            self.warnings.append(m)

    def _push(self, step: dict[str, Any]) -> None:
        if len(self.steps) >= MAX_STEPS:
            self.warn(msg("web.record_limit", "more than {n} steps: the rest was not recorded",
                          n=MAX_STEPS))
            return
        self.steps.append(step)

    def begin(self, start: str) -> None:
        self._t_start = time.monotonic()
        self._start = start
        self._push({"goto": start})

    def _signing_in(self, url: str) -> bool:
        """The page is the site's sign-in page (web.login_url / web.signed_out_url)."""
        w = self.conn.web
        if w.signed_out_url and w.signed_out_url in url:
            return True
        if not w.login_url:
            return False
        if w.login_url.startswith("https://"):
            return url.startswith(w.login_url)
        base_path = urlsplit(self.conn.resolved_base_url()).path.rstrip("/")
        path = urlsplit(url).path.removeprefix(base_path)
        return _origin(url) == _origin(self.conn.resolved_base_url()) and \
            path.startswith(w.login_url.split("?")[0])

    def add(self, ev: Any) -> None:
        """One event from the page: {kind, selector, value?, key?, name?, secret?}."""
        if self.done or not isinstance(ev, dict) or self._away:
            return
        kind, sel = ev.get("kind"), ev.get("selector")
        if not isinstance(sel, str) or not sel.strip() or len(sel) > MAX_SELECTOR:
            return
        name = str(ev.get("name") or "")[:80]
        self._size(ev.get("vw"), ev.get("vh"))
        lang = ev.get("lang")
        if self.locale is None and isinstance(lang, str) and _LOCALE.fullmatch(lang):
            self.locale = lang
        if kind in ("click", "press", "open"):
            self._t_event = time.monotonic()
        if kind == "open":
            href = ev.get("href")
            rel = self._rel(href) if isinstance(href, str) else None
            if rel is None:  # not an address of the site: keep the click
                self._push({"click": sel})
            else:
                self._goto(rel)
        elif kind == "click":
            self._push({"click": sel})
        elif kind == "press":
            if ev.get("key") == "Enter":
                self._push({"press": {"selector": sel, "key": "Enter"}})
        elif kind == "fill":
            if ev.get("secret"):
                self.warn(msg("web.record_password", "a password field is never recorded"))
                return
            param = self._fields.get(sel)
            if param is None:
                base = _slug(name, "value")
                param, n = base, 2
                while param in self.params:
                    param, n = f"{base[:37]}_{n}", n + 1
                self._fields[sel] = param
                label = re.sub(r"\s+", " ", name).strip()[:100]
                self.params[param] = {"description": f"text for the field {label!r}" if label
                                      else "text for a field"}
            value = ev.get("value")
            if isinstance(value, str):
                self.typed[param] = {"label": re.sub(r"\s+", " ", name).strip()[:100],
                                     "value": value[:MAX_TYPED]}
            step = {"fill": {"selector": sel, "value": "{" + param + "}"}}
            if step in self.steps[-2:]:
                return  # the same field again (its change event after Enter)
            self._push(step)
        elif kind == "select":
            value = ev.get("value")
            if isinstance(value, str) and len(value) <= 200:
                self._push({"select": {"selector": sel, "value": value.replace("{", "").replace(
                    "}", "")}})
        elif kind == "extract":
            used = {k for s in self.steps if "extract" in s for k in s["extract"]}
            base = _slug(name, "text")
            key, n = base, 2
            while key in used:
                key, n = f"{base[:37]}_{n}", n + 1
            if self.steps and "extract" in self.steps[-1]:
                self.steps[-1]["extract"][key] = sel
            else:
                self._push({"extract": {key: sel}})

    def _size(self, w: Any, h: Any) -> None:
        """The window size the steps are made at: the action replays at that size."""
        if not (isinstance(w, int) and isinstance(h, int) and 320 <= w <= 3840 and 240 <= h <= 2160):
            return
        if self.viewport is None:
            self.viewport = {"width": w, "height": h}
        elif abs(self.viewport["width"] - w) > 40:
            self.warn(msg("web.record_resized",
                          "the window changed size while recording: the action runs at {size} "
                          "(the size of the first step); some sites change their menus with the "
                          "size", size=f"{self.viewport['width']}x{self.viewport['height']}"))

    def navigated(self, url: str) -> None:
        """The main page went to another address."""
        if self.done or not url.startswith(("http://", "https://")):
            return
        now = time.monotonic()
        if self._signing_in(url):
            # the person signs in during the recording: the action runs with the saved session,
            # so the steps that led here (and those on the sign-in page) are left out
            if len(self.steps) > 1 or self.params:
                self.steps = [{"goto": self._start}]
                self.params.clear()
                self._fields.clear()
            self.warn(msg("web.record_signed_in",
                          "you signed in during the recording: the sign-in steps were left out "
                          "(the action uses the saved session; next time, Sign in first)"))
            self._away = True
            return
        if self._away:  # back from the sign-in page (or from another site): a redirect
            self._away = False
            self._t_event = now
        if not _path_allowed(self.conn, url):
            p = urlsplit(url)
            self.warn(msg("web.record_left_site",
                          "the page went to {url}, outside the site or its allowed paths: an "
                          "action stops there", url=f"{_origin(url)}{p.path}"))
            self._away = True  # e.g. a sign-in on another site: its clicks are not steps
            return
        if now - self._t_start < NAV_AFTER_START_S or now - self._t_event < NAV_AFTER_EVENT_S:
            return  # a redirect, or the result of a click/Enter: replaying the click does it
        rel = self._rel(url)
        if rel is not None:
            self._goto(rel)

    def _rel(self, url: str) -> str | None:
        """The address relative to the base URL ("/admin/users?x=1"), or None when it is not
        on the site or its allowed paths."""
        if not url.startswith(("http://", "https://")) or not _path_allowed(self.conn, url):
            return None
        base_path = urlsplit(self.conn.resolved_base_url()).path.rstrip("/")
        p = urlsplit(url)
        rel = p.path.removeprefix(base_path) or "/"
        return _no_braces(rel + (f"?{p.query}" if p.query else ""))

    def _goto(self, rel: str) -> None:
        """Goes to an address. Pages passed through only on the way (a goto right after a goto)
        are dropped: the action goes straight to the last one."""
        if self.steps and "goto" in self.steps[-1]:
            if self.steps[-1]["goto"] != rel:
                self.steps[-1] = {"goto": rel}
            return
        self._push({"goto": rel})

    def request(self, method: str, url: str) -> None:
        if method not in ("GET", "HEAD") and _origin(url) == _origin(self.conn.resolved_base_url()):
            self.posted = True
            self.warn(msg("web.record_post",
                          "the page sent {method} requests: a read action blocks them (add "
                          "allow_post: true if they only search, or make it a write action)",
                          method=method))

    def new_tab(self) -> None:
        self.warn(msg("web.record_new_tab",
                      "a new tab was opened: an action runs in one tab, so steps in the other "
                      "tab were not recorded"))

    # --- the draft ------------------------------------------------------------------------------
    def action(self, fixed: set[str] | frozenset[str] = frozenset(),
               allow_post: bool = False) -> dict[str, Any]:
        """The draft. `fixed`: params that keep the text typed while recording. `allow_post`: the
        page may send POST requests (some sites connect or load data that way)."""
        fixed = {f for f in fixed if f in self.typed}
        a: dict[str, Any] = {"description": "Recorded action: check the steps and say what it does",
                             "access": "read"}
        if allow_post:
            a["allow_post"] = True
        if self.viewport:
            a["viewport"] = dict(self.viewport)
        if self.locale:
            a["locale"] = self.locale
        params = {k: dict(v) for k, v in self.params.items() if k not in fixed}
        if params:
            a["params"] = params
        steps = json.loads(json.dumps(self.steps))
        for st in steps:
            f = st.get("fill")
            if f and f["value"][1:-1] in fixed:  # braces would read as a {param}
                f["value"] = self.typed[f["value"][1:-1]]["value"].replace("{", "(").replace("}", ")")
        a["steps"] = steps
        return a

    def problems(self) -> list[str]:
        """What the draft action would fail on when saved (step rules, placeholders)."""
        errs = step_errors(self.steps)
        if not errs:
            try:
                Action.model_validate(self.action())
            except ValueError as e:
                errs.append(str(e))
        return errs

    def yaml(self, name: str, fixed: set[str] | frozenset[str] = frozenset(),
             allow_post: bool = False) -> str:
        name = name if ACTION_NAME.fullmatch(name) else "recorded"
        return yaml.safe_dump({name: self.action(fixed, allow_post)}, allow_unicode=True,
                              sort_keys=False, width=100)

    def summary(self) -> dict[str, Any]:
        if self.done and len(self.steps) > 1 and not any(
                k in s for s in self.steps for k in ("extract", "extract_all", "extract_table")):
            self.warn(msg("web.record_no_extract",
                          "the action extracts nothing, so the agent gets no data back: record "
                          "again and Alt+click the texts it needs"))
        return {"recording": not self.done, "steps": self.steps, "params": self.params,
                "typed": self.typed, "posted": self.posted,
                "warnings": [{"msg": str(w), **w.to_json()} for w in self.warnings],
                "problems": self.problems() if self.done else [], "error": self.error}


# --- the script in the page ---------------------------------------------------------------------
# Runs in the top page of the recording window only. It suggests a selector for each element:
# a stable id, a test/name/label attribute, the text of a button or tab, or a CSS path.
_RECORDER_JS = r"""
(() => {
  if (window.top !== window || window.__afsRecording) return;
  window.__afsRecording = true;
  const BANNER = __BANNER__;
  // each event says the window size: the action replays at the size it was recorded at
  const send = (ev) => { try { window.__afsRecord({ ...ev, vw: innerWidth, vh: innerHeight, lang: navigator.language }); } catch (e) { /* closing */ } };
  const esc = (s) => (window.CSS && CSS.escape) ? CSS.escape(s) : String(s).replace(/[^\w-]/g, "\\$&");
  const q = (s) => JSON.stringify(s);
  const unique = (s) => { try { return document.querySelectorAll(s).length === 1; } catch (e) { return false; } };
  const stableId = (id) => !!id && id.length < 60 && !/\d{3,}|^\d|[:]|[-_]\d+$/.test(id);
  // an item of a list (search results, menus): found by its text, never by its position
  const LISTED = "[role=option],[role=menuitem],[role=treeitem],li";
  const text = (el) => (el.innerText || el.textContent || "").trim().replace(/\s+/g, " ");
  const ATTRS = ["data-testid", "data-test", "data-qa", "name", "aria-label", "placeholder", "title"];
  const TEXTUAL = "button,a,summary,label,[role=button],[role=tab],[role=menuitem],[role=link],[role=option]";
  function selector(el, byText = true) {  // byText: false for an extract (its text is the value)
    const tag = el.tagName.toLowerCase();
    if (byText && el.matches(LISTED)) {
      const t = text(el);
      if (t && t.length <= 40) return tag + ":text-is(" + q(t) + ")";
    }
    if (stableId(el.id) && unique("#" + esc(el.id))) return "#" + esc(el.id);
    for (const a of ATTRS) {
      const v = el.getAttribute(a);
      if (v && v.length < 80) { const s = tag + "[" + a + "=" + q(v) + "]"; if (unique(s)) return s; }
    }
    if (byText && el.matches(TEXTUAL)) {
      const t = text(el);
      if (t && t.length <= 40) return tag + ":text-is(" + q(t) + ")";
    }
    for (const c of el.classList) {  // a class that names the element (not a generated one)
      if (/^[A-Za-z][\w-]{1,40}$/.test(c) && !/\d{3,}/.test(c) && unique(tag + "." + esc(c))) return tag + "." + esc(c);
    }
    const parts = [];
    for (let n = el; n && n.nodeType === 1 && n !== document.body; n = n.parentElement) {
      if (n !== el && stableId(n.id) && unique("#" + esc(n.id))) { parts.unshift("#" + esc(n.id)); break; }
      let p = n.tagName.toLowerCase();
      const same = n.parentElement ? [...n.parentElement.children].filter((c) => c.tagName === n.tagName) : [];
      if (same.length > 1) p += ":nth-of-type(" + (same.indexOf(n) + 1) + ")";
      parts.unshift(p);
    }
    return parts.join(" > ");
  }
  const className = (el) => [...el.classList].find((c) => /^[A-Za-z][\w-]{1,40}$/.test(c)) || "";
  function label(el) {
    if (el.labels && el.labels[0]) return text(el.labels[0]);
    return el.getAttribute("aria-label") || el.getAttribute("name") || el.getAttribute("placeholder") || el.id || "";
  }
  const isField = (el) => el.matches("textarea, select, input:not([type]), input[type=text], input[type=search], " +
    "input[type=email], input[type=number], input[type=tel], input[type=url], input[type=date], " +
    "input[type=datetime-local], input[type=password]");
  const inBanner = (el) => banner && banner.contains(el);
  function fill(el) {
    if (el.value === el.__afsSent) return;
    el.__afsSent = el.value;
    const secret = el.type === "password";  // its value never leaves the page
    send({ kind: "fill", selector: selector(el), name: label(el), secret,
      value: secret ? undefined : String(el.value).slice(0, 200) });
  }
  document.addEventListener("click", (e) => {
    const target = e.target;
    if (!(target instanceof Element) || inBanner(target)) return;
    if (e.altKey) {  // Alt+click: extract this text (the page does not see the click)
      e.preventDefault(); e.stopPropagation();
      const old = target.style.outline;
      target.style.outline = "2px solid #e8590c";
      setTimeout(() => { target.style.outline = old; }, 800);
      send({ kind: "extract", selector: selector(target, false),
        name: target.getAttribute("aria-label") || target.id || label(target) || className(target) });
      return;
    }
    // Enter in a form also "clicks" its submit button: the recorded Enter already does that
    if (e.detail === 0 && performance.now() - lastEnter < 500) return;
    const el = target.closest(TEXTUAL + "," + LISTED + ",input,select,textarea,[onclick]") || target;
    if (isField(el)) return;  // typed fields are recorded on change
    // a link to an address: the action goes to the address (it does not depend on the language
    // of the texts, nor on menus that change with the window size)
    const link = target.closest("a[href]");
    const href = link && link.getAttribute("href");
    if (link && href && !href.startsWith("#") && !/^(javascript|mailto|tel):/i.test(href)
        && !link.hasAttribute("download") && (!link.target || link.target === "_self")
        && !e.ctrlKey && !e.metaKey && !e.shiftKey) {
      send({ kind: "open", selector: selector(el), href: link.href });
      return;
    }
    send({ kind: "click", selector: selector(el) });
  }, true);
  document.addEventListener("change", (e) => {
    const el = e.target;
    if (!(el instanceof Element) || inBanner(el)) return;
    if (el.tagName === "SELECT") send({ kind: "select", selector: selector(el), value: el.value });
    else if (isField(el)) fill(el);
  }, true);
  document.addEventListener("keydown", (e) => {
    const el = e.target;
    if (e.key !== "Enter" || !(el instanceof Element) || el.tagName !== "INPUT" || !isField(el)) return;
    fill(el);  // the value first, then the key
    lastEnter = performance.now();
    send({ kind: "press", selector: selector(el), key: "Enter" });
  }, true);
  let banner = null;
  let box = null;
  let lastEnter = -1e9;
  // the hint never hides what the person wants to click: it moves to the other corner when the
  // mouse comes near it, and it disappears while Alt is held (Alt+click = extract)
  const CORNERS = [{ left: "12px", bottom: "12px", right: "auto", top: "auto" },
                   { left: "auto", bottom: "auto", right: "12px", top: "12px" }];
  let corner = 0;
  const show = () => {
    if (banner || !document.body) return;
    banner = document.createElement("div");
    const root = banner.attachShadow({ mode: "closed" });
    box = document.createElement("div");
    box.textContent = BANNER;
    Object.assign(box.style, { position: "fixed", zIndex: "2147483647", background: "#1f2430",
      color: "#ffffff", font: "13px system-ui, sans-serif", padding: "8px 12px", borderRadius: "6px",
      boxShadow: "0 2px 8px rgba(0,0,0,.3)", maxWidth: "420px", pointerEvents: "none",
      opacity: "0.92", transition: "opacity .15s" }, CORNERS[corner]);
    root.appendChild(box);
    document.documentElement.appendChild(banner);
  };
  document.addEventListener("mousemove", (e) => {
    if (!box) return;
    const r = box.getBoundingClientRect(), m = 48;
    if (e.clientX > r.left - m && e.clientX < r.right + m && e.clientY > r.top - m && e.clientY < r.bottom + m) {
      corner = 1 - corner;
      Object.assign(box.style, CORNERS[corner]);
    }
  }, true);
  const hide = (on) => { if (box) box.style.opacity = on ? "0" : "0.92"; };
  document.addEventListener("keydown", (e) => { if (e.key === "Alt") hide(true); }, true);
  document.addEventListener("keyup", (e) => { if (e.key === "Alt") hide(false); }, true);
  window.addEventListener("blur", () => hide(false));
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", show); else show();
})();
"""


def recorder_js(banner: str) -> str:
    text = banner.strip()[:300] or "Recording: work on the site as usual. Alt+click a text to " \
        "extract it. Close this window to finish."
    return _RECORDER_JS.replace("__BANNER__", json.dumps(text))


async def record(name: str, conn: Connection, rec: Recording, banner: str = "", *,
                 headless: bool = False) -> None:
    """Opens a visible window on the site with the connector's session and records until the
    person closes it, the editor stops it or 30 minutes pass (`headless` is for tests)."""
    start = conn.allowed_paths[0] if conn.allowed_paths else "/"
    base = conn.resolved_base_url()
    lock = _locks.setdefault(name, asyncio.Lock())
    try:
        async with lock, _playwright()() as pw:
            profile_dir(name).mkdir(parents=True, exist_ok=True)
            ctx = await pw.chromium.launch_persistent_context(str(profile_dir(name)),
                                                              **_launch_args(conn, headless=headless))
            closed = asyncio.Event()
            try:
                await _restore_state(name, ctx)
                page = rec.page = ctx.pages[0] if ctx.pages else await ctx.new_page()

                def on_event(source: dict[str, Any], ev: Any) -> None:
                    if source.get("frame") == page.main_frame:
                        rec.add(ev)

                await ctx.expose_binding("__afsRecord", on_event)
                await ctx.add_init_script(script=recorder_js(banner))
                ctx.on("request", lambda r: rec.request(r.method, r.url))
                ctx.on("page", lambda p: rec.new_tab() if p != page else None)
                page.on("framenavigated",
                        lambda f: rec.navigated(f.url) if f == page.main_frame else None)
                page.on("close", lambda *_: closed.set())
                ctx.on("close", lambda *_: closed.set())
                rec.begin(start)
                await page.goto(base + start)
                deadline = asyncio.get_running_loop().time() + RECORD_TIMEOUT_S
                while not (closed.is_set() or rec.stop.is_set()) and \
                        asyncio.get_running_loop().time() < deadline:
                    try:
                        _save_state(name, await ctx.storage_state())  # keep the session fresh
                    except Exception:  # noqa: BLE001 — the window is closing
                        break
                    try:
                        await asyncio.wait_for(closed.wait(), timeout=2)
                    except asyncio.TimeoutError:
                        pass
            finally:
                if not closed.is_set():
                    await ctx.close()
    except Exception as e:  # noqa: BLE001 — Playwright errors: a short line, no stack
        rec.error = f"{type(e).__name__}: {str(e)[:300]}"
    finally:
        rec.done = True

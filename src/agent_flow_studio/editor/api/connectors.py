"""Connectors: definitions, secrets, tests, website sign-in and recording, OAuth sign-in."""
from __future__ import annotations

import asyncio
import time
from typing import Any

import httpx
import yaml
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse
from pydantic import ValidationError

from ...connections import actions as conn_actions
from ...connections import client as conn_client
from ...connections import oauth as conn_oauth
from ...connections import recorder as conn_rec
from ...connections import store as conn_store
from ...connections import web as conn_web
from ...connections.model import SLUG as CONN_SLUG
from ...connections.model import Connection, types_meta
from ...publish import (
    PUBLIC,
    PrivateTermsError,
    check_public,
)
from ..validation import _structured
from .common import (
    OAUTH_CALLBACK,
    ActionTestBody,
    AllowPostBody,
    Ctx,
    RecordBody,
    SecretBody,
    _actions_yaml,
    _audit_event,
    _locations,
    _oauth_page,
    _read_json_file,
    _tests_file,
    _write_tests,
)


def _conns() -> dict[str, Connection]:
    try:
        return conn_store.load()
    except conn_store.ConnectorConfigError as e:
        raise HTTPException(422, str(e)) from e


def _conn_summary() -> dict[str, Any]:
    try:
        conns = conn_store.load()
    except conn_store.ConnectorConfigError:
        return {}
    return {n: {"type": c.type, "environment": c.environment, "description": c.description,
                "access": c.access, "has_instructions": bool(c.instructions.strip()),
                "actions_only": c.actions_only,  # what an agent with it gets (agent panel)
                "actions": {k: {"access": x.access, "description": x.description,
                                "params": list(x.params)} for k, x in c.actions.items()}}
            for n, c in conns.items()}


def _conn_name(name: str) -> str:
    if not CONN_SLUG.fullmatch(name):
        raise HTTPException(400, "invalid connector name")
    return name


def register(app: FastAPI, ctx: Ctx) -> None:
    csrf = ctx.csrf



    # --- connectors ---------------------------------------------------------------------------
    def _redirect_uri(request: Request) -> str:
        # the origin the editor was opened at (the Host is already limited to local hosts)
        return str(request.base_url).rstrip("/") + OAUTH_CALLBACK

    @app.get("/api/connections")
    def connections(request: Request) -> dict[str, Any]:
        conns = _conns()
        return {**types_meta(), "file": str(conn_store.connections_file()),
                "oauth_redirect_uri": _redirect_uri(request),
                "locations": _locations(),
                "connections": {n: c.model_dump(mode="json") | {
                    "location": conn_store.location(n),
                    "actions_yaml": _actions_yaml(c),
                    "web_session": ({"profile": conn_web.has_profile(n),
                                     "signing_in": n in signing_in} if c.type == "website" else None),
                    "secret": conn_store.secret_status(n, c),
                    "oauth": conn_store.oauth_status(n, c)} for n, c in conns.items()}}

    # --- website connectors: the person signs in in a browser window -----------------------------
    signing_in: dict[str, asyncio.Task[Any]] = {}

    def _website(name: str) -> Connection:
        c = _conns().get(_conn_name(name))
        if c is None:
            raise HTTPException(404, "connector does not exist")
        if c.type != "website":
            raise HTTPException(400, "not a website connector")
        return c

    @app.post("/api/connections/{name}/browser/sign-in", dependencies=[Depends(csrf)])
    async def browser_sign_in(name: str) -> dict[str, Any]:
        """Opens a visible browser window on the site's login page; returns at once. The person
        signs in there and closes the window; the session stays on this machine."""
        c = _website(name)
        if name in signing_in:
            return {"signing_in": True}
        try:
            conn_web._playwright()
        except conn_web.WebError as e:
            raise HTTPException(400, str(e)) from e

        async def run() -> None:
            try:
                await conn_web.sign_in(name, c)
            finally:
                signing_in.pop(name, None)

        signing_in[name] = asyncio.create_task(run())
        return {"signing_in": True}

    @app.delete("/api/connections/{name}/browser", dependencies=[Depends(csrf)])
    def browser_sign_out(name: str) -> dict[str, Any]:
        _website(name)
        conn_web.forget_session(name)
        return {"profile": False}

    # --- test one action from the Connectors window (read actions only) ----------------------
    @app.post("/api/connections/{name}/actions/{action}/test", dependencies=[Depends(csrf)])
    async def test_action(name: str, action: str, body: ActionTestBody) -> dict[str, Any]:
        """Runs a read action now (a website one in a visible window) and returns what an agent
        would get. Write actions only run in a flow, with the autonomy and approvals."""
        c = _conns().get(_conn_name(name))
        if c is None:
            raise HTTPException(404, "connector does not exist")
        a = c.actions.get(action)
        if a is None:
            raise HTTPException(404, "the connector has no such action")
        if a.access != "read":
            raise HTTPException(400, "write actions run only in a flow (with approval)")
        if c.type == "website" and name in signing_in:
            raise HTTPException(409, "the sign-in window is still open")
        _audit_event("connector_action_tested", connector=name, action=action)  # names only
        try:
            out: dict[str, Any] = {"ok": True, "result": await conn_actions.run(
                name, action, body.params, visible=c.type == "website")}
        except (conn_actions.ActionError, conn_client.ConnectorError) as e:  # no secrets in them
            out = {"ok": False, "error": str(e), "blocked": getattr(e, "blocked", [])}
        # the last test of each action stays on this machine (runs/, outside git), so the person
        # can look at what it returned when tuning the agent that uses it
        tests = _read_json_file(_tests_file(name))
        tests[action] = {**out, "ts": time.time(), "params": body.params}
        _write_tests(name, tests)
        return out

    @app.get("/api/connections/{name}/tests")
    def action_tests(name: str) -> dict[str, Any]:
        """The last test of each action of a connector (what it returned, or its error)."""
        return {"tests": _read_json_file(_tests_file(_conn_name(name)))}

    @app.post("/api/connections/{name}/actions/{action}/allow-post", dependencies=[Depends(csrf)])
    def action_allow_post(name: str, action: str, body: AllowPostBody) -> dict[str, Any]:
        """The person allows (or not) POST requests in one read action of a website: some sites
        connect to their server or load data that way. Steps still never write by themselves."""
        conns = _conns()
        c = conns.get(_conn_name(name))
        if c is None or action not in c.actions:
            raise HTTPException(404, "the connector has no such action")
        if c.type != "website":
            raise HTTPException(400, "allow_post is only for website connectors")
        c.actions[action].allow_post = body.allow
        conn_store.save(conns)
        _audit_event("connector_action_allow_post", connector=name, action=action,
                     allow=body.allow)
        return {"allow_post": body.allow}

    # --- website connectors: record an action while the person clicks on the site ---------------
    recordings: dict[str, conn_rec.Recording] = {}

    @app.post("/api/connections/{name}/browser/record", dependencies=[Depends(csrf)])
    async def browser_record(name: str, body: RecordBody) -> dict[str, Any]:
        """Opens a visible window on the site and records the steps; returns at once. The editor
        reads them with GET while the person works; the result is a draft, never saved here."""
        c = _website(name)
        if name in signing_in:
            raise HTTPException(409, "the sign-in window is still open")
        old = recordings.get(name)
        if old and not old.done:
            return old.summary()
        try:
            conn_web._playwright()
        except conn_web.WebError as e:
            raise HTTPException(400, str(e)) from e
        rec = recordings[name] = conn_rec.Recording(c)
        asyncio.create_task(conn_rec.record(name, c, rec, body.banner))
        return rec.summary()

    @app.get("/api/connections/{name}/browser/record")
    def browser_recording(name: str, action: str = "recorded", fixed: str = "",
                          allow_post: bool = False) -> dict[str, Any]:
        """`fixed`: params (comma separated) that keep the text typed while recording;
        `allow_post`: the draft lets the page send POST requests."""
        _website(name)
        rec = recordings.get(name)
        if rec is None:
            raise HTTPException(404, "no recording for this connector")
        keep = {f for f in fixed.split(",") if f}
        return {**rec.summary(),
                "yaml": rec.yaml(action, keep, allow_post) if rec.done and rec.steps else ""}

    @app.delete("/api/connections/{name}/browser/record", dependencies=[Depends(csrf)])
    def browser_record_stop(name: str) -> dict[str, Any]:
        """Stops a recording (closes its window) or, when it has ended, forgets it."""
        _website(name)
        rec = recordings.get(name)
        if rec is None:
            return {"recording": False}
        if rec.done:
            recordings.pop(name, None)
        else:
            rec.stop.set()
        return {"recording": not rec.done}

    # --- sign-in on a page (OAuth 2 + PKCE) ------------------------------------------------------
    @app.post("/api/connections/{name}/oauth/start", dependencies=[Depends(csrf)])
    def oauth_start(name: str, request: Request) -> dict[str, Any]:
        c = _conns().get(_conn_name(name))
        if c is None:
            raise HTTPException(404, "connector does not exist")
        redirect = _redirect_uri(request)
        try:
            url = conn_oauth.start(name, c, redirect)
        except conn_oauth.OAuthError as e:
            raise HTTPException(400, str(e)) from e
        return {"url": url, "redirect_uri": redirect}

    @app.delete("/api/connections/{name}/oauth", dependencies=[Depends(csrf)])
    def oauth_sign_out(name: str) -> dict[str, Any]:
        c = _conns().get(_conn_name(name))
        if c is None:
            raise HTTPException(404, "connector does not exist")
        conn_store.delete_oauth_tokens(name)
        return {"oauth": conn_store.oauth_status(name, c)}

    @app.get(OAUTH_CALLBACK, response_class=HTMLResponse)
    async def oauth_callback(state: str = "", code: str = "", error: str = "",
                             error_description: str = "") -> HTMLResponse:
        if error or not code:
            name = conn_oauth.cancel(state)  # a refused sign-in cannot be reused
            detail = (error or "no code") + (f" — {error_description}" if error_description else "")
            return _oauth_page(False, f"The provider did not sign in {name or 'the connector'}: "
                                      f"{detail[:300]}")
        try:
            name = await conn_oauth.finish(state, code, _conns())
        except conn_oauth.OAuthError as e:
            return _oauth_page(False, str(e))
        except httpx.HTTPError as e:
            return _oauth_page(False, f"could not reach the provider: {type(e).__name__}")
        return _oauth_page(True, f"Connector {name} is signed in. You can close this tab and go "
                                 "back to the editor.")

    @app.put("/api/connections/{name}", dependencies=[Depends(csrf)])
    def save_connection(name: str, body: dict[str, Any]) -> dict[str, Any]:
        conns = _conns()
        old = conns.get(_conn_name(name))
        body.pop("secret", None)
        loc = body.pop("location", None)  # where to keep it: local, an extension or public
        if loc is not None and loc not in _locations():
            raise HTTPException(400, f"unknown location {loc!r}")
        if "actions_yaml" in body:  # the editor edits the actions as YAML text
            text = str(body.pop("actions_yaml") or "")
            try:
                parsed = yaml.safe_load(text) if text.strip() else {}
            except yaml.YAMLError as e:
                raise HTTPException(422, f"actions: invalid YAML: {e}") from e
            if not isinstance(parsed, dict):
                raise HTTPException(422, "actions: a mapping of action name → definition")
            body["actions"] = parsed
        if old:  # secret references do not come from the UI: keep them
            body["secrets"] = dict(old.secrets)
        else:
            body["secrets"] = {}
        try:
            conns[name] = Connection.model_validate(body)
        except ValidationError as e:
            raise HTTPException(422, _structured(e, {})) from e
        if (loc or conn_store.location(name)) == PUBLIC:  # git publishes it: no private words
            try:
                check_public(name + "\n" + conns[name].model_dump_json())
            except PrivateTermsError as e:
                raise HTTPException(409, {"private_terms": e.terms}) from e
        conn_store.save(conns, moves={name: loc} if loc else None)
        if (old.instructions if old else "") != conns[name].instructions:
            _audit_event("connector_instructions_changed", connector=name,
                         chars=len(conns[name].instructions))  # never the text itself
        new = conns[name]
        if (old.actions if old else {}) != new.actions or \
                (old.actions_only if old else False) != new.actions_only:
            _audit_event("connector_actions_changed", connector=name,
                         actions={k: a.access for k, a in new.actions.items()},
                         actions_only=new.actions_only)  # names and access only
        return {"saved": name, "secret": conn_store.secret_status(name, conns[name])}

    @app.put("/api/connections/{name}/secret", dependencies=[Depends(csrf)])
    def set_secret(name: str, body: SecretBody) -> dict[str, Any]:
        conns = _conns()
        c = conns.get(_conn_name(name))
        if c is None:
            raise HTTPException(404, "connector does not exist")
        field = c.secret_field
        if field is None:
            raise HTTPException(400, "this authentication method does not use a secret")
        if bool(body.value) == bool(body.env):
            raise HTTPException(400, "give 'value' (stored in the keyring) or 'env'")
        if body.value:
            conn_store.set_secret(name, field, body.value)
            ref = "keyring"
        else:
            ref = "${" + str(body.env) + "}"
        conns[name] = c.model_copy(update={"secrets": {**c.secrets, field: ref}})
        conn_store.save(conns)
        return {"secret": conn_store.secret_status(name, conns[name])}

    @app.delete("/api/connections/{name}", dependencies=[Depends(csrf)])
    def delete_connection(name: str) -> dict[str, Any]:
        conns = _conns()
        c = conns.pop(_conn_name(name), None)
        if c is None:
            raise HTTPException(404, "connector does not exist")
        conn_store.delete_secrets(name, c)
        conn_store.save(conns)
        _tests_file(name).unlink(missing_ok=True)  # its saved test results go with it
        return {"deleted": name}

    @app.post("/api/connections/{name}/test", dependencies=[Depends(csrf)])
    async def test_connection(name: str) -> dict[str, Any]:
        return await conn_client.test(_conn_name(name), _conns())

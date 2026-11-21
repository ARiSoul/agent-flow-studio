"""Connector profiles (connections.yaml): type, authentication, environment and allowed paths.

They never hold secrets: each secret is a `${VAR}` or `keyring` reference (Windows Credential
Manager / system keychain), resolved only at request time.
"""
from __future__ import annotations

import json
import re
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

ConnType = Literal["http_api", "azure", "azure_devops", "microsoft_graph", "github", "gitlab",
                   "atlassian", "website"]
AuthMethod = Literal["none", "api_key", "bearer", "basic", "client_credentials", "azure_cli",
                     "pat", "gh_cli", "oauth_browser", "managed_identity", "browser_session"]
Environment = Literal["dev", "staging", "prod"]

SLUG = re.compile(r"^[a-z][a-z0-9_]{0,39}$")
_ENV_REF = re.compile(r"^\$\{[A-Z_][A-Z0-9_]*\}$")
# set by the sign-in itself; authorize_params cannot override them
_RESERVED_PARAMS = {"client_id", "redirect_uri", "response_type", "scope", "state",
                    "code_challenge", "code_challenge_method"}

# Microsoft Entra ID (sign-in on a page). {tenant} = tenant_id, or "organizations" when empty.
_ENTRA_AUTHORIZE = "https://login.microsoftonline.com/{tenant}/oauth2/v2.0/authorize"
_ENTRA_TOKEN = "https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token"
_DEVOPS = "499b84ac-1321-427f-aa17-267ca6975798"

# Per type: accepted methods, default base URL, the path used by "Test connection", the scope for
# app tokens (client credentials, CLI, managed identity) and the OAuth defaults for oauth_browser.
# In OAuth URLs, {origin} = scheme://host of the connector's base URL (self-hosted servers).
TYPES: dict[str, dict] = {
    # a site used like a person in a browser: your own sign-in (session kept on this machine) and
    # named actions with steps (connections/web.py); no API calls
    "website": {"label": "Website (your sign-in in a browser)", "methods": ["browser_session"],
                "base_url": None, "test_path": "/"},
    "http_api": {"label": "HTTP API", "methods": ["none", "api_key", "bearer", "basic",
                                                   "client_credentials", "oauth_browser"],
                 "base_url": None, "test_path": "/"},
    "azure": {"label": "Azure (Resource Manager)",
              "methods": ["azure_cli", "client_credentials", "oauth_browser", "managed_identity"],
              "base_url": "https://management.azure.com",
              "test_path": "/subscriptions?api-version=2022-12-01",
              "scope": "https://management.azure.com/.default",
              "oauth": {"authorize_url": _ENTRA_AUTHORIZE, "token_url": _ENTRA_TOKEN,
                        "scope": "https://management.azure.com/user_impersonation offline_access"}},
    "azure_devops": {"label": "Azure DevOps",
                     "methods": ["pat", "azure_cli", "client_credentials", "oauth_browser",
                                 "managed_identity"],
                     "base_url": "https://dev.azure.com/{organization}",
                     "test_path": "/_apis/projects?api-version=7.1",
                     "scope": f"{_DEVOPS}/.default",
                     "oauth": {"authorize_url": _ENTRA_AUTHORIZE, "token_url": _ENTRA_TOKEN,
                               "scope": f"{_DEVOPS}/user_impersonation offline_access"}},
    "microsoft_graph": {"label": "Microsoft Graph",
                        "methods": ["oauth_browser", "azure_cli", "client_credentials",
                                    "managed_identity"],
                        "base_url": "https://graph.microsoft.com",
                        "test_path": "/v1.0/organization",
                        "scope": "https://graph.microsoft.com/.default",
                        "oauth": {"authorize_url": _ENTRA_AUTHORIZE, "token_url": _ENTRA_TOKEN,
                                  "scope": "https://graph.microsoft.com/User.Read offline_access"}},
    "github": {"label": "GitHub", "methods": ["pat", "gh_cli", "oauth_browser"],
               "base_url": "https://api.github.com", "test_path": "/user",
               "oauth": {"authorize_url": "https://github.com/login/oauth/authorize",
                         "token_url": "https://github.com/login/oauth/access_token",
                         "scope": "read:user"}},
    "gitlab": {"label": "GitLab", "methods": ["pat", "oauth_browser"],
               "base_url": "https://gitlab.com/api/v4", "test_path": "/user",
               "oauth": {"authorize_url": "{origin}/oauth/authorize",
                         "token_url": "{origin}/oauth/token", "scope": "read_api"}},
    "atlassian": {"label": "Atlassian Cloud (Jira, Confluence)", "methods": ["basic"],
                  "base_url": "https://{organization}.atlassian.net",
                  "test_path": "/rest/api/3/myself"},
}

_META_AUTHORIZE = "https://www.facebook.com/v25.0/dialog/oauth"
_META_TOKEN = "https://graph.facebook.com/v25.0/oauth/access_token"
_META_EXCHANGE = {"url": _META_TOKEN, "grant_type": "fb_exchange_token",
                  "token_param": "fb_exchange_token"}

# Ready-made OAuth settings for HTTP API connectors (sign-in on a page). They only fill the form:
# check them against the provider's docs. Each provider needs an app registered by you, with the
# editor's redirect URI; some (Instagram, Threads) only accept an https redirect URI.
OAUTH_PRESETS: dict[str, dict] = {
    "facebook": {"label": "Facebook (Meta Graph API)",
                 "base_url": "https://graph.facebook.com/v25.0", "test_path": "/me",
                 "auth": {"authorize_url": _META_AUTHORIZE, "token_url": _META_TOKEN,
                          "scope": "public_profile", "token_in": "query",
                          "exchange": _META_EXCHANGE}},
    "instagram": {"label": "Instagram (Instagram Login)",
                  "base_url": "https://graph.instagram.com/v25.0", "test_path": "/me",
                  "auth": {"authorize_url": "https://www.instagram.com/oauth/authorize",
                           "token_url": "https://api.instagram.com/oauth/access_token",
                           "scope": "instagram_business_basic", "token_in": "query",
                           "exchange": {"url": "https://graph.instagram.com/access_token",
                                        "grant_type": "ig_exchange_token",
                                        "token_param": "access_token"}}},
    "threads": {"label": "Threads", "base_url": "https://graph.threads.net/v1.0",
                "test_path": "/me",
                "auth": {"authorize_url": "https://threads.com/oauth/authorize",
                         "token_url": "https://graph.threads.net/oauth/access_token",
                         "scope": "threads_basic", "token_in": "query",
                         "exchange": {"url": "https://graph.threads.net/access_token",
                                      "grant_type": "th_exchange_token",
                                      "token_param": "access_token"}}},
    "whatsapp": {"label": "WhatsApp Business (Meta Cloud API)",
                 "base_url": "https://graph.facebook.com/v25.0", "test_path": "/me",
                 "auth": {"authorize_url": _META_AUTHORIZE, "token_url": _META_TOKEN,
                          "scope": "whatsapp_business_management,whatsapp_business_messaging",
                          "exchange": _META_EXCHANGE}},
    "reddit": {"label": "Reddit", "base_url": "https://oauth.reddit.com",
               "test_path": "/api/v1/me",
               "auth": {"authorize_url": "https://www.reddit.com/api/v1/authorize",
                        "token_url": "https://www.reddit.com/api/v1/access_token",
                        "scope": "identity read", "token_auth": "basic",
                        "authorize_params": {"duration": "permanent"}}},
}
# secret required by each method (field name)
SECRET_FIELD: dict[str, str | None] = {
    "none": None, "api_key": "api_key", "bearer": "token", "basic": "password",
    "client_credentials": "client_secret", "azure_cli": None, "pat": "token", "gh_cli": None,
    "oauth_browser": "client_secret", "managed_identity": None, "browser_session": None,
}
# methods whose secret is optional (public OAuth clients use PKCE without a client secret)
OPTIONAL_SECRET = {"oauth_browser"}
# keyring field where a sign-in on a page keeps its tokens (JSON); never in connections.yaml
OAUTH_TOKEN_FIELD = "oauth_token"
METHOD_LABEL = {
    "none": "No authentication", "api_key": "API key (header)", "bearer": "Bearer token",
    "basic": "Username and password", "client_credentials": "Client credentials (OAuth 2)",
    "azure_cli": "Azure CLI session (az login)", "pat": "Personal Access Token",
    "gh_cli": "GitHub CLI session (gh auth login)",
    "oauth_browser": "Sign in on a page (OAuth 2)",
    "managed_identity": "Azure managed identity",
    "browser_session": "Your sign-in in a browser window (session kept on this machine)",
}


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class TokenExchange(_Strict):
    """Swaps the first token for a long-lived one (Meta: about 60 days, no refresh token)."""
    url: str
    grant_type: str = Field(pattern=r"^[a-z_]{1,40}$")
    token_param: str = Field(default="access_token", pattern=r"^[a-z_]{1,40}$")


class Auth(_Strict):
    method: AuthMethod = "none"
    header: str = Field(default="X-API-Key", pattern=r"^[A-Za-z0-9-]{1,64}$")  # api_key
    username: str | None = None  # basic
    tenant_id: str | None = None  # client_credentials / oauth_browser (Entra ID)
    client_id: str | None = None  # client_credentials, oauth_browser, user-assigned identity
    token_url: str | None = None  # client_credentials (http_api), oauth_browser
    scope: str | None = None  # defaults to the type's
    # oauth_browser (the type gives the defaults, except for HTTP API)
    authorize_url: str | None = None
    authorize_params: dict[str, str] = Field(default_factory=dict)  # extra, e.g. duration
    token_auth: Literal["body", "basic"] = "body"  # how the client secret goes to token_url
    token_in: Literal["header", "query"] = "header"  # Bearer header or ?access_token=
    exchange: TokenExchange | None = None


# Text that looks like a secret (instructions go into agent prompts: never credentials there).
_SECRET_LIKE = re.compile(
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----|\bgh[pousr]_[A-Za-z0-9]{20,}|\bgithub_pat_[A-Za-z0-9_]{20,}"
    r"|\bglpat-[A-Za-z0-9_-]{20,}|\bxox[abprs]-[A-Za-z0-9-]{10,}|\bAKIA[0-9A-Z]{16}\b"
    r"|\beyJ[A-Za-z0-9_-]{10,}\.eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}"
    r"|(?i:\b(?:password|passwd|pwd|secret|api[_-]?key|client[_-]?secret|token)\b\s*[:=]\s*"
    r"(?!\$\{)[\"']?[^\s\"'${}<>]{8,})")
MAX_INSTRUCTIONS = 20_000
_PLACEHOLDER = re.compile(r"\{([a-z][a-z0-9_]{0,39})\}")
ACTION_NAME = re.compile(r"^[a-z][a-z0-9_]{0,39}$")


class ActionParam(_Strict):
    """A parameter of an action; the value the agent gives is checked before the request."""
    type: Literal["text", "integer", "number", "boolean", "choice", "date"] = "text"
    description: str = ""
    required: bool = True
    pattern: str | None = None  # text: the whole value must match
    options: list[str] = Field(default_factory=list)  # choice
    max_length: int = Field(default=200, ge=1, le=10_000)

    @model_validator(mode="after")
    def _check(self) -> ActionParam:
        if self.type == "choice" and not self.options:
            raise ValueError("a choice parameter needs 'options'")
        if self.pattern:
            try:
                re.compile(self.pattern)
            except re.error as e:
                raise ValueError(f"invalid pattern: {e}") from e
        return self


class Viewport(_Strict):
    """The window size a website action runs at: many sites change their layout (menus, links)
    with the size, so the action uses the size it was recorded at."""
    width: int = Field(ge=320, le=3840)
    height: int = Field(ge=240, le=2160)


class Action(_Strict):
    """A named operation of a connector: the only way the agents use it when `actions_only`.
    HTTP: method + path/query/body templates with {param} placeholders. Website connectors use
    `steps` instead (see connections/web.py)."""
    description: str = Field(min_length=1, max_length=500)
    access: Literal["read", "write"] = "read"
    method: Literal["GET", "POST", "PUT", "PATCH", "DELETE"] = "GET"
    path: str = ""  # relative to the base URL, starts with /; {param} is URL-encoded
    query: dict[str, str] = Field(default_factory=dict)
    body: Any = None  # write: JSON; a string that is exactly "{param}" takes the typed value
    params: dict[str, ActionParam] = Field(default_factory=dict)
    instructions: str = Field(default="", max_length=5_000)  # when/how to use it (for the agent)
    steps: list[dict[str, Any]] = Field(default_factory=list)  # website connectors (web.py)
    # website, read actions: the page may send POST requests (e.g. a search form); by default a
    # read action blocks every request that is not GET/HEAD, so it cannot change anything
    allow_post: bool = False
    # personal data written in free text that the agents should not see: replaced in every value
    # the action returns ("[phone]", "[email]") before it reaches the model
    redact: list[Literal["phone", "email"]] = Field(default_factory=list)
    viewport: Viewport | None = None  # website: the window size (default 1280x720)
    # website: the browser language (e.g. pt-PT): a multilingual site shows its texts, and the
    # steps find buttons and links by those texts, in the language it was recorded in
    locale: str | None = Field(default=None, pattern=r"^[a-z]{2,3}(-[A-Za-z0-9]{2,8}){0,2}$")

    def placeholders(self) -> set[str]:
        texts = [self.path, *self.query.values(), json.dumps(self.body)]
        texts += [json.dumps(s) for s in self.steps]
        return {m for t in texts for m in _PLACEHOLDER.findall(t)}

    @model_validator(mode="after")
    def _check(self) -> Action:
        for n in self.params:
            if not ACTION_NAME.fullmatch(n):
                raise ValueError(f"invalid parameter name {n!r}")
        unknown = self.placeholders() - set(self.params)
        if unknown:
            raise ValueError(f"placeholder(s) without a parameter: {', '.join(sorted(unknown))}")
        if _SECRET_LIKE.search(self.instructions) or _SECRET_LIKE.search(self.description):
            raise ValueError("action text: this looks like a secret")
        return self


# Steps of a website action: one key per step. Selectors are Playwright selectors (CSS, "text=…").
# goto takes a path relative to the base URL ({param} URL-encoded); fill/select values take
# {param} as text. extract*: name → selector; the values are what the action returns.
# get_json: a GET with the signed-in session to the site's own JSON (no page, no clicks), keeping
# only the listed fields: {path, fields: {name: "a.b" | "list[].x" | "list[].{x,y.z}"}}.
STEP_KINDS = {"goto": str, "click": str, "wait_for": str, "fill": dict, "select": dict,
              "press": dict, "extract": dict, "extract_all": dict, "extract_table": str,
              "get_json": dict}
_STEP_FIELDS = {"fill": {"selector", "value"}, "select": {"selector", "value"},
                "press": {"selector", "key"}, "get_json": {"path", "fields"}}
_FIELD_PATH = re.compile(r"^[A-Za-z_][\w-]*(\[\])?(\.([A-Za-z_][\w-]*(\[\])?|\{[\w.,-]+\}))*$")


def step_errors(steps: list[dict[str, Any]]) -> list[str]:
    errors = []
    for i, st in enumerate(steps, 1):
        if not isinstance(st, dict) or len(st) != 1:
            errors.append(f"step {i}: one key per step ({', '.join(STEP_KINDS)})")
            continue
        (kind, arg), = st.items()
        if kind not in STEP_KINDS:
            errors.append(f"step {i}: unknown step {kind!r}")
        elif not isinstance(arg, STEP_KINDS[kind]) or (isinstance(arg, str) and not arg.strip()):
            errors.append(f"step {i} ({kind}): expected {STEP_KINDS[kind].__name__}")
        elif kind in _STEP_FIELDS and set(arg) != _STEP_FIELDS[kind]:
            errors.append(f"step {i} ({kind}): needs {', '.join(sorted(_STEP_FIELDS[kind]))}")
        elif kind in ("extract", "extract_all") and (not arg or not all(
                ACTION_NAME.fullmatch(k) and isinstance(v, str) and v for k, v in arg.items())):
            errors.append(f"step {i} ({kind}): name → selector")
        elif kind in ("goto", "get_json"):
            p = _PLACEHOLDER.sub("x", arg if kind == "goto" else str(arg["path"]))
            if not p.startswith("/") or ".." in p or "://" in p:
                errors.append(f"step {i} ({kind}): a path relative to the base URL, starting with /")
            if kind == "get_json" and (not isinstance(arg["fields"], dict) or not arg["fields"]
                                       or not all(ACTION_NAME.fullmatch(k) and isinstance(v, str)
                                                  and _FIELD_PATH.fullmatch(v)
                                                  for k, v in arg["fields"].items())):
                errors.append(f"step {i} (get_json): fields = name → path (a.b, list[].x, "
                              "list[].{x,y.z})")
    return errors


class WebSettings(_Strict):
    """Website connectors: how the studio knows the session is still signed in."""
    login_url: str | None = None  # where to sign in (path or https URL); default: the base URL
    signed_in_selector: str | None = None  # an element only shown when signed in (e.g. the avatar)
    signed_out_url: str | None = None  # text in the URL when the site asks to sign in (e.g. /login)
    # "Test connection" on a site used through get_json: a harmless GET that answers JSON when
    # signed in (e.g. the notifications the site polls); without it, the test opens a page
    check_path: str | None = None
    browser: Literal["msedge", "chrome", "chromium"] = "msedge"  # installed Edge/Chrome, or bundled
    timeout_s: int = Field(default=30, ge=5, le=300)


class Connection(_Strict):
    type: ConnType
    description: str = ""
    # how to use this connector (endpoints, fields, limits, pitfalls): added to the prompt of every
    # agent that has it. Never secrets; it does not widen access (paths and access still apply).
    instructions: str = Field(default="", max_length=MAX_INSTRUCTIONS)
    # named operations (permissions): with actions_only, the agents can do only these
    actions: dict[str, Action] = Field(default_factory=dict)
    actions_only: bool = False
    web: WebSettings = Field(default_factory=WebSettings)  # website connectors only
    environment: Environment = "dev"
    access: Literal["read", "write"] = "read"  # write: allows http_write (with the flow autonomy)
    base_url: str | None = None
    organization: str | None = Field(default=None, pattern=r"^[A-Za-z0-9_.-]{1,100}$")
    test_path: str | None = None
    allowed_paths: list[str] = Field(default_factory=list)  # prefixes; empty = all
    auth: Auth = Field(default_factory=Auth)
    secrets: dict[str, str] = Field(default_factory=dict)  # field → "${VAR}" or "keyring"

    @model_validator(mode="after")
    def _check(self) -> Connection:
        meta = TYPES[self.type]
        if self.auth.method not in meta["methods"]:
            raise ValueError(f"{meta['label']}: method {self.auth.method!r} not supported "
                             f"(use {', '.join(meta['methods'])})")
        if self.type == "azure_devops" and not self.organization:
            raise ValueError("Azure DevOps needs 'organization'")
        if self.type == "atlassian" and not self.organization:
            raise ValueError("Atlassian Cloud needs 'organization' (the site: <site>.atlassian.net)")
        if self.type == "http_api" and not self.base_url:
            raise ValueError("HTTP API needs 'base_url'")
        if self.type == "website":
            if not self.base_url:
                raise ValueError("a website needs 'base_url' (the site's address)")
            lu = self.web.login_url
            if lu and not lu.startswith(("/", "https://")):
                raise ValueError("web.login_url: a path (/login) or an https:// URL")
            cp = self.web.check_path
            if cp and (not cp.startswith("/") or ".." in cp or "://" in cp):
                raise ValueError("web.check_path: a path relative to the base URL, starting with /")
        local = re.match(r"^http://(localhost|127\.0\.0\.1)(:\d+)?(/|$)", self.base_url or "")
        if self.base_url and not self.base_url.startswith("https://") and not (
                self.type == "website" and local):  # a site on this machine may be plain http
            raise ValueError("base_url must be https://")
        if self.auth.method == "client_credentials":
            if not self.auth.client_id:
                raise ValueError("client credentials needs client_id")
            if self.type == "http_api" and not (self.auth.token_url or "").startswith("https://"):
                raise ValueError("client credentials needs an https:// token_url")
            if self.type != "http_api" and not self.auth.tenant_id:
                raise ValueError("client credentials on Azure needs tenant_id")
        if self.auth.method == "basic" and not self.auth.username:
            raise ValueError("basic authentication needs username")
        if self.auth.method == "oauth_browser":
            if not self.auth.client_id:
                raise ValueError("sign-in on a page needs client_id (your app at the provider)")
            authorize, token, _ = self.resolved_oauth()
            urls = {"authorize_url": authorize, "token_url": token}
            if self.auth.exchange:
                urls["exchange.url"] = self.auth.exchange.url
            for label, url in urls.items():
                if not url.startswith("https://"):
                    raise ValueError(f"sign-in on a page needs an https:// {label}")
        if self.auth.method != "oauth_browser" and (self.auth.token_in == "query"
                                                     or self.auth.exchange):
            raise ValueError("token_in: query and exchange are only for sign-in on a page")
        for k in self.auth.authorize_params:
            if not re.fullmatch(r"[a-z_]{1,40}", k) or k in _RESERVED_PARAMS:
                raise ValueError(f"authorize_params: invalid or reserved name {k!r}")
        for p in [*self.allowed_paths, *( [self.test_path] if self.test_path else [])]:
            if not p.startswith("/") or ".." in p or "://" in p:
                raise ValueError(f"invalid path {p!r}: relative to the base, starts with /")
        for an, act in self.actions.items():
            if not ACTION_NAME.fullmatch(an):
                raise ValueError(f"invalid action name {an!r} (lowercase, digits and _)")
            if act.access == "write" and self.access != "write":
                raise ValueError(f"action {an}: write actions need a connector with access: write")
            if self.type == "website":
                if not act.steps:
                    raise ValueError(f"action {an}: a website action needs 'steps'")
                errs = step_errors(act.steps)
                if errs:
                    raise ValueError(f"action {an}: " + "; ".join(errs))
                if act.path or act.query or act.body is not None:
                    raise ValueError(f"action {an}: website actions use steps, not path/query/body")
            else:
                if act.allow_post:
                    raise ValueError(f"action {an}: allow_post is only for website connectors")
                if act.viewport or act.locale:
                    raise ValueError(f"action {an}: viewport and locale are only for website "
                                     "connectors")
                if not act.path:
                    raise ValueError(f"action {an}: 'path' is required")
                if act.steps:
                    raise ValueError(f"action {an}: 'steps' are only for website connectors")
                if (act.access == "read") != (act.method == "GET"):
                    raise ValueError(f"action {an}: read actions use GET, write actions "
                                     "POST/PUT/PATCH/DELETE")
                p = _PLACEHOLDER.sub("x", act.path)
                if not p.startswith("/") or ".." in p or "://" in p:
                    raise ValueError(f"action {an}: path must start with / (relative to the base)")
                if act.body is not None and act.access == "read":
                    raise ValueError(f"action {an}: a read action has no body")
        if self.actions_only and not self.actions:
            raise ValueError("actions_only needs at least one action")
        if _SECRET_LIKE.search(self.instructions) or _SECRET_LIKE.search(self.description):
            raise ValueError("instructions/description: this looks like a secret (a key, a token "
                             "or a password); secrets go in the keyring or ${VAR}, never in the "
                             "text that agents read")
        for k, v in self.secrets.items():
            if not (v == "keyring" or _ENV_REF.fullmatch(v)):
                raise ValueError(f"secret {k}: use 'keyring' or ${{VAR}}; the value is never "
                                 "stored in the file")
        return self

    @property
    def secret_field(self) -> str | None:
        return SECRET_FIELD[self.auth.method]

    def resolved_base_url(self) -> str:
        url = self.base_url or TYPES[self.type]["base_url"]
        return url.replace("{organization}", self.organization or "").rstrip("/")

    def resolved_test_path(self) -> str:
        return self.test_path or TYPES[self.type]["test_path"]

    def resolved_scope(self) -> str | None:
        if self.auth.method == "oauth_browser":
            return self.auth.scope or TYPES[self.type].get("oauth", {}).get("scope")
        return self.auth.scope or TYPES[self.type].get("scope")

    def resolved_oauth(self) -> tuple[str, str, str]:
        """(authorize_url, token_url, scope) for sign-in on a page."""
        meta = TYPES[self.type].get("oauth", {})
        base = self.resolved_base_url() if (self.base_url or TYPES[self.type]["base_url"]) else ""
        origin = "/".join(base.split("/")[:3]) if base.startswith("https://") else ""

        def fill(u: str | None) -> str:
            return (u or "").replace("{tenant}", self.auth.tenant_id or "organizations").replace(
                "{origin}", origin)

        return (fill(self.auth.authorize_url or meta.get("authorize_url")),
                fill(self.auth.token_url or meta.get("token_url")), self.resolved_scope() or "")


def types_meta() -> dict:
    """For the editor: types, methods, the secret each method needs and the OAuth presets."""
    return {
        "types": {k: {"label": v["label"], "methods": v["methods"], "base_url": v["base_url"],
                      "test_path": v["test_path"], "oauth": v.get("oauth")}
                  for k, v in TYPES.items()},
        "methods": {m: {"label": METHOD_LABEL[m], "secret": SECRET_FIELD[m],
                        "optional": m in OPTIONAL_SECRET} for m in METHOD_LABEL},
        "environments": ["dev", "staging", "prod"],
        "oauth_presets": OAUTH_PRESETS,
    }

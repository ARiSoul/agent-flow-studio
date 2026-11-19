"""Connector profiles (connections.yaml): type, authentication, environment and allowed paths.

They never hold secrets: each secret is a `${VAR}` or `keyring` reference (Windows Credential
Manager / system keychain), resolved only at request time.
"""
from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

ConnType = Literal["http_api", "azure", "azure_devops", "microsoft_graph", "github", "gitlab",
                   "atlassian"]
AuthMethod = Literal["none", "api_key", "bearer", "basic", "client_credentials", "azure_cli",
                     "pat", "gh_cli", "oauth_browser", "managed_identity"]
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
    "oauth_browser": "client_secret", "managed_identity": None,
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


class Connection(_Strict):
    type: ConnType
    description: str = ""
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
        if self.base_url and not self.base_url.startswith("https://"):
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

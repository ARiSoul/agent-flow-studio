"""Connector profiles (connections.yaml): type, authentication, environment and allowed paths.

They never hold secrets: each secret is a `${VAR}` or `keyring` reference (Windows Credential
Manager / system keychain), resolved only at request time.
"""
from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

ConnType = Literal["http_api", "azure", "azure_devops", "github"]
AuthMethod = Literal["none", "api_key", "bearer", "basic", "client_credentials", "azure_cli",
                     "pat", "gh_cli"]
Environment = Literal["dev", "staging", "prod"]

SLUG = re.compile(r"^[a-z][a-z0-9_]{0,39}$")
_ENV_REF = re.compile(r"^\$\{[A-Z_][A-Z0-9_]*\}$")

# per type: accepted methods, default base URL and the path used by "Test connection"
TYPES: dict[str, dict] = {
    "http_api": {"label": "HTTP API", "methods": ["none", "api_key", "bearer", "basic",
                                                   "client_credentials"],
                 "base_url": None, "test_path": "/"},
    "azure": {"label": "Azure (Resource Manager)", "methods": ["azure_cli", "client_credentials"],
              "base_url": "https://management.azure.com",
              "test_path": "/subscriptions?api-version=2022-12-01",
              "scope": "https://management.azure.com/.default"},
    "azure_devops": {"label": "Azure DevOps", "methods": ["pat", "azure_cli", "client_credentials"],
                     "base_url": "https://dev.azure.com/{organization}",
                     "test_path": "/_apis/projects?api-version=7.1",
                     "scope": "499b84ac-1321-427f-aa17-267ca6975798/.default"},
    "github": {"label": "GitHub", "methods": ["pat", "gh_cli"], "base_url": "https://api.github.com",
               "test_path": "/user"},
}
# secret required by each method (field name)
SECRET_FIELD: dict[str, str | None] = {
    "none": None, "api_key": "api_key", "bearer": "token", "basic": "password",
    "client_credentials": "client_secret", "azure_cli": None, "pat": "token", "gh_cli": None,
}
METHOD_LABEL = {
    "none": "No authentication", "api_key": "API key (header)", "bearer": "Bearer token",
    "basic": "Username and password", "client_credentials": "Client credentials (OAuth 2)",
    "azure_cli": "Azure CLI session (az login)", "pat": "Personal Access Token",
    "gh_cli": "GitHub CLI session (gh auth login)",
}


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Auth(_Strict):
    method: AuthMethod = "none"
    header: str = Field(default="X-API-Key", pattern=r"^[A-Za-z0-9-]{1,64}$")  # api_key
    username: str | None = None  # basic
    tenant_id: str | None = None  # client_credentials (Azure)
    client_id: str | None = None  # client_credentials
    token_url: str | None = None  # generic client_credentials (http_api)
    scope: str | None = None  # client_credentials / azure_cli (defaults to the type's)


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
        return self.auth.scope or TYPES[self.type].get("scope")


def types_meta() -> dict:
    """For the editor: types, methods and the secret each method needs."""
    return {
        "types": {k: {"label": v["label"], "methods": v["methods"], "base_url": v["base_url"],
                      "test_path": v["test_path"]} for k, v in TYPES.items()},
        "methods": {m: {"label": METHOD_LABEL[m], "secret": SECRET_FIELD[m]} for m in METHOD_LABEL},
        "environments": ["dev", "staging", "prod"],
    }

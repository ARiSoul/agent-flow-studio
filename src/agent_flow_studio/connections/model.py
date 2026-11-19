"""Perfis de conectores (connections.yaml): tipo, autenticação, ambiente e caminhos permitidos.

Nunca contêm segredos: cada segredo é uma referência `${VAR}` ou `keyring` (Gestor de Credenciais
do Windows / keychain do sistema), resolvida só no momento do pedido.
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

# por tipo: métodos aceites, URL base por defeito e caminho usado no "Testar ligação"
TYPES: dict[str, dict] = {
    "http_api": {"label": "API HTTP", "methods": ["none", "api_key", "bearer", "basic",
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
# segredo necessário por método (nome do campo)
SECRET_FIELD: dict[str, str | None] = {
    "none": None, "api_key": "api_key", "bearer": "token", "basic": "password",
    "client_credentials": "client_secret", "azure_cli": None, "pat": "token", "gh_cli": None,
}
METHOD_LABEL = {
    "none": "Sem autenticação", "api_key": "Chave de API (cabeçalho)", "bearer": "Token Bearer",
    "basic": "Utilizador e palavra-passe", "client_credentials": "Client credentials (OAuth 2)",
    "azure_cli": "Sessão da Azure CLI (az login)", "pat": "Personal Access Token",
    "gh_cli": "Sessão da GitHub CLI (gh auth login)",
}


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Auth(_Strict):
    method: AuthMethod = "none"
    header: str = Field(default="X-API-Key", pattern=r"^[A-Za-z0-9-]{1,64}$")  # api_key
    username: str | None = None  # basic
    tenant_id: str | None = None  # client_credentials (Azure)
    client_id: str | None = None  # client_credentials
    token_url: str | None = None  # client_credentials genérico (http_api)
    scope: str | None = None  # client_credentials / azure_cli (por defeito o do tipo)


class Connection(_Strict):
    type: ConnType
    description: str = ""
    environment: Environment = "dev"
    access: Literal["read", "write"] = "read"  # write: permite http_write (com a autonomia do flow)
    base_url: str | None = None
    organization: str | None = Field(default=None, pattern=r"^[A-Za-z0-9_.-]{1,100}$")
    test_path: str | None = None
    allowed_paths: list[str] = Field(default_factory=list)  # prefixos; vazio = todos
    auth: Auth = Field(default_factory=Auth)
    secrets: dict[str, str] = Field(default_factory=dict)  # campo → "${VAR}" ou "keyring"

    @model_validator(mode="after")
    def _check(self) -> Connection:
        meta = TYPES[self.type]
        if self.auth.method not in meta["methods"]:
            raise ValueError(f"{meta['label']}: método {self.auth.method!r} não suportado "
                             f"(use {', '.join(meta['methods'])})")
        if self.type == "azure_devops" and not self.organization:
            raise ValueError("Azure DevOps precisa de 'organization'")
        if self.type == "http_api" and not self.base_url:
            raise ValueError("API HTTP precisa de 'base_url'")
        if self.base_url and not self.base_url.startswith("https://"):
            raise ValueError("base_url tem de ser https://")
        if self.auth.method == "client_credentials":
            if not self.auth.client_id:
                raise ValueError("client credentials precisa de client_id")
            if self.type == "http_api" and not (self.auth.token_url or "").startswith("https://"):
                raise ValueError("client credentials precisa de token_url https://")
            if self.type != "http_api" and not self.auth.tenant_id:
                raise ValueError("client credentials no Azure precisa de tenant_id")
        if self.auth.method == "basic" and not self.auth.username:
            raise ValueError("autenticação basic precisa de username")
        for p in [*self.allowed_paths, *( [self.test_path] if self.test_path else [])]:
            if not p.startswith("/") or ".." in p or "://" in p:
                raise ValueError(f"caminho inválido {p!r}: relativo à base, começa por /")
        for k, v in self.secrets.items():
            if not (v == "keyring" or _ENV_REF.fullmatch(v)):
                raise ValueError(f"segredo {k}: use 'keyring' ou ${{VAR}}; o valor não fica no ficheiro")
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
    """Para o editor: tipos, métodos e o segredo pedido por cada método."""
    return {
        "types": {k: {"label": v["label"], "methods": v["methods"], "base_url": v["base_url"],
                      "test_path": v["test_path"]} for k, v in TYPES.items()},
        "methods": {m: {"label": METHOD_LABEL[m], "secret": SECRET_FIELD[m]} for m in METHOD_LABEL},
        "environments": ["dev", "staging", "prod"],
    }

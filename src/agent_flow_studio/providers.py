"""Model providers: where an agent's model runs.

The runtime is always the Claude Agent SDK (the guard hook, the write policy and the io tools live
there); a provider only changes where the CLI sends its model calls, through the environment of
the session (`ClaudeAgentOptions.env`). Providers are data in `providers.yaml` (this machine,
outside git; `PROVIDERS_FILE` moves it) and in `extensions/<name>/providers.yaml`:

```yaml
providers:
  bedrock:                       # a name: models are written `bedrock/<model id>`
    kind: bedrock                # anthropic | bedrock | vertex | foundry | gateway
    region: eu-west-1
    models: [eu.anthropic.claude-sonnet-4-6]
  litellm:
    kind: gateway                # anything that speaks the Anthropic Messages API (LiteLLM…)
    base_url: http://localhost:4000
    key: ${LITELLM_KEY}          # or `keyring` (set with `agent-flow-studio-flows provider-key`)
    models: [gpt-5, gemini-2.5-pro]
```

An agent or flow `model` of `<provider>/<model>` runs on that provider; a model without a known
provider prefix runs as before (the CLI's own sign-in). Keys are only `${VAR}` or `keyring`
references, resolved when a session starts, never written to files, events or logs. The session
environment always blanks the Anthropic key, token and switches it does not set, so a key of
this machine never reaches another provider.
"""
from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from .messages import MsgError, msg

NAME = re.compile(r"[a-z][a-z0-9-]{0,39}")
_REF = re.compile(r"\$\{([A-Z_][A-Z0-9_]*)\}")
KEY_FIELD = "key"
Kind = Literal["anthropic", "bedrock", "vertex", "foundry", "gateway"]
KINDS: tuple[str, ...] = ("anthropic", "bedrock", "vertex", "foundry", "gateway")
# what a provider's session may change; each session sets all of them (blank = not used)
_MANAGED = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL",
            "CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_USE_VERTEX", "CLAUDE_CODE_USE_FOUNDRY",
            "ANTHROPIC_FOUNDRY_API_KEY")
_ALIASES = ("ANTHROPIC_DEFAULT_HAIKU_MODEL", "ANTHROPIC_DEFAULT_SONNET_MODEL",
            "ANTHROPIC_DEFAULT_OPUS_MODEL", "ANTHROPIC_SMALL_FAST_MODEL")


class ProviderError(MsgError):
    pass


class Provider(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Kind
    title: str | None = Field(default=None, max_length=80)
    base_url: str | None = None  # gateway (required); a proxy for the other kinds
    key: str | None = None  # "${VAR}" or "keyring": anthropic, gateway, foundry
    region: str | None = Field(default=None, max_length=40)  # bedrock, vertex
    project: str | None = Field(default=None, max_length=80)  # vertex
    resource: str | None = Field(default=None, max_length=80)  # foundry
    aws_profile: str | None = Field(default=None, max_length=80)  # bedrock
    models: list[str] = Field(default_factory=list)  # offered in the editor's model picker
    small_model: str | None = None  # background calls (gateway default: the agent's model)
    priced: bool | None = None  # the CLI knows its prices (default: not for a gateway)

    @field_validator("key")
    @classmethod
    def _key(cls, v: str | None) -> str | None:
        if v is not None and v != "keyring" and not _REF.fullmatch(v):
            raise ValueError("key must be ${VAR} or 'keyring' (never the secret itself)")
        return v

    @field_validator("base_url")
    @classmethod
    def _url(cls, v: str | None) -> str | None:
        if v is not None and not re.fullmatch(r"https?://[^\s@]+", v):
            raise ValueError("base_url must be an http(s) URL without credentials")
        return v

    @field_validator("models")
    @classmethod
    def _models(cls, v: list[str]) -> list[str]:
        for m in v:
            if not m or len(m) > 200 or any(c.isspace() for c in m):
                raise ValueError(f"invalid model id {m!r}")
        return v

    def check(self, name: str) -> None:
        need = {"gateway": ("base_url",), "vertex": ("region", "project"),
                "bedrock": ("region",), "foundry": (), "anthropic": ()}[self.kind]
        missing = [f for f in need if not getattr(self, f)]
        if self.kind == "foundry" and not (self.resource or self.base_url):
            missing.append("resource or base_url")
        if missing:
            raise ProviderError(msg("provider.missing", "provider {name}: {kind} needs {fields}",
                                    name=repr(name), kind=self.kind,
                                    fields=", ".join(missing)))

    @property
    def is_priced(self) -> bool:
        return self.priced if self.priced is not None else self.kind != "gateway"


def providers_file() -> Path:
    return Path(os.getenv("PROVIDERS_FILE", "providers.yaml"))


def provider_files() -> list[tuple[str, Path]]:
    from .extensions import LOADED, extension_root
    return [("local", providers_file()),
            *[(n, extension_root(n) / "providers.yaml") for n in LOADED.names]]


def _read(path: Path) -> dict[str, Provider]:
    if not path.is_file():
        return {}
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    out: dict[str, Provider] = {}
    for name, data in (raw.get("providers") or {}).items():
        if not NAME.fullmatch(str(name)):
            raise ProviderError(msg("provider.name", "provider {name}: invalid name",
                                    name=repr(name)))
        try:
            p = Provider.model_validate(data)
        except ValidationError as e:
            raise ProviderError(msg("provider.invalid", "provider {name}: {error}",
                                    name=repr(name), error=str(e))) from e
        p.check(name)
        out[str(name)] = p
    return out


def load() -> dict[str, Provider]:
    """All providers of this machine: the local file, then the extensions (first one wins)."""
    return {n: p for n, (p, _) in load_with_location().items()}


def load_with_location() -> dict[str, tuple[Provider, str]]:
    """name → (provider, "local" or the extension it comes from)."""
    out: dict[str, tuple[Provider, str]] = {}
    for where, path in provider_files():
        for name, p in _read(path).items():
            out.setdefault(name, (p, where))
    return out


def save(name: str, p: Provider) -> None:
    """Adds or replaces a provider in this machine's providers.yaml (an extension's file is
    shared through the extension: it is edited there). No secret can be in it (`key`)."""
    if not NAME.fullmatch(name):
        raise ProviderError(msg("provider.name", "provider {name}: invalid name", name=repr(name)))
    p.check(name)
    found = load_with_location()
    if name in found and found[name][1] != "local":
        raise ProviderError(msg("provider.shared",
                                "provider {name} comes from the extension {where}: change it there",
                                name=repr(name), where=found[name][1]))
    path = providers_file()
    own = _read(path)
    own[name] = p
    _write(path, own)


def delete(name: str) -> None:
    path = providers_file()
    own = _read(path)
    gone = own.pop(name, None)
    if gone is None:
        raise ProviderError(msg("provider.not_local",
                                "provider {name} is not in this machine's providers.yaml",
                                name=repr(name)))
    if gone.key == "keyring":
        delete_key(name)
    _write(path, own)


def delete_key(name: str) -> None:
    import contextlib

    from .connections.store import SERVICE, _keyring
    with contextlib.suppress(Exception):  # already gone, or no keyring backend
        _keyring().delete_password(SERVICE, _keyring_name(name))


def _write(path: Path, found: dict[str, Provider]) -> None:
    data = {"providers": {n: {"kind": p.kind,
                              **p.model_dump(mode="json", exclude_none=True,
                                             exclude_defaults=True, exclude={"kind"})}
                          for n, p in sorted(found.items())}}
    head = ("# Model providers on this machine (agent-flow-studio). No secrets: a key is only "
            "${VAR} or 'keyring'.\n")
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(head + yaml.safe_dump(data, allow_unicode=True, sort_keys=False),
                   encoding="utf-8")
    os.replace(tmp, path)


def split(ref: str | None, known: dict[str, Provider] | None = None
          ) -> tuple[str | None, str | None]:
    """`provider/model` → (provider, model); a model with no provider prefix → (None, model).
    A prefix that looks like a provider name but is not set up raises (a typo, or a flow from
    another machine), so it never runs silently on the default sign-in."""
    if not ref or "/" not in ref:
        return None, ref
    head, rest = ref.split("/", 1)
    if not NAME.fullmatch(head):  # e.g. a Bedrock ARN: arn:aws:bedrock:…/…
        return None, ref
    known = load() if known is None else known
    if head not in known:
        raise ProviderError(msg("provider.unknown",
                                "model {ref}: provider {name} is not set up on this machine "
                                "(providers.yaml)", ref=repr(ref), name=repr(head)))
    if not rest:
        raise ProviderError(msg("provider.no_model", "model {ref}: no model after the provider",
                                ref=repr(ref)))
    return head, rest


# --- secrets: only ${VAR} or the keyring ---------------------------------------------------------
def _keyring_name(name: str) -> str:
    return f"provider:{name}/{KEY_FIELD}"


def get_key(name: str, p: Provider) -> str | None:
    if p.key is None:
        return None
    if p.key == "keyring":
        from .connections.store import SERVICE, _keyring
        return _keyring().get_password(SERVICE, _keyring_name(name))
    found = _REF.fullmatch(p.key)
    return os.getenv(found.group(1)) if found else None


def set_key(name: str, value: str) -> None:
    from .connections.store import SERVICE, _keyring
    _keyring().set_password(SERVICE, _keyring_name(name), value)


def key_status(name: str, p: Provider) -> str:
    """'none' (no key needed), 'set' or 'missing' — never the value."""
    if p.key is None:
        return "none"
    try:
        return "set" if get_key(name, p) else "missing"
    except Exception:  # noqa: BLE001 — no keyring backend
        return "missing"


# --- the session environment ---------------------------------------------------------------------
def session_env(name: str, p: Provider, model: str, *, resolve_secrets: bool = True
                ) -> dict[str, str]:
    """The environment of a session on provider `name` (merged by the SDK over the studio's
    own). Secrets are read only with `resolve_secrets` (validation never reads them)."""
    env = dict.fromkeys(_MANAGED, "")
    key = ""
    if p.key is not None and resolve_secrets:
        key = get_key(name, p) or ""
        if not key:
            where = "the keyring" if p.key == "keyring" else p.key
            raise ProviderError(msg("provider.no_key",
                                    "provider {name}: its key is not set ({where})",
                                    name=repr(name), where=where))
    if p.kind == "anthropic":
        env["ANTHROPIC_API_KEY"] = key
        env["ANTHROPIC_BASE_URL"] = p.base_url or ""
    elif p.kind == "gateway":
        env["ANTHROPIC_BASE_URL"] = p.base_url or ""
        env["ANTHROPIC_AUTH_TOKEN"] = key
        # every alias and background call stays on the gateway, on a model it has
        env.update(dict.fromkeys(_ALIASES, p.small_model or model))
        env["ANTHROPIC_DEFAULT_SONNET_MODEL"] = env["ANTHROPIC_DEFAULT_OPUS_MODEL"] = model
    elif p.kind == "bedrock":
        env["CLAUDE_CODE_USE_BEDROCK"] = "1"
        env["AWS_REGION"] = p.region or ""
        if p.aws_profile:
            env["AWS_PROFILE"] = p.aws_profile
        if p.base_url:
            env["ANTHROPIC_BEDROCK_BASE_URL"] = p.base_url
    elif p.kind == "vertex":
        env["CLAUDE_CODE_USE_VERTEX"] = "1"
        env["CLOUD_ML_REGION"] = p.region or ""
        env["ANTHROPIC_VERTEX_PROJECT_ID"] = p.project or ""
        if p.base_url:
            env["ANTHROPIC_VERTEX_BASE_URL"] = p.base_url
    elif p.kind == "foundry":
        env["CLAUDE_CODE_USE_FOUNDRY"] = "1"
        if p.resource:
            env["ANTHROPIC_FOUNDRY_RESOURCE"] = p.resource
        if p.base_url:
            env["ANTHROPIC_FOUNDRY_BASE_URL"] = p.base_url
        env["ANTHROPIC_FOUNDRY_API_KEY"] = key  # blank: Microsoft Entra ID sign-in
    if p.kind != "gateway" and p.small_model:
        env["ANTHROPIC_DEFAULT_HAIKU_MODEL"] = env["ANTHROPIC_SMALL_FAST_MODEL"] = p.small_model
    return env


async def test(name: str, p: Provider) -> dict[str, Any]:
    """Checks that the provider answers with its key: the model list of a gateway or of the
    Anthropic API (nothing is generated, so nothing is spent). Cloud kinds sign in through the
    CLI and their own tools: they are checked when a session starts."""
    import httpx
    if p.kind not in ("gateway", "anthropic"):
        return {"ok": None, "message": "checked when a session starts (cloud sign-in)"}
    key = get_key(name, p) if p.key else None
    if p.key and not key:
        return {"ok": False, "message": "the key is not set"}
    base = (p.base_url or "https://api.anthropic.com").rstrip("/")
    headers = {"anthropic-version": "2023-06-01"}
    if key and p.kind == "gateway":
        headers["authorization"] = f"Bearer {key}"
    elif key:
        headers["x-api-key"] = key
    try:
        async with httpx.AsyncClient(timeout=15, follow_redirects=False,
                                     transport=_transport) as c:
            r = await c.get(f"{base}/v1/models", headers=headers)
    except httpx.HTTPError as e:
        return {"ok": False, "message": f"no answer from {base}: {type(e).__name__}"}
    if r.status_code != 200:
        return {"ok": False, "message": f"{base} answered HTTP {r.status_code}"}
    try:
        ids = [str(m.get("id")) for m in (r.json().get("data") or []) if isinstance(m, dict)]
    except ValueError:
        ids = []
    return {"ok": True, "message": f"{base} answered", "models": ids[:200]}


_transport: Any = None  # tests: httpx.MockTransport


def detail() -> dict[str, Any]:
    """Everything the Providers window shows (no secret: only where the key is and if it is set)."""
    return {n: p.model_dump(mode="json") | {"location": where, "key_status": key_status(n, p)}
            for n, (p, where) in load_with_location().items()}


def summary() -> list[dict[str, Any]]:
    """For the editor and the CLI: no secrets, only whether the key is there."""
    try:
        found = load()
    except ProviderError as e:
        return [{"name": "", "error": str(e)}]
    return [{"name": n, "kind": p.kind, "title": p.title or n, "models": p.models,
             "key": key_status(n, p), "priced": p.is_priced} for n, p in found.items()]

"""Model providers (providers.yaml): list, save, delete, the key, a check."""
from __future__ import annotations

from typing import Any

from fastapi import Depends, FastAPI, HTTPException
from pydantic import ValidationError

from ... import providers
from .common import Ctx, SecretBody


def register(app: FastAPI, ctx: Ctx) -> None:
    csrf = ctx.csrf
    # --- model providers (providers.yaml: no secrets; keys in env vars or the keyring) --------
    @app.get("/api/providers")
    def providers_list() -> dict[str, Any]:
        out = {"file": str(providers.providers_file()), "kinds": list(providers.KINDS)}
        try:
            return out | {"providers": providers.detail()}
        except providers.ProviderError as e:
            return out | {"providers": {}, "error": str(e)}

    @app.put("/api/providers/{name}", dependencies=[Depends(csrf)])
    def provider_save(name: str, body: dict[str, Any]) -> dict[str, Any]:
        data = {k: v for k, v in body.items() if k not in ("location", "key_status")}
        try:
            providers.save(name, providers.Provider.model_validate(data))
        except ValidationError as e:
            raise HTTPException(400, "; ".join(f"{'.'.join(map(str, x['loc']))}: {x['msg']}"
                                               for x in e.errors())) from e
        except providers.ProviderError as e:
            raise HTTPException(400, str(e)) from e
        return providers_list()

    @app.delete("/api/providers/{name}", dependencies=[Depends(csrf)])
    def provider_delete(name: str) -> dict[str, Any]:
        try:
            providers.delete(name)
        except providers.ProviderError as e:
            raise HTTPException(400, str(e)) from e
        return providers_list()

    @app.put("/api/providers/{name}/key", dependencies=[Depends(csrf)])
    def provider_key(name: str, body: SecretBody) -> dict[str, Any]:
        """The key goes to the keyring (`value`) or is read from an env var (`env`); the file
        only gets the reference. The value is never returned."""
        found = providers.load_with_location()
        if name not in found or found[name][1] != "local":
            raise HTTPException(404, "provider is not in this machine's providers.yaml")
        if bool(body.value) == bool(body.env):
            raise HTTPException(400, "give 'value' (stored in the keyring) or 'env'")
        if body.value:
            providers.set_key(name, body.value)
        elif found[name][0].key == "keyring":  # now from an env var: no copy left behind
            providers.delete_key(name)
        ref = "keyring" if body.value else "${" + str(body.env) + "}"
        try:
            providers.save(name, found[name][0].model_copy(update={"key": ref}))
        except providers.ProviderError as e:
            raise HTTPException(400, str(e)) from e
        return providers_list()

    @app.post("/api/providers/{name}/test", dependencies=[Depends(csrf)])
    async def provider_test(name: str) -> dict[str, Any]:
        found = providers.load()
        if name not in found:
            raise HTTPException(404, "provider does not exist")
        return await providers.test(name, found[name])

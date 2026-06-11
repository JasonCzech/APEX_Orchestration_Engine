"""Env-backed secrets adapter (provider "env"): resolves "env:NAME" from os.environ.

Vault/manager-backed providers land M4+. Raw secret values must never be logged
or persisted — SecretValue redacts repr/str.
"""

import os

from apex.adapters.registry import AdapterRegistry, ConnectionConfig, PortKind
from apex.domain.integrations import SecretValue


@AdapterRegistry.register(PortKind.SECRETS, "env")
class EnvSecretsAdapter:
    def __init__(
        self, conn: ConnectionConfig | None = None, secret: SecretValue | None = None
    ) -> None:
        self._conn = conn

    async def resolve(self, secret_ref: str) -> SecretValue:
        scheme, _, name = secret_ref.partition(":")
        if scheme != "env" or not name:
            raise ValueError(f"unsupported secret_ref {secret_ref!r}; expected 'env:NAME'")
        try:
            return SecretValue(value=os.environ[name])
        except KeyError:
            raise KeyError(
                f"environment variable {name!r} is not set (secret_ref {secret_ref!r})"
            ) from None

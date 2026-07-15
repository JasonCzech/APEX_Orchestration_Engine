"""Env-backed secrets adapter (provider "env"): resolves "env:NAME" from os.environ.

Vault/manager-backed providers land M4+. Raw secret values must never be logged
or persisted — SecretValue redacts repr/str.
"""

import os

from apex.adapters.registry import AdapterRegistry, ConnectionConfig, PortKind
from apex.domain.integrations import SecretValue
from apex.services.connection_credentials import validate_secret_ref
from apex.settings import get_settings


@AdapterRegistry.register(PortKind.SECRETS, "env")
class EnvSecretsAdapter:
    def __init__(
        self, conn: ConnectionConfig | None = None, secret: SecretValue | None = None
    ) -> None:
        self._conn = conn

    async def resolve(self, secret_ref: str) -> SecretValue:
        try:
            validate_secret_ref(secret_ref)
        except ValueError as exc:
            raise ValueError(f"unsupported secret_ref {secret_ref!r}; expected 'env:NAME'") from exc
        _, _, name = secret_ref.partition(":")
        prefixes = tuple(get_settings().env_secret_prefixes)
        if prefixes and not name.startswith(prefixes):
            raise ValueError(f"environment variable {name!r} is not allowed by env_secret_prefixes")
        try:
            return SecretValue(value=os.environ[name])
        except KeyError:
            raise KeyError(
                f"environment variable {name!r} is not set (secret_ref {secret_ref!r})"
            ) from None

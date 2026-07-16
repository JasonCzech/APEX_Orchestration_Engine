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
        invalid_ref = False
        try:
            validate_secret_ref(secret_ref)
        except ValueError:
            invalid_ref = True
        if invalid_ref:
            # Invalid references may themselves be raw credentials. Do not
            # reflect the value or retain the validator exception in the chain.
            raise ValueError("unsupported secret_ref; expected 'env:NAME'")
        _, _, name = secret_ref.partition(":")
        prefixes = tuple(get_settings().env_secret_prefixes)
        if prefixes and not name.startswith(prefixes):
            raise ValueError(f"environment variable {name!r} is not allowed by env_secret_prefixes")
        if name not in os.environ:
            raise KeyError(f"environment variable {name!r} is not set (secret_ref {secret_ref!r})")
        return SecretValue(value=os.environ[name])

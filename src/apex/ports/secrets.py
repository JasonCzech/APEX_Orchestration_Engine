"""Secrets port. secret_ref grammar: "<scheme>:<locator>", e.g. "env:NAME"."""

from typing import Protocol, runtime_checkable

from apex.domain.integrations import SecretValue


@runtime_checkable
class SecretsPort(Protocol):
    async def resolve(self, secret_ref: str) -> SecretValue: ...

"""(port kind, provider) -> adapter factory registry (ADR-0002).

Adapters self-register at import time via @AdapterRegistry.register — importing
apex.adapters.stubs / apex.adapters.sim_engine loads the built-ins. A factory is
any callable (ConnectionConfig, SecretValue | None) -> adapter, including an
adapter class whose __init__ matches that shape.
"""

from collections.abc import Callable
from enum import StrEnum
from typing import Any, ClassVar

from pydantic import BaseModel, Field

from apex.domain.integrations import SecretValue
from apex.ports.secrets import SecretsPort


class PortKind(StrEnum):
    WORK_TRACKING = "work_tracking"
    LOG_SEARCH = "log_search"
    OBSERVABILITY = "observability"
    DOCUMENTS = "documents"
    CLUSTER_INVENTORY = "cluster_inventory"
    SOURCE_CONTROL = "source_control"
    EXECUTION_ENGINE = "execution_engine"
    ARTIFACT_STORE = "artifact_store"
    SECRETS = "secrets"


class ConnectionConfig(BaseModel):
    """Static in M1; becomes the `connections` table row (DB-backed CRUD) in M2."""

    id: str
    kind: PortKind
    provider: str
    name: str
    options: dict[str, Any] = Field(default_factory=dict)
    secret_ref: str | None = None


AdapterFactory = Callable[[ConnectionConfig, SecretValue | None], Any]


class AdapterRegistry:
    _factories: ClassVar[dict[tuple[PortKind, str], AdapterFactory]] = {}

    @classmethod
    def register[F: AdapterFactory](cls, kind: PortKind, provider: str) -> Callable[[F], F]:
        """Decorator: register `factory` for (kind, provider). Returns it unchanged."""

        def decorator(factory: F) -> F:
            cls._factories[(kind, provider)] = factory
            return factory

        return decorator

    @classmethod
    def providers_for(cls, kind: PortKind) -> list[str]:
        return sorted(provider for k, provider in cls._factories if k == kind)

    @classmethod
    async def build(cls, conn: ConnectionConfig, secrets: SecretsPort | None = None) -> Any:
        """Resolve conn.secret_ref (if any) through `secrets`, then build the adapter."""
        try:
            factory = cls._factories[(conn.kind, conn.provider)]
        except KeyError:
            registered = cls.providers_for(conn.kind)
            hint = ", ".join(registered) if registered else "none"
            raise KeyError(
                f"no adapter registered for kind={conn.kind.value!r} "
                f"provider={conn.provider!r}; registered providers for this kind: {hint} "
                "(did you import apex.adapters.stubs / apex.adapters.sim_engine?)"
            ) from None

        secret: SecretValue | None = None
        if conn.secret_ref is not None:
            if secrets is None:
                raise ValueError(
                    f"connection {conn.id!r} has secret_ref={conn.secret_ref!r} "
                    "but no secrets port was provided"
                )
            secret = await secrets.resolve(conn.secret_ref)
        return factory(conn, secret)

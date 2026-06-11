"""Stub cluster inventory: fixed three-service snapshot (timestamp pinned for determinism)."""

from apex.adapters.registry import AdapterRegistry, ConnectionConfig, PortKind
from apex.domain.integrations import EnvironmentSnapshot, EnvRef, SecretValue, ServiceInfo

_SCANNED_AT = "2026-01-01T00:00:00+00:00"

_SERVICES: tuple[ServiceInfo, ...] = (
    ServiceInfo(name="checkout-api", replicas=3, image="registry.stub.local/checkout-api:2.7.1"),
    ServiceInfo(name="payment-svc", replicas=2, image="registry.stub.local/payment-svc:2.14.0"),
    ServiceInfo(name="cart-svc", replicas=2, image="registry.stub.local/cart-svc:1.9.3"),
)


@AdapterRegistry.register(PortKind.CLUSTER_INVENTORY, "stub")
class StubClusterInventoryAdapter:
    def __init__(
        self, conn: ConnectionConfig | None = None, secret: SecretValue | None = None
    ) -> None:
        self._conn = conn

    async def scan_environment(self, env_ref: EnvRef) -> EnvironmentSnapshot:
        return EnvironmentSnapshot(
            services=[s.model_copy(deep=True) for s in _SERVICES], scanned_at=_SCANNED_AT
        )

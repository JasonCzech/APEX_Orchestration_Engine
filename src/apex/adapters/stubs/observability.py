"""Stub observability: fixed metric series + healthy service health."""

from apex.adapters.registry import AdapterRegistry, ConnectionConfig, PortKind
from apex.domain.integrations import (
    MetricPoint,
    MetricQuery,
    MetricSeries,
    SecretValue,
    ServiceHealth,
    TimeWindow,
)

_POINTS: tuple[MetricPoint, ...] = tuple(
    MetricPoint(at=f"2026-01-01T00:0{i}:00+00:00", value=value)
    for i, value in enumerate((212.0, 218.0, 231.0, 224.0, 219.0))
)


@AdapterRegistry.register(PortKind.OBSERVABILITY, "stub")
class StubObservabilityAdapter:
    def __init__(
        self, conn: ConnectionConfig | None = None, secret: SecretValue | None = None
    ) -> None:
        self._conn = conn

    async def query_metrics(self, query: MetricQuery, *, window: TimeWindow) -> MetricSeries:
        return MetricSeries(name=query.query, points=[p.model_copy(deep=True) for p in _POINTS])

    async def get_service_health(self, service: str, *, window: TimeWindow) -> ServiceHealth:
        return ServiceHealth(
            service=service,
            healthy=True,
            status="healthy",
            indicators={"error_rate": 0.002, "p95_ms": 219.0, "cpu_pct": 41.0},
        )

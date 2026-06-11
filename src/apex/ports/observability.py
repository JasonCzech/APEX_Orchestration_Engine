"""Observability port (provider TBD pre-M4 — plan risk #9; stub until then)."""

from typing import Protocol, runtime_checkable

from apex.domain.integrations import MetricQuery, MetricSeries, ServiceHealth, TimeWindow


@runtime_checkable
class ObservabilityPort(Protocol):
    async def query_metrics(self, query: MetricQuery, *, window: TimeWindow) -> MetricSeries: ...

    async def get_service_health(self, service: str, *, window: TimeWindow) -> ServiceHealth: ...

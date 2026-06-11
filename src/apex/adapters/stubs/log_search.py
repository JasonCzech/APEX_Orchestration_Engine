"""Stub log search: three canned entries matching the PHX-241 demo narrative."""

from apex.adapters.registry import AdapterRegistry, ConnectionConfig, PortKind
from apex.domain.integrations import (
    LogEntry,
    LogQuery,
    LogSearchResult,
    Page,
    SecretValue,
    TimeWindow,
)

_ENTRIES: tuple[LogEntry, ...] = (
    LogEntry(
        at="2026-01-01T00:00:01+00:00",
        level="ERROR",
        service="payment-svc",
        message="upstream timeout after 800ms calling card-gateway (attempt 2/3)",
    ),
    LogEntry(
        at="2026-01-01T00:00:02+00:00",
        level="WARN",
        service="checkout-api",
        message="latency budget exceeded for POST /checkout: p95=870ms (budget 250ms)",
    ),
    LogEntry(
        at="2026-01-01T00:00:03+00:00",
        level="INFO",
        service="payment-svc",
        message="connection pool exhausted; queueing request (pool_size=20)",
    ),
)


@AdapterRegistry.register(PortKind.LOG_SEARCH, "stub")
class StubLogSearchAdapter:
    def __init__(
        self, conn: ConnectionConfig | None = None, secret: SecretValue | None = None
    ) -> None:
        self._conn = conn

    async def search(self, query: LogQuery, *, window: TimeWindow, page: Page) -> LogSearchResult:
        entries = [e.model_copy(deep=True) for e in _ENTRIES]
        return LogSearchResult(
            entries=entries[page.offset : page.offset + page.limit], total=len(_ENTRIES)
        )

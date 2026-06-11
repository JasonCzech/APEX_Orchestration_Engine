"""Log-search port (ELK / stub)."""

from typing import Protocol, runtime_checkable

from apex.domain.integrations import LogQuery, LogSearchResult, Page, TimeWindow


@runtime_checkable
class LogSearchPort(Protocol):
    async def search(
        self, query: LogQuery, *, window: TimeWindow, page: Page
    ) -> LogSearchResult: ...

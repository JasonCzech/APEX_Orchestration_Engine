"""Shared /logs plumbing: time-window defaulting/validation + adapter resolution.

Conventions (also documented on the router):
- A request without a window means "the last hour", computed server-side at
  request time; the router echoes the effective window back to the caller.
- A partially-bounded window receives a deterministic bound so an API request
  can never trigger an open-ended provider scan.
- LogQuery.filters entries become ANDed term filters. The "thread_id" key is
  reserved by convention for pipeline-run deep links: pipeline runs will tag
  their log lines with the thread id in a later milestone, so the dashboard's
  GET ?thread=... deep link rides on POST /logs/search with
  filters={"thread_id": <thread>}.
"""

from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import Any

from apex.adapters.registry import PortKind
from apex.domain.integrations import TimeWindow
from apex.services.connections import get_connection_resolver

DEFAULT_WINDOW = timedelta(hours=1)
MAX_WINDOW = timedelta(days=31)

# (connection_id, project_id) -> LogSearchPort adapter; the router depends on
# this callable shape so tests can override resolution wholesale.
LogSearchResolver = Callable[[str | None, str | None], Awaitable[Any]]


def parse_bound(name: str, value: str | None) -> datetime | None:
    """ISO-8601 bound or None; never reflect the caller's value in errors."""
    if value is None:
        return None
    try:
        return datetime.fromisoformat(value)
    except (OverflowError, ValueError):
        raise ValueError(f"{name} must be an ISO-8601 timestamp") from None


def _comparable(at: datetime) -> datetime:
    """Naive bounds are treated as UTC so mixed naive/aware windows compare."""
    return at if at.tzinfo is not None else at.replace(tzinfo=UTC)


def effective_window(
    start: str | None = None, end: str | None = None, *, now: datetime | None = None
) -> TimeWindow:
    """Return a finite, validated provider window.

    Missing windows default to the last hour. A lone end receives a one-hour
    lookback; a lone start is bounded by ``now``. Explicit or derived windows
    wider than ``MAX_WINDOW`` are rejected before adapter resolution.
    """
    anchor = now if now is not None else datetime.now(UTC)
    if start is None and end is None:
        try:
            default_start = anchor - DEFAULT_WINDOW
        except OverflowError:
            raise ValueError("log search window is outside the supported datetime range") from None
        return TimeWindow(start=default_start.isoformat(), end=anchor.isoformat())
    start_at = parse_bound("window.from", start)
    end_at = parse_bound("window.to", end)
    if start_at is None:
        if end_at is None:  # defensive: handled by the default case above
            raise ValueError("log search window is missing both bounds")
        try:
            start_at = end_at - DEFAULT_WINDOW
        except OverflowError:
            raise ValueError("log search window is outside the supported datetime range") from None
        start = start_at.isoformat()
    if end_at is None:
        end_at = anchor
        end = end_at.isoformat()
    comparable_start = _comparable(start_at)
    comparable_end = _comparable(end_at)
    try:
        if comparable_start > comparable_end:
            raise ValueError("window.from must be earlier than or equal to window.to")
        if comparable_end - comparable_start > MAX_WINDOW:
            raise ValueError(f"log search window must not exceed {MAX_WINDOW.days} days")
    except OverflowError:
        raise ValueError("log search window is outside the supported datetime range") from None
    return TimeWindow(start=start, end=end)


async def resolve_log_search_adapter(
    connection_id: str | None = None, project_id: str | None = None
) -> Any:
    """LOG_SEARCH adapter via the connection resolver (explicit connection_id >
    project-scoped row > global row > static stub fallback)."""
    return await get_connection_resolver().resolve(
        PortKind.LOG_SEARCH, connection_id=connection_id, project_id=project_id
    )

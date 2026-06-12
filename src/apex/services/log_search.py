"""Shared /logs plumbing: time-window defaulting/validation + adapter resolution.

Conventions (also documented on the router):
- A request without a window means "the last hour", computed server-side at
  request time; the router echoes the effective window back to the caller.
- A partially-bounded window leaves the other side open (unbounded).
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

# (connection_id, project_id) -> LogSearchPort adapter; the router depends on
# this callable shape so tests can override resolution wholesale.
LogSearchResolver = Callable[[str | None, str | None], Awaitable[Any]]


def parse_bound(name: str, value: str | None) -> datetime | None:
    """ISO-8601 bound or None; raises ValueError with the offending field name."""
    if value is None:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        raise ValueError(f"{name} must be an ISO-8601 timestamp, got {value!r}") from None


def _comparable(at: datetime) -> datetime:
    """Naive bounds are treated as UTC so mixed naive/aware windows compare."""
    return at if at.tzinfo is not None else at.replace(tzinfo=UTC)


def effective_window(
    start: str | None = None, end: str | None = None, *, now: datetime | None = None
) -> TimeWindow:
    """Validated window; both bounds missing -> the last hour ending at `now`."""
    if start is None and end is None:
        anchor = now if now is not None else datetime.now(UTC)
        return TimeWindow(start=(anchor - DEFAULT_WINDOW).isoformat(), end=anchor.isoformat())
    start_at = parse_bound("window.from", start)
    end_at = parse_bound("window.to", end)
    if start_at is not None and end_at is not None and _comparable(start_at) > _comparable(end_at):
        raise ValueError(f"window.from {start!r} is after window.to {end!r}")
    return TimeWindow(start=start, end=end)


async def resolve_log_search_adapter(
    connection_id: str | None = None, project_id: str | None = None
) -> Any:
    """LOG_SEARCH adapter via the connection resolver (explicit connection_id >
    project-scoped row > global row > static stub fallback)."""
    return await get_connection_resolver().resolve(
        PortKind.LOG_SEARCH, connection_id=connection_id, project_id=project_id
    )

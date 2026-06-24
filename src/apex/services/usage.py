"""Usage-analytics events: best-effort writers, /v1 middleware, and aggregation.

Write paths (both modeled on apex.services.engine_runs — throwaway NullPool engine,
swallow-and-log, never raises):

* `/v1` requests: `UsageTrackingMiddleware` (pure ASGI, registered in apex.app.http)
  times each request and, after the response is sent, schedules a fire-and-forget
  asyncio task. The identity the route handler resolved is not reachable
  post-response without threading state through every dependency, so consumer
  attribution is resolved LAZILY inside that task: re-extract the API key header
  and run it through the shared IdentityResolver. Accepted tradeoffs (analytics,
  not billing):
    - the resolver may run twice per request (the dev key has a no-DB fast path;
      the DB path bumps `last_used_at` a second time);
    - when resolution fails (key revoked mid-flight, DB down) the event records a
      sha256 key fingerprint (`key:<12 hex>`) instead of a name;
    - fire-and-forget means an event scheduled just before process shutdown can
      be lost, and the request never waits on the analytics write.
* graph nodes: `record_phase_usage_sync`, a sync bridge called from the phase
  finalize node (apex.graphs.pipeline.phase_subgraph) — one event per phase
  terminal status, surface "graph", action `phase:<phase>:<status>`.

Read path: `UsageAnalyticsRepository` runs the Postgres aggregation (date_trunc
buckets) on a request-scoped session for GET /v1/analytics/usage.
"""

import asyncio
import time
from collections.abc import Mapping
from datetime import datetime
from decimal import Decimal
from typing import Any
from urllib.parse import parse_qs

import structlog
from sqlalchemy import ColumnElement, case, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from apex.auth.service import extract_api_key, get_default_resolver, hash_api_key
from apex.graphs.pipeline.configurable import PipelineConfigurable
from apex.persistence.models import AgentEvent, UsageEvent
from apex.services.pricing import compute_cost
from apex.settings import get_settings

logger = structlog.get_logger(__name__)

SURFACE_V1 = "v1"
SURFACE_GRAPH = "graph"

# Phase terminal statuses that count as "ok" (everything else terminal is "error").
_OK_PHASE_STATUSES = ("succeeded", "skipped")

# Strong references to in-flight fire-and-forget writes (loops may GC bare tasks).
_PENDING: set[asyncio.Task[None]] = set()


# ── Best-effort writers ──────────────────────────────────────────────────────


async def record_usage_event(
    *,
    consumer_name: str,
    surface: str,
    action: str,
    status: str = "ok",
    project_id: str | None = None,
    thread_id: str | None = None,
    duration_ms: int | None = None,
    extra: dict[str, Any] | None = None,
) -> None:
    """Insert one usage event; never raises (mirrors services.engine_runs)."""
    try:
        # Throwaway engine per call: callers include graph worker threads with
        # short-lived event loops, so pooled connections must not outlive them.
        engine_db = create_async_engine(get_settings().database.uri, poolclass=NullPool)
        try:
            session_factory = async_sessionmaker(engine_db, expire_on_commit=False)
            await _insert_usage_event(
                session_factory,
                consumer_name=consumer_name,
                surface=surface,
                action=action,
                status=status,
                project_id=project_id,
                thread_id=thread_id,
                duration_ms=duration_ms,
                extra=extra,
            )
        finally:
            await engine_db.dispose()
    except Exception as exc:  # noqa: BLE001 — analytics never fails a request or run
        logger.warning("usage.record_failed", action=action, error=str(exc))


async def _insert_usage_event(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    consumer_name: str,
    surface: str,
    action: str,
    status: str = "ok",
    project_id: str | None = None,
    thread_id: str | None = None,
    duration_ms: int | None = None,
    extra: dict[str, Any] | None = None,
) -> None:
    async with session_factory() as session:
        session.add(
            UsageEvent(
                consumer_name=consumer_name,
                project_id=project_id,
                surface=surface,
                action=action,
                thread_id=thread_id,
                duration_ms=duration_ms,
                status=status,
                extra=extra or {},
            )
        )
        await session.commit()


def record_usage_event_sync(**kwargs: Any) -> None:
    """Sync bridge for graph nodes (which run sync on worker threads)."""
    try:
        asyncio.run(record_usage_event(**kwargs))
    except Exception as exc:  # noqa: BLE001
        logger.warning("usage.record_failed", error=str(exc))


def record_phase_usage_sync(phase: str, status: str, config: Any) -> None:
    """One graph-surface event per phase terminal status; never raises.

    Called from the phase finalize node. Consumer attribution comes from the
    LangGraph auth context (configurable.langgraph_auth_user), falling back to
    "graph" for unauthenticated/local runs.
    """
    try:
        configurable: dict[str, Any] = dict((config or {}).get("configurable") or {})
        user = configurable.get("langgraph_auth_user")
        identity = (
            user.get("identity") if isinstance(user, dict) else getattr(user, "identity", None)
        )
        thread_id = configurable.get("thread_id")
        project_id = configurable.get("project_id")
        record_usage_event_sync(
            consumer_name=str(identity) if identity else "graph",
            surface=SURFACE_GRAPH,
            action=f"phase:{phase}:{status}",
            status="ok" if status in _OK_PHASE_STATUSES else "error",
            project_id=str(project_id) if project_id else None,
            thread_id=str(thread_id) if thread_id else None,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("usage.phase_record_failed", phase=phase, error=str(exc))


def _usage_int(value: Any) -> int:
    if value is None:
        return 0
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return 0


def _usage_detail_int(details: Mapping[str, Any], *keys: str) -> int:
    for key in keys:
        if key in details:
            return _usage_int(details.get(key))
    return 0


def normalize_usage_metadata(usage: Mapping[str, Any] | None) -> dict[str, int]:
    """Flatten LangChain AIMessage.usage_metadata into durable token columns."""
    usage = usage or {}
    input_details = usage.get("input_token_details")
    output_details = usage.get("output_token_details")
    input_details = input_details if isinstance(input_details, Mapping) else {}
    output_details = output_details if isinstance(output_details, Mapping) else {}
    input_tokens = _usage_int(usage.get("input_tokens"))
    output_tokens = _usage_int(usage.get("output_tokens"))
    total_tokens = _usage_int(usage.get("total_tokens")) or input_tokens + output_tokens
    return {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "total_tokens": total_tokens,
        "cache_read_tokens": _usage_detail_int(
            input_details, "cache_read", "cache_read_tokens", "cache_read_input_tokens"
        ),
        "cache_creation_tokens": _usage_detail_int(
            input_details,
            "cache_creation",
            "cache_creation_tokens",
            "cache_creation_input_tokens",
            "cache_write",
        ),
        "reasoning_tokens": _usage_detail_int(
            output_details, "reasoning", "reasoning_tokens", "reasoning_output_tokens"
        ),
    }


def _provider_from(model: str | None, usage: Mapping[str, Any] | None) -> str | None:
    raw = (usage or {}).get("provider") or (usage or {}).get("ls_provider")
    if raw:
        return str(raw)
    if model and ":" in model:
        return model.split(":", 1)[0]
    if model and "/" in model:
        return model.split("/", 1)[0]
    if model and model.startswith("claude-"):
        return "anthropic"
    if model and model.startswith("gpt-"):
        return "openai"
    return None


async def _insert_agent_event(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    thread_id: str | None,
    project_id: str | None,
    phase: str,
    agent_name: str,
    model: str | None,
    provider: str | None,
    attempt: int | None,
    status: str,
    input_tokens: int,
    output_tokens: int,
    total_tokens: int,
    cache_read_tokens: int,
    cache_creation_tokens: int,
    reasoning_tokens: int,
    cost_usd: Decimal | None,
    latency_ms: int | None,
    extra: dict[str, Any] | None = None,
) -> None:
    async with session_factory() as session:
        session.add(
            AgentEvent(
                thread_id=thread_id,
                project_id=project_id,
                phase=phase,
                agent_name=agent_name,
                model=model,
                provider=provider,
                attempt=attempt,
                status=status,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                total_tokens=total_tokens,
                cache_read_tokens=cache_read_tokens,
                cache_creation_tokens=cache_creation_tokens,
                reasoning_tokens=reasoning_tokens,
                cost_usd=cost_usd,
                latency_ms=latency_ms,
                extra=extra or {},
            )
        )
        await session.commit()


async def record_agent_event(
    *,
    thread_id: str | None,
    project_id: str | None,
    phase: str,
    agent_name: str,
    model: str | None,
    provider: str | None,
    attempt: int | None,
    status: str,
    latency_ms: int | None,
    usage: Mapping[str, Any] | None = None,
) -> None:
    """Insert one agent-behavior event; never raises."""
    token_usage = normalize_usage_metadata(usage)
    cost_usd, pricing = compute_cost(model, token_usage)
    extra: dict[str, Any] = {}
    if pricing is not None:
        extra["pricing"] = pricing
    if usage:
        finish_reason = usage.get("finish_reason") or usage.get("stop_reason")
        if finish_reason:
            extra["finish_reason"] = finish_reason
    try:
        engine_db = create_async_engine(get_settings().database.uri, poolclass=NullPool)
        try:
            session_factory = async_sessionmaker(engine_db, expire_on_commit=False)
            await _insert_agent_event(
                session_factory,
                thread_id=thread_id,
                project_id=project_id,
                phase=phase,
                agent_name=agent_name,
                model=model,
                provider=provider,
                attempt=attempt,
                status=status,
                latency_ms=latency_ms,
                cost_usd=cost_usd,
                extra=extra,
                **token_usage,
            )
        finally:
            await engine_db.dispose()
    except Exception as exc:  # noqa: BLE001 — analytics never fails a request or run
        logger.warning("agent_events.record_failed", phase=phase, error=str(exc))


def record_agent_event_sync(
    *,
    phase: str,
    status: str,
    attempt: int | None,
    config: Any,
    latency_ms: int | None,
    usage: Mapping[str, Any] | None = None,
    agent_name: str | None = None,
) -> None:
    """Sync bridge for graph nodes: one row per phase/agent invocation."""
    try:
        configurable: dict[str, Any] = dict((config or {}).get("configurable") or {})
        cfg = PipelineConfigurable.from_config(config)
        model: str | None = None
        for key, value in cfg.model_by_phase.items():
            if str(key) == phase:
                model = value
                break
        asyncio.run(
            record_agent_event(
                thread_id=str(configurable.get("thread_id"))
                if configurable.get("thread_id")
                else None,
                project_id=str(configurable.get("project_id"))
                if configurable.get("project_id")
                else None,
                phase=phase,
                agent_name=agent_name or f"{phase}.worker",
                model=model,
                provider=_provider_from(model, usage),
                attempt=attempt,
                status="ok" if status in _OK_PHASE_STATUSES else "error",
                latency_ms=latency_ms,
                usage=usage,
            )
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("agent_events.record_failed", phase=phase, error=str(exc))


# ── /v1 request middleware ───────────────────────────────────────────────────


async def _resolve_consumer_name(api_key: str | None) -> str:
    """Lazy post-hoc attribution; falls back to a key fingerprint, never raises."""
    try:
        identity = await get_default_resolver().resolve(api_key)
    except Exception:  # noqa: BLE001 — resolver failures must not kill the write task
        identity = None
    if identity is not None:
        return identity.name
    if api_key:
        return f"key:{hash_api_key(api_key)[:12]}"
    return "anonymous"


async def _record_request_event(
    *,
    api_key: str | None,
    action: str,
    status: str,
    duration_ms: int,
    project_id: str | None,
    extra: dict[str, Any],
) -> None:
    consumer_name = await _resolve_consumer_name(api_key)
    try:
        from apex.persistence.db import get_sessionmaker

        await _insert_usage_event(
            get_sessionmaker(),
            consumer_name=consumer_name,
            surface=SURFACE_V1,
            action=action,
            status=status,
            duration_ms=duration_ms,
            project_id=project_id,
            extra=extra,
        )
    except Exception as exc:  # noqa: BLE001 — analytics never fails a request
        logger.warning("usage.record_failed", action=action, error=str(exc))


def _request_project_id(scope: Mapping[str, Any]) -> str | None:
    """Best-effort project attribution: `project_id` path param, else `project` query."""
    value = (scope.get("path_params") or {}).get("project_id")
    if value:
        return str(value)
    query = parse_qs((scope.get("query_string") or b"").decode("latin-1"))
    values = query.get("project")
    return values[0] if values else None


class UsageTrackingMiddleware:
    """Pure-ASGI middleware recording one usage event per matched /v1 operation.

    Only requests that matched a FastAPI route carrying an operation_id are
    recorded (FastAPI puts the matched route in scope["route"]), which naturally
    skips /v1/docs, /v1/openapi.json, and unmatched 404s. status maps "ok" below
    HTTP 400 and "error" at/above; exceptions that escape the app's handlers are
    recorded as 500 and re-raised. The DB write is scheduled fire-and-forget via
    asyncio.create_task — the response is never delayed by analytics (the write
    can be lost on process shutdown; acceptable for usage metrics).
    """

    def __init__(self, app: Any) -> None:
        self._app = app

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return
        started = time.perf_counter()
        status_code = 500

        async def send_wrapper(message: Mapping[str, Any]) -> None:
            nonlocal status_code
            if message["type"] == "http.response.start":
                status_code = int(message["status"])
            await send(message)

        try:
            await self._app(scope, receive, send_wrapper)
        except Exception:
            self._schedule(scope, 500, started)
            raise
        self._schedule(scope, status_code, started)

    def _schedule(self, scope: Mapping[str, Any], status_code: int, started: float) -> None:
        try:
            operation_id = getattr(scope.get("route"), "operation_id", None)
            if not operation_id:
                return  # docs/openapi/unmatched paths carry no contract operation
            duration_ms = int((time.perf_counter() - started) * 1000)
            api_key = extract_api_key(dict(scope.get("headers") or []))
            task = asyncio.get_running_loop().create_task(
                _record_request_event(
                    api_key=api_key,
                    action=str(operation_id),
                    status="ok" if status_code < 400 else "error",
                    duration_ms=duration_ms,
                    project_id=_request_project_id(scope),
                    extra={"status_code": status_code, "path": str(scope.get("path") or "")},
                )
            )
            _PENDING.add(task)
            task.add_done_callback(_PENDING.discard)
        except Exception as exc:  # noqa: BLE001 — analytics never fails a request
            logger.warning("usage.middleware_failed", error=str(exc))


# ── Aggregation (GET /v1/analytics/usage) ────────────────────────────────────


class UsageAnalyticsRepository:
    """Postgres aggregation over usage_events (date_trunc buckets; request session).

    `visible_project_ids`: None means unscoped (no visibility filter); a tuple
    means "rows in these projects, plus project-less rows" — scoped consumers
    see their scoped projects and null-project events.
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def aggregate(
        self,
        *,
        window_from: datetime,
        window_to: datetime,
        bucket: str,
        project_id: str | None = None,
        visible_project_ids: tuple[str, ...] | None = None,
    ) -> dict[str, Any]:
        filters: list[ColumnElement[bool]] = [
            UsageEvent.at >= window_from,
            UsageEvent.at < window_to,
        ]
        if project_id is not None:
            filters.append(UsageEvent.project_id == project_id)
        elif visible_project_ids is not None:
            filters.append(
                or_(
                    UsageEvent.project_id.is_(None),
                    UsageEvent.project_id.in_(visible_project_ids),
                )
            )
        error_count = func.sum(case((UsageEvent.status == "error", 1), else_=0))

        surface_rows = (
            await self._session.execute(
                select(UsageEvent.surface, func.count(), error_count)
                .where(*filters)
                .group_by(UsageEvent.surface)
            )
        ).all()

        bucket_start = func.date_trunc(bucket, UsageEvent.at)
        bucket_rows = (
            await self._session.execute(
                select(bucket_start.label("bucket_start"), func.count(), error_count)
                .where(*filters)
                .group_by(bucket_start)
                .order_by(bucket_start)
            )
        ).all()

        action_rows = (
            await self._session.execute(
                select(UsageEvent.action, func.count())
                .where(*filters)
                .group_by(UsageEvent.action)
                .order_by(func.count().desc(), UsageEvent.action)
                .limit(10)
            )
        ).all()

        runs_row = (
            await self._session.execute(
                select(
                    func.sum(case((UsageEvent.action.like("phase:%:succeeded"), 1), else_=0)),
                    func.sum(case((UsageEvent.action.like("phase:%:failed"), 1), else_=0)),
                ).where(*filters, UsageEvent.surface == SURFACE_GRAPH)
            )
        ).one()

        return {
            "totals": {
                "events": sum(int(count) for _surface, count, _errors in surface_rows),
                "errors": sum(int(errors or 0) for _surface, _count, errors in surface_rows),
                "by_surface": {surface: int(count) for surface, count, _errors in surface_rows},
            },
            "buckets": [
                {"bucket_start": start, "events": int(count), "errors": int(errors or 0)}
                for start, count, errors in bucket_rows
            ],
            "top_actions": [
                {"action": action, "count": int(count)} for action, count in action_rows
            ],
            "runs": {
                "phases_succeeded": int(runs_row[0] or 0),
                "phases_failed": int(runs_row[1] or 0),
            },
        }

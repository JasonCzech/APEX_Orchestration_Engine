"""Usage-analytics events: best-effort writers, /v1 middleware, and aggregation.

Write paths (both modeled on apex.services.engine_runs — throwaway NullPool engine,
swallow-and-log, never raises):

* `/v1` requests: `UsageTrackingMiddleware` (pure ASGI, registered in apex.app.http)
  times each request and, after the response is sent, schedules a fire-and-forget
  asyncio task. The resolved identity is read from ASGI request state before the
  task is created, so raw API credentials never outlive request processing and
  are never authenticated a second time. Failed authentication is attributed by
  an irreversible key fingerprint (`key:<12 hex>`). A write scheduled just before
  process shutdown can be lost, and the request never waits on analytics storage.
* graph nodes: `record_phase_usage_sync`, a sync bridge called from the phase
  finalize node (apex.graphs.pipeline.phase_subgraph) — one event per phase
  terminal status, surface "graph", action `phase:<phase>:<status>`.

Read path: `UsageAnalyticsRepository` runs the Postgres aggregation (date_trunc
buckets) on a request-scoped session for GET /v1/analytics/usage.
"""

import asyncio
import hashlib
import json
import time
from collections.abc import Mapping
from datetime import datetime
from decimal import Decimal
from typing import Any
from urllib.parse import parse_qs

import structlog
from sqlalchemy import ColumnElement, case, func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from apex.auth.identity import ConsumerIdentity, ScopeRef
from apex.auth.service import extract_api_key, hash_api_key
from apex.domain.durable_evidence import sanitize_durable_object, sanitize_durable_text
from apex.graphs.pipeline.configurable import PipelineConfigurable
from apex.persistence.models import AgentEvent, UsageEvent
from apex.services.analytics_scope import analytics_scope_filter
from apex.services.pricing import MAX_TOKEN_COUNT, coerce_token_count, compute_cost
from apex.settings import database_asyncpg_uri, database_ssl_connect_args, get_settings

logger = structlog.get_logger(__name__)

SURFACE_V1 = "v1"
SURFACE_GRAPH = "graph"

# Phase terminal statuses that count as "ok" (everything else terminal is "error").
_OK_PHASE_STATUSES = ("succeeded", "skipped")

# Strong references to in-flight fire-and-forget writes (loops may GC bare tasks).
_PENDING: set[asyncio.Task[None]] = set()
_MAX_PENDING = 1024

_USAGE_TEXT_LIMITS = {
    "event_key": 512,
    "consumer_name": 255,
    "project_id": 255,
    "app_id": 255,
    "surface": 32,
    "action": 255,
    "thread_id": 64,
    "status": 16,
}
_AGENT_TEXT_LIMITS = {
    "event_key": 512,
    "thread_id": 64,
    "project_id": 255,
    "app_id": 255,
    "phase": 64,
    "agent_name": 255,
    "model": 255,
    "provider": 64,
    "status": 16,
}


def _sanitize_event_values(
    values: dict[str, Any], text_limits: Mapping[str, int]
) -> dict[str, Any]:
    """Apply the durable-evidence policy immediately before event persistence."""

    sanitized = dict(values)
    for field_name, limit in text_limits.items():
        value = sanitized.get(field_name)
        if value is not None:
            sanitized[field_name] = sanitize_durable_text(str(value), limit)
    sanitized["extra"] = sanitize_durable_object(sanitized.get("extra") or {})
    return sanitized


def _replay_event_key(kind: str, **identity: Any) -> str:
    """Compact deterministic key for one checkpoint-replayable graph event."""
    payload = json.dumps(identity, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return f"{kind}:{hashlib.sha256(payload.encode('utf-8')).hexdigest()}"


def _idempotent_insert(model: Any, values: dict[str, Any], dialect: str) -> Any | None:
    """Build the native duplicate-safe insert supported by runtime/test dialects."""
    if values.get("event_key") is None:
        return None
    if dialect == "postgresql":
        return (
            pg_insert(model)
            .values(**values)
            .on_conflict_do_nothing(index_elements=[model.event_key])
        )
    if dialect == "sqlite":
        return (
            sqlite_insert(model)
            .values(**values)
            .on_conflict_do_nothing(index_elements=[model.event_key])
        )
    return None


async def _commit_event(session: AsyncSession, row: Any, values: dict[str, Any]) -> None:
    statement = _idempotent_insert(row.__class__, values, session.get_bind().dialect.name)
    if statement is not None:
        await session.execute(statement)
    else:
        session.add(row)
    try:
        await session.commit()
    except IntegrityError:
        # Native upserts cover PostgreSQL and SQLite. This fallback makes another
        # SQLAlchemy dialect replay-safe while preserving non-duplicate failures.
        await session.rollback()
        event_key = values.get("event_key")
        if event_key is None:
            raise
        existing = await session.scalar(
            select(row.__class__.id).where(row.__class__.event_key == event_key)
        )
        if existing is None:
            raise


# ── Best-effort writers ──────────────────────────────────────────────────────


async def record_usage_event(
    *,
    consumer_name: str,
    surface: str,
    action: str,
    status: str = "ok",
    project_id: str | None = None,
    app_id: str | None = None,
    thread_id: str | None = None,
    duration_ms: int | None = None,
    event_key: str | None = None,
    extra: dict[str, Any] | None = None,
) -> None:
    """Insert one usage event; never raises (mirrors services.engine_runs)."""
    try:
        # Throwaway engine per call: callers include graph worker threads with
        # short-lived event loops, so pooled connections must not outlive them.
        database = get_settings().database
        engine_db = create_async_engine(
            database_asyncpg_uri(database.uri),
            poolclass=NullPool,
            connect_args=database_ssl_connect_args(database.uri, database.ssl_mode),
        )
        try:
            session_factory = async_sessionmaker(engine_db, expire_on_commit=False)
            await _insert_usage_event(
                session_factory,
                consumer_name=consumer_name,
                surface=surface,
                action=action,
                status=status,
                project_id=project_id,
                app_id=app_id,
                thread_id=thread_id,
                duration_ms=duration_ms,
                event_key=event_key,
                extra=extra,
            )
        finally:
            await engine_db.dispose()
    except Exception as exc:  # noqa: BLE001 — analytics never fails a request or run
        logger.warning(
            "usage.record_failed",
            action=action,
            error_type=exc.__class__.__name__,
        )


async def _insert_usage_event(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    consumer_name: str,
    surface: str,
    action: str,
    status: str = "ok",
    project_id: str | None = None,
    app_id: str | None = None,
    thread_id: str | None = None,
    duration_ms: int | None = None,
    event_key: str | None = None,
    extra: dict[str, Any] | None = None,
) -> None:
    async with session_factory() as session:
        values = _sanitize_event_values(
            {
                "event_key": event_key,
                "consumer_name": consumer_name,
                "project_id": project_id,
                "app_id": app_id,
                "surface": surface,
                "action": action,
                "thread_id": thread_id,
                "duration_ms": duration_ms,
                "status": status,
                "extra": extra or {},
            },
            _USAGE_TEXT_LIMITS,
        )
        await _commit_event(session, UsageEvent(**values), values)


def record_usage_event_sync(**kwargs: Any) -> None:
    """Sync bridge for graph nodes (which run sync on worker threads)."""
    try:
        asyncio.run(record_usage_event(**kwargs))
    except Exception as exc:  # noqa: BLE001
        logger.warning("usage.record_failed", error_type=exc.__class__.__name__)


def record_phase_usage_sync(
    phase: str,
    status: str,
    config: Any,
    *,
    attempt: int | None = None,
) -> None:
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
        app_id = configurable.get("app_id")
        event_key = (
            _replay_event_key(
                "phase",
                thread_id=str(thread_id),
                phase=phase,
                attempt=attempt,
                project_id=str(project_id) if project_id else None,
                app_id=str(app_id) if app_id else None,
            )
            if thread_id and attempt is not None
            else None
        )
        event: dict[str, Any] = {
            "consumer_name": str(identity) if identity else "graph",
            "surface": SURFACE_GRAPH,
            "action": f"phase:{phase}:{status}",
            "status": "ok" if status in _OK_PHASE_STATUSES else "error",
            "project_id": str(project_id) if project_id else None,
            "app_id": str(app_id) if app_id else None,
            "thread_id": str(thread_id) if thread_id else None,
        }
        if event_key is not None:
            event["event_key"] = event_key
        record_usage_event_sync(**event)
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "usage.phase_record_failed",
            phase=phase,
            error_type=exc.__class__.__name__,
        )


def _usage_int(value: Any) -> int:
    return coerce_token_count(value)


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
    total_tokens = _usage_int(usage.get("total_tokens")) or min(
        input_tokens + output_tokens,
        MAX_TOKEN_COUNT,
    )
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
    if isinstance(raw, str) and raw:
        return raw.replace("\x00", "\\0")[:64]
    if model and ":" in model:
        return model.split(":", 1)[0]
    if model and "/" in model:
        return model.split("/", 1)[0]
    if model and model.startswith("claude-"):
        return "anthropic"
    if model and model.startswith("gpt-"):
        return "openai"
    return None


def _bounded_event_label(value: str | None, max_chars: int) -> str | None:
    if not isinstance(value, str):
        return None
    return value.replace("\x00", "\\0")[:max_chars] or None


async def _insert_agent_event(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    thread_id: str | None,
    project_id: str | None,
    app_id: str | None,
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
    event_key: str | None = None,
    extra: dict[str, Any] | None = None,
) -> None:
    async with session_factory() as session:
        values = _sanitize_event_values(
            {
                "event_key": event_key,
                "thread_id": thread_id,
                "project_id": project_id,
                "app_id": app_id,
                "phase": phase,
                "agent_name": agent_name,
                "model": model,
                "provider": provider,
                "attempt": attempt,
                "status": status,
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "total_tokens": total_tokens,
                "cache_read_tokens": cache_read_tokens,
                "cache_creation_tokens": cache_creation_tokens,
                "reasoning_tokens": reasoning_tokens,
                "cost_usd": cost_usd,
                "latency_ms": latency_ms,
                "extra": extra or {},
            },
            _AGENT_TEXT_LIMITS,
        )
        await _commit_event(session, AgentEvent(**values), values)


async def record_agent_event(
    *,
    thread_id: str | None,
    project_id: str | None,
    app_id: str | None,
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
    model = _bounded_event_label(model, 255)
    provider = _bounded_event_label(provider, 64)
    cost_usd, pricing = compute_cost(model, token_usage)
    extra: dict[str, Any] = {}
    if pricing is not None:
        extra["pricing"] = pricing
    if usage:
        finish_reason = usage.get("finish_reason") or usage.get("stop_reason")
        safe_finish_reason = _bounded_event_label(finish_reason, 255)
        if safe_finish_reason:
            extra["finish_reason"] = safe_finish_reason
    event_key = (
        _replay_event_key(
            "agent",
            thread_id=thread_id,
            phase=phase,
            attempt=attempt,
            agent_name=agent_name,
            project_id=project_id,
            app_id=app_id,
        )
        if thread_id and attempt is not None
        else None
    )
    try:
        database = get_settings().database
        engine_db = create_async_engine(
            database_asyncpg_uri(database.uri),
            poolclass=NullPool,
            connect_args=database_ssl_connect_args(database.uri, database.ssl_mode),
        )
        try:
            session_factory = async_sessionmaker(engine_db, expire_on_commit=False)
            await _insert_agent_event(
                session_factory,
                thread_id=thread_id,
                project_id=project_id,
                app_id=app_id,
                phase=phase,
                agent_name=agent_name,
                model=model,
                provider=provider,
                attempt=attempt,
                status=status,
                latency_ms=latency_ms,
                cost_usd=cost_usd,
                event_key=event_key,
                extra=extra,
                **token_usage,
            )
        finally:
            await engine_db.dispose()
    except Exception as exc:  # noqa: BLE001 — analytics never fails a request or run
        logger.warning(
            "agent_events.record_failed",
            phase=phase,
            error_type=exc.__class__.__name__,
        )


def record_agent_event_sync(
    *,
    phase: str,
    status: str,
    attempt: int | None,
    config: Any,
    latency_ms: int | None,
    usage: Mapping[str, Any] | None = None,
    agent_name: str | None = None,
    model: str | None = None,
) -> None:
    """Sync bridge for graph nodes: one row per phase/agent invocation.

    `model` is the model the agent actually used (recorded by the LLM agent). When
    omitted (e.g. the deterministic stub) it falls back to the per-phase config
    override so behavior is unchanged for stub runs.
    """
    try:
        configurable: dict[str, Any] = dict((config or {}).get("configurable") or {})
        cfg = PipelineConfigurable.from_config(config)
        if model is None:
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
                app_id=str(configurable.get("app_id")) if configurable.get("app_id") else None,
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
        logger.warning(
            "agent_events.record_failed",
            phase=phase,
            error_type=exc.__class__.__name__,
        )


# ── /v1 request middleware ───────────────────────────────────────────────────


def _request_consumer_name(scope: Mapping[str, Any]) -> str:
    """Capture safe attribution without retaining a request credential."""

    state = scope.get("state") or {}
    identity = state.get("identity") if isinstance(state, Mapping) else None
    if isinstance(identity, ConsumerIdentity):
        return identity.name
    api_key = extract_api_key(scope.get("headers") or [])
    if api_key:
        return f"key:{hash_api_key(api_key)[:12]}"
    return "anonymous"


async def _record_request_event(
    *,
    consumer_name: str,
    action: str,
    status: str,
    duration_ms: int,
    project_id: str | None,
    app_id: str | None,
    extra: dict[str, Any],
) -> None:
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
            app_id=app_id,
            extra=extra,
        )
    except Exception as exc:  # noqa: BLE001 — analytics never fails a request
        logger.warning(
            "usage.record_failed",
            action=action,
            error_type=exc.__class__.__name__,
        )


def _first_value(values: Mapping[str, list[str]], *keys: str) -> str | None:
    for key in keys:
        candidates = values.get(key)
        if candidates and candidates[0]:
            return candidates[0]
    return None


def _request_scope(scope: Mapping[str, Any]) -> tuple[str | None, str | None]:
    """Return a safely attributable project/app pair for one HTTP request.

    Explicit scope values are accepted only when the route-resolved identity may
    access that exact scope. Without explicit values, a single identity scope is
    unambiguous. An app-only identity with one app in an explicit project is also
    unambiguous; multiple app scopes are deliberately left unattributed instead
    of widening the event to project scope.
    """
    state = scope.get("state") or {}
    identity = state.get("identity") if isinstance(state, Mapping) else None
    if not isinstance(identity, ConsumerIdentity):
        return None, None

    path_params = scope.get("path_params") or {}
    query = parse_qs((scope.get("query_string") or b"").decode("latin-1"))
    project_id = path_params.get("project_id") or _first_value(query, "project", "project_id")
    app_id = path_params.get("app_id") or _first_value(query, "app", "app_id")
    project_id = str(project_id) if project_id else None
    app_id = str(app_id) if app_id else None

    if app_id is not None and project_id is None:
        return None, None
    if project_id is not None:
        if app_id is not None:
            return (
                (project_id, app_id)
                if identity.allows_scope(project_id=project_id, app_id=app_id)
                else (None, None)
            )
        if not identity.allows_project(project_id):
            return None, None
        if identity.is_unscoped or identity.contains_scope(ScopeRef(project_id=project_id)):
            return project_id, None
        app_ids = {
            item.app_id
            for item in identity.scopes
            if item.project_id == project_id and item.app_id is not None
        }
        return (project_id, next(iter(app_ids))) if len(app_ids) == 1 else (None, None)

    if identity.is_unscoped:
        return None, None
    distinct_scopes = {(item.project_id, item.app_id) for item in identity.scopes}
    return next(iter(distinct_scopes)) if len(distinct_scopes) == 1 else (None, None)


def _request_project_id(scope: Mapping[str, Any]) -> str | None:
    """Compatibility wrapper for callers that need only project attribution."""
    return _request_scope(scope)[0]


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
            if str(scope.get("path") or "") == "/ready":
                # Kubelet polls this frequently and it is not product usage.
                return
            operation_id = getattr(scope.get("route"), "operation_id", None)
            if not operation_id:
                return  # docs/openapi/unmatched paths carry no contract operation
            duration_ms = int((time.perf_counter() - started) * 1000)
            consumer_name = _request_consumer_name(scope)
            project_id, app_id = _request_scope(scope)
            if len(_PENDING) >= _MAX_PENDING:
                return
            task = asyncio.get_running_loop().create_task(
                _record_request_event(
                    consumer_name=consumer_name,
                    action=str(operation_id),
                    status="ok" if status_code < 400 else "error",
                    duration_ms=duration_ms,
                    project_id=project_id,
                    app_id=app_id,
                    # ``action`` is already the stable route operation id. Never
                    # persist the concrete request path: path parameters can be
                    # user-controlled identifiers or accidentally carry secrets.
                    extra={"status_code": status_code},
                )
            )
            _PENDING.add(task)
            task.add_done_callback(_PENDING.discard)
        except Exception as exc:  # noqa: BLE001 — analytics never fails a request
            logger.warning(
                "usage.middleware_failed",
                error_type=exc.__class__.__name__,
            )


# ── Aggregation (GET /v1/analytics/usage) ────────────────────────────────────


class UsageAnalyticsRepository:
    """Postgres aggregation over usage_events (date_trunc buckets; request session).

    ``visible_scopes=None`` means an unscoped platform administrator. Any tuple,
    including an empty one, is translated to an exact project/app predicate.
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
        visible_scopes: tuple[ScopeRef, ...] | None = None,
    ) -> dict[str, Any]:
        filters: list[ColumnElement[bool]] = [
            UsageEvent.at >= window_from,
            UsageEvent.at < window_to,
        ]
        if project_id is not None:
            filters.append(UsageEvent.project_id == project_id)
        scope_filter = analytics_scope_filter(UsageEvent, visible_scopes)
        if scope_filter is not None:
            filters.append(scope_filter)
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

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
from typing import Any, cast
from urllib.parse import parse_qs

import structlog
from langchain_core.runnables import RunnableConfig
from sqlalchemy import ColumnElement, case, func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from apex.auth.identity import ConsumerIdentity, ScopeRef
from apex.auth.service import extract_api_key, hash_api_key
from apex.domain.diagnostics import bounded_diagnostic, safe_type_name
from apex.domain.durable_evidence import sanitize_durable_object, sanitize_durable_text
from apex.graphs.pipeline.configurable import PipelineConfigurable
from apex.persistence.db import dispose_engine_instance_definitively
from apex.persistence.models import AgentEvent, UsageEvent
from apex.services.analytics_scope import analytics_scope_filter
from apex.services.pricing import (
    coerce_token_count,
    compute_cost,
    normalize_cache_token_counts,
    normalize_usage_mapping,
)
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
            sanitized[field_name] = sanitize_durable_text(value, limit)
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
            await dispose_engine_instance_definitively(engine_db)
    except Exception as exc:  # noqa: BLE001 — analytics never fails a request or run
        logger.warning(
            "usage.record_failed",
            action=action,
            error_type=safe_type_name(exc),
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
        logger.warning("usage.record_failed", error_type=safe_type_name(exc))


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
        raw_configurable = config.get("configurable") if type(config) is dict else None
        configurable: dict[str, Any] = (
            dict(raw_configurable) if type(raw_configurable) is dict else {}
        )
        safe_phase = _bounded_event_label(phase, _AGENT_TEXT_LIMITS["phase"])
        safe_status = _bounded_event_label(status, _USAGE_TEXT_LIMITS["status"])
        if safe_phase is None or safe_status is None:
            return
        user = configurable.get("langgraph_auth_user")
        identity = user.get("identity") if type(user) is dict else None
        consumer_name = _bounded_event_label(identity, _USAGE_TEXT_LIMITS["consumer_name"])
        safe_thread_id = _bounded_event_label(
            configurable.get("thread_id"), _USAGE_TEXT_LIMITS["thread_id"]
        )
        safe_project_id = _bounded_event_label(
            configurable.get("project_id"), _USAGE_TEXT_LIMITS["project_id"]
        )
        safe_app_id = _bounded_event_label(configurable.get("app_id"), _USAGE_TEXT_LIMITS["app_id"])
        event_key = (
            _replay_event_key(
                "phase",
                thread_id=safe_thread_id,
                phase=safe_phase,
                attempt=attempt,
                project_id=safe_project_id,
                app_id=safe_app_id,
            )
            if safe_thread_id and type(attempt) is int and 1 <= attempt <= 1_000_000
            else None
        )
        event: dict[str, Any] = {
            "consumer_name": consumer_name or "graph",
            "surface": SURFACE_GRAPH,
            "action": f"phase:{safe_phase}:{safe_status}",
            "status": "ok" if safe_status in _OK_PHASE_STATUSES else "error",
            "project_id": safe_project_id,
            "app_id": safe_app_id,
            "thread_id": safe_thread_id,
        }
        if event_key is not None:
            event["event_key"] = event_key
        record_usage_event_sync(**event)
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "usage.phase_record_failed",
            phase=_bounded_event_label(phase, _AGENT_TEXT_LIMITS["phase"]),
            error_type=safe_type_name(exc),
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
    # Provider metadata is untrusted. Exact built-in dictionaries preserve the
    # normalized LangChain contract without executing custom Mapping truthiness,
    # membership, or ``get`` hooks during best-effort analytics.
    usage = normalize_usage_mapping(usage) or {}
    input_details = usage.get("input_token_details")
    output_details = usage.get("output_token_details")
    input_details = normalize_usage_mapping(input_details) or {}
    output_details = normalize_usage_mapping(output_details) or {}
    input_tokens = _usage_int(usage.get("input_tokens"))
    output_tokens = _usage_int(usage.get("output_tokens"))
    # LangChain's normalized contract defines total_tokens as exactly the sum of
    # input and output. Deriving it avoids persisting contradictory provider
    # metadata and keeps aggregates internally reconcilable.
    total_tokens = input_tokens + output_tokens
    cache_read_tokens, cache_creation_tokens = normalize_cache_token_counts(
        input_tokens,
        _usage_detail_int(
            input_details, "cache_read", "cache_read_tokens", "cache_read_input_tokens"
        ),
        _usage_detail_int(
            input_details,
            "cache_creation",
            "cache_creation_tokens",
            "cache_creation_input_tokens",
            "cache_write",
        ),
    )
    return {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "total_tokens": total_tokens,
        "cache_read_tokens": cache_read_tokens,
        "cache_creation_tokens": cache_creation_tokens,
        "reasoning_tokens": min(
            output_tokens,
            _usage_detail_int(
                output_details, "reasoning", "reasoning_tokens", "reasoning_output_tokens"
            ),
        ),
    }


def _first_exact_nonempty_text(values: dict[str, Any], *keys: str) -> str | None:
    """Select provider text without executing arbitrary scalar truthiness hooks."""

    for key in keys:
        candidate = values.get(key)
        if type(candidate) is str and candidate:
            return candidate
    return None


def _provider_from(model: str | None, usage: Mapping[str, Any] | None) -> str | None:
    safe_usage = normalize_usage_mapping(usage) or {}
    raw = _first_exact_nonempty_text(safe_usage, "provider", "ls_provider")
    if raw is not None:
        return raw.replace("\x00", "\\0")[:64]
    safe_model = model if type(model) is str else None
    if safe_model and ":" in safe_model:
        return safe_model.split(":", 1)[0]
    if safe_model and "/" in safe_model:
        return safe_model.split("/", 1)[0]
    if safe_model and safe_model.startswith("claude-"):
        return "anthropic"
    if safe_model and safe_model.startswith("gpt-"):
        return "openai"
    return None


def _bounded_event_label(value: str | None, max_chars: int) -> str | None:
    if type(value) is not str:
        return None
    rendered = sanitize_durable_text(value, max_chars)
    if rendered is None or len(rendered) > max_chars or "\x00" in value:
        rendered = bounded_diagnostic(value, max_chars=max_chars)
    return rendered or None


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
    try:
        safe_thread_id = _bounded_event_label(thread_id, _AGENT_TEXT_LIMITS["thread_id"])
        safe_project_id = _bounded_event_label(project_id, _AGENT_TEXT_LIMITS["project_id"])
        safe_app_id = _bounded_event_label(app_id, _AGENT_TEXT_LIMITS["app_id"])
        safe_phase = _bounded_event_label(phase, _AGENT_TEXT_LIMITS["phase"])
        safe_agent_name = _bounded_event_label(agent_name, _AGENT_TEXT_LIMITS["agent_name"])
        safe_status = _bounded_event_label(status, _AGENT_TEXT_LIMITS["status"])
        if safe_phase is None or safe_agent_name is None or safe_status is None:
            return
        safe_attempt = attempt if type(attempt) is int and 1 <= attempt <= 1_000_000 else None
        safe_latency_ms = (
            latency_ms if type(latency_ms) is int and 0 <= latency_ms <= 86_400_000 else None
        )
        safe_usage = normalize_usage_mapping(usage)
        token_usage = normalize_usage_metadata(safe_usage)
        safe_model = _bounded_event_label(model, _AGENT_TEXT_LIMITS["model"])
        safe_provider = _bounded_event_label(provider, _AGENT_TEXT_LIMITS["provider"])
        cost_usd, pricing = compute_cost(safe_model, token_usage)
        extra: dict[str, Any] = {}
        if pricing is not None:
            extra["pricing"] = pricing
        if safe_usage:
            finish_reason = _first_exact_nonempty_text(safe_usage, "finish_reason", "stop_reason")
            safe_finish_reason = _bounded_event_label(finish_reason, 255)
            if safe_finish_reason:
                extra["finish_reason"] = safe_finish_reason
        event_key = (
            _replay_event_key(
                "agent",
                thread_id=safe_thread_id,
                phase=safe_phase,
                attempt=safe_attempt,
                agent_name=safe_agent_name,
                project_id=safe_project_id,
                app_id=safe_app_id,
            )
            if safe_thread_id is not None and safe_attempt is not None
            else None
        )
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
                thread_id=safe_thread_id,
                project_id=safe_project_id,
                app_id=safe_app_id,
                phase=safe_phase,
                agent_name=safe_agent_name,
                model=safe_model,
                provider=safe_provider,
                attempt=safe_attempt,
                status=safe_status,
                latency_ms=safe_latency_ms,
                cost_usd=cost_usd,
                event_key=event_key,
                extra=extra,
                **token_usage,
            )
        finally:
            await dispose_engine_instance_definitively(engine_db)
    except Exception as exc:  # noqa: BLE001 — analytics never fails a request or run
        logger.warning(
            "agent_events.record_failed",
            phase=_bounded_event_label(phase, _AGENT_TEXT_LIMITS["phase"]),
            error_type=safe_type_name(exc),
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
        raw_configurable = config.get("configurable") if type(config) is dict else None
        configurable: dict[str, Any] = (
            dict(raw_configurable) if type(raw_configurable) is dict else {}
        )
        safe_phase = _bounded_event_label(phase, _AGENT_TEXT_LIMITS["phase"])
        safe_status = _bounded_event_label(status, _AGENT_TEXT_LIMITS["status"])
        if safe_phase is None or safe_status is None:
            return
        cfg = PipelineConfigurable.from_config(
            cast(RunnableConfig | None, config if type(config) is dict else None)
        )
        if model is None:
            for key, value in cfg.model_by_phase.items():
                if key.value == safe_phase:
                    model = value
                    break
        safe_agent_name = (
            _bounded_event_label(agent_name, _AGENT_TEXT_LIMITS["agent_name"])
            or f"{safe_phase}.worker"
        )
        asyncio.run(
            record_agent_event(
                thread_id=_bounded_event_label(
                    configurable.get("thread_id"), _AGENT_TEXT_LIMITS["thread_id"]
                ),
                project_id=_bounded_event_label(
                    configurable.get("project_id"), _AGENT_TEXT_LIMITS["project_id"]
                ),
                app_id=_bounded_event_label(
                    configurable.get("app_id"), _AGENT_TEXT_LIMITS["app_id"]
                ),
                phase=safe_phase,
                agent_name=safe_agent_name,
                model=model,
                provider=_provider_from(model, usage),
                attempt=attempt,
                status="ok" if safe_status in _OK_PHASE_STATUSES else "error",
                latency_ms=latency_ms,
                usage=usage,
            )
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "agent_events.record_failed",
            phase=_bounded_event_label(phase, _AGENT_TEXT_LIMITS["phase"]),
            error_type=safe_type_name(exc),
        )


# ── /v1 request middleware ───────────────────────────────────────────────────


def _request_consumer_name(scope: Mapping[str, Any]) -> str:
    """Capture safe attribution without retaining a request credential."""

    state = scope.get("state")
    identity = state.get("identity") if type(state) is dict else None
    if type(identity) is ConsumerIdentity:
        return (
            _bounded_event_label(identity.name, _USAGE_TEXT_LIMITS["consumer_name"])
            or "authenticated"
        )
    raw_headers = scope.get("headers")
    api_key = extract_api_key(raw_headers if type(raw_headers) is list else [])
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
            error_type=safe_type_name(exc),
        )


def _first_value(values: Mapping[str, list[str]], *keys: str) -> str | None:
    for key in keys:
        candidates = values.get(key)
        if type(candidates) is list and candidates and type(candidates[0]) is str and candidates[0]:
            return candidates[0]
    return None


def _scope_attribution_label(value: Any) -> str | None:
    if type(value) is not str or not 1 <= len(value) <= 255 or value != value.strip():
        return None
    return value if sanitize_durable_text(value, 255) == value else None


def _request_scope(scope: Mapping[str, Any]) -> tuple[str | None, str | None]:
    """Return a safely attributable project/app pair for one HTTP request.

    Explicit scope values are accepted only when the route-resolved identity may
    access that exact scope. Without explicit values, a single identity scope is
    unambiguous. An app-only identity with one app in an explicit project is also
    unambiguous; multiple app scopes are deliberately left unattributed instead
    of widening the event to project scope.
    """
    state = scope.get("state")
    identity = state.get("identity") if type(state) is dict else None
    if type(identity) is not ConsumerIdentity:
        return None, None

    path_params = scope.get("path_params")
    path_params = path_params if type(path_params) is dict else {}
    raw_query = scope.get("query_string")
    query: dict[str, list[str]] = {}
    if type(raw_query) is bytes and len(raw_query) <= 100_000:
        try:
            query = parse_qs(raw_query.decode("latin-1"), max_num_fields=256)
        except ValueError:
            query = {}
    project_id = _scope_attribution_label(
        path_params.get("project_id") or _first_value(query, "project", "project_id")
    )
    app_id = _scope_attribution_label(
        path_params.get("app_id") or _first_value(query, "app", "app_id")
    )

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
            if scope.get("path") == "/ready":
                # Kubelet polls this frequently and it is not product usage.
                return
            operation_id = getattr(scope.get("route"), "operation_id", None)
            safe_operation_id = _bounded_event_label(operation_id, _USAGE_TEXT_LIMITS["action"])
            if safe_operation_id is None:
                return  # docs/openapi/unmatched paths carry no contract operation
            duration_ms = int((time.perf_counter() - started) * 1000)
            consumer_name = _request_consumer_name(scope)
            project_id, app_id = _request_scope(scope)
            if len(_PENDING) >= _MAX_PENDING:
                return
            task = asyncio.get_running_loop().create_task(
                _record_request_event(
                    consumer_name=consumer_name,
                    action=safe_operation_id,
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
                error_type=safe_type_name(exc),
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

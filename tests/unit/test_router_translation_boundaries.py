"""Regression canaries for detached HTTP-boundary exception translations."""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any, cast

import pytest
from fastapi import HTTPException

from apex.auth.identity import Role
from apex.routers import analytics as analytics_router
from apex.routers import artifacts as artifacts_router
from apex.routers import catalog as catalog_router
from apex.routers import compliance as compliance_router
from apex.routers import connections as connections_router
from apex.routers import consumers as consumers_router
from apex.routers import context as context_router
from apex.routers import engines as engines_router
from apex.routers import inventory as inventory_router
from apex.routers import logs as logs_router
from apex.routers import pipelines as pipelines_router
from apex.routers import work_tracking as work_tracking_router
from apex.services.engine_abort import (
    EngineProjectionFinalizationPendingError,
    EngineProviderAbortError,
)


def _assert_detached(error: BaseException, canary: str) -> None:
    assert error.__cause__ is None
    assert error.__context__ is None
    assert canary not in repr(error)


async def test_inventory_provider_translation_drops_raw_exception_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    canary = "inventory-provider-context-secret-canary"
    environment = SimpleNamespace(
        id="env-1",
        name="Production",
        application_id="app-1",
        application=SimpleNamespace(project_id="project-1"),
    )

    class Repository:
        async def get_environment(self, _environment_id: str) -> Any:
            return environment

    class Identity:
        def allows_scope(self, **_kwargs: Any) -> bool:
            return True

    class FailingService:
        def __init__(self, _repository: Any) -> None:
            pass

        async def rescan(self, *_args: Any, **_kwargs: Any) -> Any:
            raise RuntimeError(canary)

    async def release_transactions(_repository: Any) -> None:
        return None

    monkeypatch.setattr(inventory_router, "InventoryService", FailingService)
    monkeypatch.setattr(inventory_router, "release_read_transactions", release_transactions)

    with pytest.raises(HTTPException) as raised:
        await inventory_router.rescan_environment(
            "env-1",
            cast(Any, Identity()),
            cast(Any, Repository()),
            cast(Any, object()),
        )

    assert raised.value.status_code == 502
    _assert_detached(raised.value, canary)


def test_log_projection_translation_drops_hostile_provider_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    canary = "log-projection-context-secret-canary"

    def fail_projection(*_args: Any, **_kwargs: Any) -> Any:
        raise RuntimeError(canary)

    monkeypatch.setattr(logs_router, "_project_public_log_result", fail_projection)

    with pytest.raises(ValueError, match="invalid log result") as raised:
        logs_router._public_log_result(object(), requested_limit=1)

    _assert_detached(raised.value, canary)


async def test_artifact_preflight_translation_drops_store_exception_context() -> None:
    canary = "artifact-store-context-secret-canary"

    class MissingStore:
        def iter_bytes(self, _key: str) -> Any:
            async def chunks() -> Any:
                if False:  # pragma: no cover - makes this an async generator
                    yield b""
                raise KeyError(canary)

            return chunks()

    with pytest.raises(HTTPException) as raised:
        await artifacts_router._open_stream(MissingStore(), "artifact-key")

    assert raised.value.status_code == 404
    _assert_detached(raised.value, canary)


def test_work_tracking_adapter_translation_drops_provider_exception_context() -> None:
    canary = "work-tracker-context-secret-canary"
    boundary = work_tracking_router.adapter_errors()

    with pytest.raises(HTTPException) as raised:
        with boundary:
            raise ValueError(canary)
        boundary.raise_if_error()

    assert raised.value.status_code == 422
    _assert_detached(raised.value, canary)


def test_catalog_input_translation_drops_validator_exception_context() -> None:
    canary = "catalog-options-context-secret-canary"

    with pytest.raises(HTTPException) as raised:
        catalog_router._validate_environment_options(
            cast(Any, SimpleNamespace(role=Role.ADMIN, is_unscoped=True)),
            {"password": canary},
        )

    assert raised.value.status_code == 422
    _assert_detached(raised.value, canary)


def test_connection_target_translation_drops_parser_exception_context() -> None:
    canary = "connection-port-context-secret-canary"

    with pytest.raises(HTTPException) as raised:
        connections_router._validate_connection_target(
            f"https://provider.example:{canary}", {}, None
        )

    assert raised.value.status_code == 422
    _assert_detached(raised.value, canary)


async def test_context_input_translation_drops_validator_exception_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    canary = "context-input-context-secret-canary"

    def fail_validation(_payload: Any) -> Any:
        raise ValueError(canary)

    monkeypatch.setattr(context_router, "validate_context_run_input", fail_validation)
    monkeypatch.setattr(context_router, "select_work_tracking_project", lambda *_args: None)

    with pytest.raises(HTTPException) as raised:
        await context_router.create_context_summary(
            context_router.ContextSummaryRequest(subject="safe subject"),
            cast(Any, object()),
            cast(Any, object()),
            cast(Any, object()),
        )

    assert raised.value.status_code == 422
    _assert_detached(raised.value, canary)


async def test_context_runtime_translation_drops_loopback_exception_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    canary = "context-runtime-context-secret-canary"

    async def release_transactions(_repository: Any) -> None:
        return None

    async def fail_start(*_args: Any, **_kwargs: Any) -> Any:
        raise context_router.ContextRunStartError(canary)

    monkeypatch.setattr(context_router, "select_work_tracking_project", lambda *_args: None)
    monkeypatch.setattr(context_router, "release_read_transactions", release_transactions)
    monkeypatch.setattr(context_router, "_loopback_client_after_scope", lambda *_args: object())
    monkeypatch.setattr(context_router, "start_context_summary", fail_start)

    with pytest.raises(HTTPException) as raised:
        await context_router.create_context_summary(
            context_router.ContextSummaryRequest(subject="safe subject"),
            cast(Any, object()),
            cast(Any, object()),
            cast(Any, object()),
        )

    assert raised.value.status_code == 502
    assert raised.value.detail == "context runtime unavailable"
    _assert_detached(raised.value, canary)


async def test_engine_abort_translation_drops_service_exception_context() -> None:
    canary = "engine-abort-context-secret-canary"

    class Service:
        async def abort(self, *_args: Any, **_kwargs: Any) -> Any:
            raise EngineProviderAbortError(canary)

    with pytest.raises(HTTPException) as raised:
        await engines_router.abort_engine_run("thread-1", cast(Any, Service()))

    assert raised.value.status_code == 502
    _assert_detached(raised.value, canary)


async def test_pipeline_read_translation_drops_runtime_exception_context() -> None:
    canary = "pipeline-read-context-secret-canary"

    class Service:
        async def get_pipeline(self, _thread_id: str) -> Any:
            raise RuntimeError(canary)

    with pytest.raises(HTTPException) as raised:
        await pipelines_router.get_pipeline(
            "thread-1",
            cast(Any, object()),
            cast(Any, Service()),
        )

    assert raised.value.status_code == 502
    assert raised.value.detail == "pipeline runtime unavailable"
    _assert_detached(raised.value, canary)


@pytest.mark.parametrize(
    ("service_error", "status", "detail"),
    [
        (
            EngineProviderAbortError("pipeline-engine-provider-secret-canary"),
            502,
            "engine provider abort failed",
        ),
        (
            EngineProjectionFinalizationPendingError("thread-1"),
            503,
            "external engine stopped but provider cleanup or durable projection is "
            "pending; retry abort",
        ),
    ],
)
async def test_pipeline_abort_translates_engine_terminal_failures(
    service_error: Exception,
    status: int,
    detail: str,
) -> None:
    class EngineService:
        async def abort(self, _thread_id: str) -> Any:
            raise service_error

    with pytest.raises(HTTPException) as raised:
        await pipelines_router.abort_pipeline(
            "thread-1",
            cast(Any, object()),
            cast(Any, EngineService()),
        )

    assert raised.value.status_code == status
    assert raised.value.detail == detail
    assert raised.value.__cause__ is None
    assert raised.value.__context__ is None


def test_analytics_window_translation_drops_datetime_exception_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    canary = "analytics-window-context-secret-canary"

    def fail_timedelta(*, days: int) -> Any:
        assert days == 7
        raise OverflowError(canary)

    monkeypatch.setattr(analytics_router, "timedelta", fail_timedelta)

    with pytest.raises(HTTPException) as raised:
        analytics_router._default_window_start(datetime.now(UTC))

    assert raised.value.status_code == 422
    _assert_detached(raised.value, canary)


def test_compliance_cutoff_translation_drops_validator_exception_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    canary = "compliance-cutoff-context-secret-canary"

    def fail_validation(_before: datetime) -> None:
        raise ValueError(canary)

    monkeypatch.setattr(compliance_router, "validate_retention_cutoff", fail_validation)

    with pytest.raises(HTTPException) as raised:
        compliance_router._validate_before(datetime.now(UTC))

    assert raised.value.status_code == 422
    _assert_detached(raised.value, canary)


def test_consumer_timestamp_translation_drops_datetime_exception_context() -> None:
    canary = "consumer-timestamp-context-secret-canary"

    class OverflowingDatetime:
        tzinfo = UTC

        def astimezone(self, _timezone: Any) -> Any:
            raise OverflowError(canary)

    with pytest.raises(HTTPException) as raised:
        consumers_router._as_utc(cast(Any, OverflowingDatetime()))

    assert raised.value.status_code == 422
    _assert_detached(raised.value, canary)

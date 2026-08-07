"""APEX Load native-control router dependency regressions."""

from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any, cast

import apex.routers.apex_load as apex_load_router
from apex.adapters.apex_load.engine import ApexLoadExecutionEngine
from apex.adapters.registry import ConnectionConfig, PortKind
from apex.auth.identity import ConsumerIdentity, ConsumerType, Role, ScopeRef
from apex.domain.integrations import SecretValue


async def test_native_control_allows_distinct_orchestration_and_provider_project_ids() -> None:
    adapter = ApexLoadExecutionEngine(
        ConnectionConfig(
            id="connection-1",
            kind=PortKind.EXECUTION_ENGINE,
            provider="apex_load",
            name="APEX Load",
            options={
                "base_url": "https://apexload.example.test",
                "project_id": "provider-project-1",
                "native_control_enabled": True,
            },
        ),
        SecretValue(value="apex-load-service-key"),
    )

    class Repository:
        async def get(self, connection_id: str) -> object:
            assert connection_id == "connection-1"
            return SimpleNamespace(
                enabled=True,
                kind="execution_engine",
                provider="apex_load",
                project_id="orchestration-project-1",
            )

    class Resolver:
        async def resolve_with_metadata(self, kind: PortKind, **kwargs: object) -> object:
            assert kind is PortKind.EXECUTION_ENGINE
            assert kwargs == {
                "connection_id": "connection-1",
                "project_id": "orchestration-project-1",
                "expected_provider": "apex_load",
            }
            return SimpleNamespace(
                adapter=adapter,
                connection_id="connection-1",
                connection_version=datetime(2026, 8, 7, tzinfo=UTC),
                persisted=True,
            )

    identity = ConsumerIdentity(
        consumer_id="operator-1",
        name="operator-1",
        consumer_type=ConsumerType.DASHBOARD,
        role=Role.OPERATOR,
        scopes=[ScopeRef(project_id="orchestration-project-1")],
    )
    dependency: Any = apex_load_router._resolve_control(
        "connection-1",
        identity,
        cast(Any, Repository()),
        cast(Any, Resolver()),
    )

    resolved = await anext(dependency)
    try:
        assert resolved.project_id == "orchestration-project-1"
        assert resolved.adapter.native_project_id == "provider-project-1"
        assert resolved.remote_project_id == "provider-project-1"
    finally:
        await dependency.aclose()


async def test_mirrored_runs_hide_console_links_after_connection_drift(
    monkeypatch: Any,
) -> None:
    current_version = datetime(2026, 8, 7, tzinfo=UTC)

    class Adapter:
        def native_console_url(self, run_id: str) -> str:
            return f"https://apexload.example/#/test/{run_id}"

    class Repository:
        async def list_runs(self, **kwargs: object) -> list[object]:
            assert kwargs == {
                "connection_id": "connection-1",
                "project_id": "orchestration-project-1",
                "limit": 25,
                "offset": 0,
            }
            return [
                SimpleNamespace(
                    remote_id="current-run",
                    status="COMPLETE",
                    terminal=True,
                    revision=1,
                    scenario_remote_id=None,
                    schedule_remote_id=None,
                    started_at=None,
                    ended_at=None,
                    last_seen_at=current_version,
                    connection_version=current_version,
                    remote_project_id="provider-project-1",
                    pipeline_status="launched",
                    pipeline_thread_id="thread-1",
                    pipeline_run_id="run-1",
                ),
                SimpleNamespace(
                    remote_id="stale-run",
                    status="COMPLETE",
                    terminal=True,
                    revision=1,
                    scenario_remote_id=None,
                    schedule_remote_id=None,
                    started_at=None,
                    ended_at=None,
                    last_seen_at=current_version,
                    connection_version=datetime(2026, 8, 6, tzinfo=UTC),
                    remote_project_id="provider-project-1",
                    pipeline_status="stale",
                    pipeline_thread_id=None,
                    pipeline_run_id=None,
                ),
                SimpleNamespace(
                    remote_id="missing-run",
                    status="missing",
                    terminal=False,
                    revision=1,
                    scenario_remote_id=None,
                    schedule_remote_id=None,
                    started_at=None,
                    ended_at=None,
                    last_seen_at=current_version,
                    connection_version=current_version,
                    remote_project_id="provider-project-1",
                    pipeline_status="stale",
                    pipeline_thread_id=None,
                    pipeline_run_id=None,
                ),
            ]

    control = apex_load_router.ResolvedApexLoadControl(
        adapter=cast(Any, Adapter()),
        connection_id="connection-1",
        project_id="orchestration-project-1",
        connection_version=current_version,
        remote_project_id="provider-project-1",
    )
    monkeypatch.setattr(apex_load_router, "get_apex_load_native_repository", lambda: Repository())

    response = await apex_load_router.list_mirrored_runs(control)

    assert response["items"][0]["console_url"] == "https://apexload.example/#/test/current-run"
    assert response["items"][1]["console_url"] is None
    assert response["items"][2]["console_url"] is None


async def test_console_link_proves_native_run_project_ownership_first() -> None:
    events: list[str] = []

    class Adapter:
        async def get_native_run(self, run_id: str) -> dict[str, str]:
            assert run_id == "native-run-1"
            events.append("read")
            return {"id": run_id, "project_id": "provider-project-1"}

        def native_console_url(self, run_id: str) -> str:
            assert events == ["read"]
            return f"https://apexload.example/#/test/{run_id}"

    control = apex_load_router.ResolvedApexLoadControl(
        adapter=cast(Any, Adapter()),
        connection_id="connection-1",
        project_id="orchestration-project-1",
        connection_version=datetime(2026, 8, 7, tzinfo=UTC),
        remote_project_id="provider-project-1",
    )

    response = await apex_load_router.get_run_console("native-run-1", control)

    assert response.console_url == "https://apexload.example/#/test/native-run-1"

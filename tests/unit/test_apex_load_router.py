"""APEX Load native-control router dependency regressions."""

from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any, cast

import apex.routers.apex_load as apex_load_router
from apex.adapters.apex_load.engine import ApexLoadExecutionEngine
from apex.adapters.registry import ConnectionConfig, PortKind
from apex.auth.identity import ConsumerIdentity, ConsumerType, Role, ScopeRef
from apex.domain.integrations import SecretValue


async def test_native_connection_index_filters_candidates_before_pagination() -> None:
    calls: list[dict[str, object]] = []

    class Repository:
        async def list_connections(self, **kwargs: object) -> list[object]:
            calls.append(kwargs)
            return []

    identity = ConsumerIdentity(
        consumer_id="operator-1",
        name="operator-1",
        consumer_type=ConsumerType.DASHBOARD,
        role=Role.OPERATOR,
        scopes=[ScopeRef(project_id="project-1")],
    )

    response = await apex_load_router.list_native_connections(
        identity,
        cast(Any, Repository()),
    )

    assert response.items == []
    assert calls == [
        {
            "kind": "execution_engine",
            "provider": "apex_load",
            "enabled": True,
            "native_control_enabled": True,
            "allowed_project_ids": ("project-1",),
            "limit": 500,
            "offset": 0,
        }
    ]


async def test_native_connection_index_probes_past_the_response_cap_for_truncation() -> None:
    calls: list[dict[str, object]] = []

    class Repository:
        async def list_connections(self, **kwargs: object) -> list[object]:
            calls.append(kwargs)
            limit = cast(int, kwargs["limit"])
            offset = cast(int, kwargs["offset"])
            if offset < apex_load_router.MAX_NATIVE_LIST_ITEMS:
                return [
                    SimpleNamespace(
                        id=f"connection-{offset + index}",
                        name=f"Connection {offset + index}",
                        project_id="project-1",
                    )
                    for index in range(limit)
                ]
            return []

    identity = ConsumerIdentity(
        consumer_id="operator-1",
        name="operator-1",
        consumer_type=ConsumerType.DASHBOARD,
        role=Role.OPERATOR,
        scopes=[ScopeRef(project_id="project-1")],
    )

    response = await apex_load_router.list_native_connections(identity, cast(Any, Repository()))

    assert len(response.items) == apex_load_router.MAX_NATIVE_LIST_ITEMS
    assert response.truncated is False
    assert calls[-1]["limit"] == 1
    assert calls[-1]["offset"] == apex_load_router.MAX_NATIVE_LIST_ITEMS


def test_native_resource_projection_exposes_only_the_derived_console_link() -> None:
    projection = apex_load_router._project_native_resource(
        {
            "id": "run-1",
            "name": "Checkout",
            "console_url": "https://provider.invalid/?token=unsafe",
            "provider_field": "retained",
        },
        console_url="https://apexload.example/#/test/run-1",
    )

    assert projection.console_url == "https://apexload.example/#/test/run-1"
    assert projection.details == {"provider_field": "retained"}


async def test_native_scenario_create_replays_the_durable_mapping(
    monkeypatch: Any,
) -> None:
    class Repository:
        async def replay_created_resource(self, **_kwargs: object) -> dict[str, object]:
            return {
                "id": "scenario-1",
                "name": "Checkout",
                "project_id": "remote-project-1",
                "idempotency_key": "scenario-create-1",
            }

        async def record_created_resource(self, **_kwargs: object) -> dict[str, object]:
            raise AssertionError(
                "replayed creates must not call the provider or rewrite the mapping"
            )

    class Adapter:
        async def create_native_scenario(self, *_args: object, **_kwargs: object) -> object:
            raise AssertionError("replayed creates must not call APEX Load")

    monkeypatch.setattr(
        apex_load_router,
        "get_apex_load_native_repository",
        lambda: Repository(),
    )
    control = apex_load_router.ResolvedApexLoadControl(
        adapter=cast(Any, Adapter()),
        connection_id="connection-1",
        project_id="project-1",
        connection_version=datetime(2026, 8, 7, tzinfo=UTC),
        remote_project_id="remote-project-1",
    )

    response = await apex_load_router.create_scenario(
        apex_load_router.NativeObjectRequest(payload={"name": "Checkout"}),
        control,
        "scenario-create-1",
    )

    assert response.id == "scenario-1"
    assert response.idempotency_key == "scenario-create-1"


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

    assert response.items[0].console_url == "https://apexload.example/#/test/current-run"
    assert response.items[1].console_url is None
    assert response.items[2].console_url is None


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

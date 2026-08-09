"""Native APEX Load reconciliation hand-off tests."""

from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any, cast

from apex.persistence.repositories.apex_load_native import (
    NativeConnectionClaim,
    NativeRunLaunchClaim,
    native_run_idempotency_key,
)
from apex.services import apex_load_native


async def test_terminal_run_launch_is_claimed_once_across_reconcile_retries(
    monkeypatch: Any,
) -> None:
    connection_version = datetime(2026, 8, 7, tzinfo=UTC)
    events: list[str] = []
    artifact_key = "engine-runs/" + ("a" * 64) + "/apex-load-report.json"
    claim = NativeRunLaunchClaim(
        id="mirror-1",
        connection_id="connection-1",
        connection_version=connection_version,
        project_id="project-1",
        remote_project_id="remote-project-1",
        remote_id="test-1",
        status="COMPLETE",
        summary={},
        idempotency_key="apex-load-native:claim-1",
        claim_token="claim-token",
    )

    class Repository:
        def __init__(self) -> None:
            self.claimed = False
            self.launches: list[tuple[str, str]] = []

        async def claim_enabled_connections(
            self, *, limit: int, exclude_connection_ids: frozenset[str]
        ) -> list[NativeConnectionClaim]:
            assert limit == 1
            assert not exclude_connection_ids
            return []

        async def claim_terminal_runs(
            self, *, limit: int, exclude_run_ids: frozenset[str]
        ) -> list[NativeRunLaunchClaim]:
            assert limit == 1
            if claim.id in exclude_run_ids:
                return []
            return [] if self.claimed else [claim]

        async def mark_pipeline_launched(
            self, received: NativeRunLaunchClaim, *, thread_id: str, run_id: str
        ) -> bool:
            assert received == claim
            self.claimed = True
            self.launches.append((thread_id, run_id))
            return True

        async def release_launch_claim(
            self, _claim: NativeRunLaunchClaim, *, error_kind: str
        ) -> None:
            raise AssertionError(f"unexpected retry release: {error_kind}")

        async def mark_launch_claim_stale(
            self, _claim: NativeRunLaunchClaim, *, error_kind: str
        ) -> None:
            raise AssertionError(f"unexpected stale claim: {error_kind}")

    class Adapter:
        native_project_id = "remote-project-1"

        async def aclose(self) -> None:
            return None

        async def native_run_metrics(self, run_id: str) -> dict[str, object]:
            assert run_id == "test-1"
            return {"metrics": {"throughput": 12.5, "error_pct": 2.0}}

        def native_console_url(self, run_id: str) -> str:
            assert run_id == "test-1"
            return "https://apexload.example/#/test/test-1"

        async def collect_native_artifacts(
            self, run_id: str, idempotency_key: str, _store: object
        ) -> list[dict[str, Any]]:
            assert (run_id, idempotency_key) == ("test-1", claim.idempotency_key)
            events.append("artifacts")
            return [
                {
                    "kind": "engine_report",
                    "name": "apex-load-report.json",
                    "uri": f"apex-artifact:///{artifact_key}",
                    "key": artifact_key,
                    "media_type": "application/json",
                    "summary": "APEX Load archive report for test test-1",
                }
            ]

    class Resolver:
        def __init__(self) -> None:
            self.adapter = Adapter()
            self.calls: list[str] = []

        async def resolve_with_metadata(self, kind: object, **_kwargs: object) -> object:
            self.calls.append(str(kind))
            return SimpleNamespace(
                adapter=self.adapter,
                persisted=True,
                connection_id="connection-1",
                connection_version=connection_version,
            )

    class PipelineService:
        def __init__(self) -> None:
            self.calls: list[dict[str, object]] = []
            self.generated_context_packets: list[dict[str, Any]] | None = None

        async def start_run(self, **kwargs: object) -> dict[str, str]:
            callback = cast(
                Callable[[str], Awaitable[list[dict[str, Any]] | None]],
                kwargs["before_run_start"],
            )
            self.generated_context_packets = await callback("thread-1")
            events.append("run")
            self.calls.append(kwargs)
            return {"thread_id": "thread-1", "run_id": "run-1"}

    repository = Repository()
    resolver = Resolver()
    pipeline = PipelineService()
    monkeypatch.setattr(apex_load_native, "_native_control", lambda adapter, **_kwargs: adapter)
    indexed: list[Any] = []

    async def record(references: object, **_kwargs: object) -> None:
        indexed.extend(cast(list[Any], references))

    monkeypatch.setattr(apex_load_native, "record_artifact_references", record)

    await apex_load_native.reconcile_apex_load_native_once(
        repository=cast(Any, repository),
        resolver=cast(Any, resolver),
        pipeline_service=cast(Any, pipeline),
    )
    await apex_load_native.reconcile_apex_load_native_once(
        repository=cast(Any, repository),
        resolver=cast(Any, resolver),
        pipeline_service=cast(Any, pipeline),
    )

    assert repository.launches == [("thread-1", "run-1")]
    assert events == ["artifacts", "run"]
    assert len(pipeline.calls) == 1
    assert pipeline.calls[0]["phases"] == ["reporting", "postmortem"]
    assert pipeline.calls[0]["idempotency_key"] == claim.idempotency_key
    assert [reference.artifact_key for reference in indexed] == [artifact_key]
    assert pipeline.generated_context_packets == [
        {
            "id": "apex-load-artifact-1",
            "source": "apex_load",
            "title": "apex-load-report.json",
            "summary": "APEX Load archive report for test test-1",
            "ref": f"/v1/artifacts/{artifact_key}",
        }
    ]


def test_native_run_idempotency_key_isolated_by_connection_generation() -> None:
    first = native_run_idempotency_key(
        "connection-1",
        datetime(2026, 8, 7, tzinfo=UTC),
        "remote-project-1",
        "test-1",
    )
    assert first != native_run_idempotency_key(
        "connection-1",
        datetime(2026, 8, 8, tzinfo=UTC),
        "remote-project-1",
        "test-1",
    )
    assert first != native_run_idempotency_key(
        "connection-1",
        datetime(2026, 8, 7, tzinfo=UTC),
        "remote-project-2",
        "test-1",
    )


async def test_connection_is_reconciled_only_once_per_pass(monkeypatch: Any) -> None:
    claim = NativeConnectionClaim(
        connection_id="connection-1",
        connection_version=datetime(2026, 8, 7, tzinfo=UTC),
        project_id="project-1",
        claim_token="claim-token",
    )

    class Repository:
        def __init__(self) -> None:
            self.exclusions: list[frozenset[str]] = []
            self.released: list[NativeConnectionClaim] = []

        async def claim_enabled_connections(
            self, *, limit: int, exclude_connection_ids: frozenset[str]
        ) -> list[NativeConnectionClaim]:
            assert limit == 1
            self.exclusions.append(exclude_connection_ids)
            return [] if claim.connection_id in exclude_connection_ids else [claim]

        async def release_connection_claim(self, received: NativeConnectionClaim) -> None:
            self.released.append(received)

        async def claim_terminal_runs(
            self, *, limit: int, exclude_run_ids: frozenset[str]
        ) -> list[NativeRunLaunchClaim]:
            assert limit == 1
            assert not exclude_run_ids
            return []

    syncs: list[NativeConnectionClaim] = []

    async def sync_connection(**kwargs: object) -> int:
        syncs.append(cast(NativeConnectionClaim, kwargs["claim"]))
        return 1

    repository = Repository()
    monkeypatch.setattr(apex_load_native, "_sync_connection", sync_connection)

    assert await apex_load_native.reconcile_apex_load_native_once(
        repository=cast(Any, repository),
        resolver=cast(Any, object()),
        pipeline_service=cast(Any, object()),
    ) == 1
    assert repository.exclusions == [frozenset(), frozenset({"connection-1"})]
    assert repository.released == [claim]
    assert syncs == [claim]


async def test_connection_version_drift_stales_claim_without_starting_pipeline(
    monkeypatch: Any,
) -> None:
    claim = NativeRunLaunchClaim(
        id="mirror-1",
        connection_id="connection-1",
        connection_version=datetime(2026, 8, 7, tzinfo=UTC),
        project_id="project-1",
        remote_project_id="remote-project-1",
        remote_id="test-1",
        status="COMPLETE",
        summary={},
        idempotency_key="apex-load-native:claim-1",
        claim_token="claim-token",
    )

    class Repository:
        def __init__(self) -> None:
            self.stale: list[tuple[NativeRunLaunchClaim, str]] = []

        async def mark_launch_claim_stale(
            self, received: NativeRunLaunchClaim, *, error_kind: str
        ) -> None:
            self.stale.append((received, error_kind))

        async def release_launch_claim(
            _self, _claim: NativeRunLaunchClaim, *, error_kind: str
        ) -> None:
            raise AssertionError(f"unexpected retry release: {error_kind}")

    class Adapter:
        native_project_id = "remote-project-1"

        async def aclose(self) -> None:
            return None

    class Resolver:
        async def resolve_with_metadata(self, _kind: object, **_kwargs: object) -> object:
            return SimpleNamespace(
                adapter=Adapter(),
                persisted=True,
                connection_id="connection-1",
                connection_version=datetime(2026, 8, 8, tzinfo=UTC),
            )

    class PipelineService:
        async def start_run(self, **_kwargs: object) -> dict[str, str]:
            raise AssertionError("version-drifted run must not start a pipeline")

    repository = Repository()
    monkeypatch.setattr(apex_load_native, "_native_control", lambda adapter: adapter)

    assert not await apex_load_native._launch_reporting_pipeline(
        repository=cast(Any, repository),
        resolver=cast(Any, Resolver()),
        pipeline_service=cast(Any, PipelineService()),
        claim=claim,
    )
    assert repository.stale == [(claim, "connection_version_drift")]

"""HA-safe polling and reporting hand-off for opted-in APEX Load control planes."""

import asyncio
import math
from collections.abc import Callable
from datetime import datetime
from functools import lru_cache
from typing import Any, cast
from urllib.parse import quote

import structlog

from apex.adapters.registry import PortKind
from apex.domain.diagnostics import safe_type_name
from apex.domain.pipeline import ArtifactRef
from apex.persistence.db import get_sessionmaker
from apex.persistence.repositories.apex_load_native import (
    ApexLoadNativeRepository,
    NativeConnectionClaim,
    NativeRunLaunchClaim,
)
from apex.ports.apex_load_control import ApexLoadControlPlanePort
from apex.services.artifact_references import ArtifactReferenceInput, record_artifact_references
from apex.services.connections import ConnectionResolver, close_adapter, get_connection_resolver
from apex.services.langgraph_client import loopback_client
from apex.services.pipeline_read import PipelineReadService

logger = structlog.get_logger(__name__)

RECONCILE_INTERVAL_S = 30.0
NATIVE_CONNECTION_RECONCILE_TIMEOUT_S = 110.0
NATIVE_PIPELINE_LAUNCH_TIMEOUT_S = 11 * 60.0
MAX_NATIVE_CONNECTIONS_PER_PASS = 500
MAX_NATIVE_PIPELINE_LAUNCHES_PER_PASS = 100


@lru_cache
def get_apex_load_native_repository() -> ApexLoadNativeRepository:
    return ApexLoadNativeRepository(get_sessionmaker())


def _native_control(adapter: Any) -> ApexLoadControlPlanePort:
    if not isinstance(adapter, ApexLoadControlPlanePort):
        raise ValueError("resolved adapter does not expose APEX Load native control")
    if not adapter.native_control_enabled:
        raise ValueError("APEX Load native control is disabled")
    if adapter.native_project_id is None:
        raise ValueError("APEX Load provider project binding is missing")
    return adapter


def _metrics_kpis(value: Any) -> dict[str, float]:
    """Normalize only the small reporting KPI set; rich metrics stay provider-side."""

    source = (
        value.get("metrics")
        if type(value) is dict and type(value.get("metrics")) is dict
        else value
    )
    if type(source) is not dict:
        return {}

    def number(*names: str) -> float | None:
        for name in names:
            candidate = source.get(name)
            if type(candidate) in {int, float}:
                parsed = float(cast(int | float, candidate))
                if math.isfinite(parsed) and 0 <= parsed <= 1_000_000_000:
                    return parsed
        return None

    result: dict[str, float] = {}
    for key, names in (
        ("tps_avg", ("tps_avg", "average_tps", "throughput", "rps")),
        ("p95_ms", ("p95_ms", "p95_latency_ms", "latency_p95_ms")),
        ("vusers_peak", ("vusers_peak", "active_vusers", "virtual_users")),
    ):
        metric = number(*names)
        if metric is not None:
            result[key] = metric
    error_rate = number("error_rate", "error_pct", "errors_percent")
    if error_rate is not None:
        result["error_rate"] = error_rate / 100.0 if error_rate > 1 else error_rate
    return result


def _external_results(
    claim: NativeRunLaunchClaim,
    *,
    console_url: str,
    metrics: Any,
) -> dict[str, Any]:
    return {
        "source": "APEX Load native control plane",
        "uri": console_url,
        "engine": "apex_load",
        "passed": claim.status == "COMPLETE",
        "kpis": _metrics_kpis(metrics),
        "summary": f"APEX Load native test {claim.remote_id} completed with status {claim.status}",
        "notes": "Reporting and postmortem were started from the mirrored native run.",
    }


def _artifact_context_packets(artifact_refs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Expose collected evidence to reporting through capability-free artifact links."""

    packets: list[dict[str, Any]] = []
    for index, raw_ref in enumerate(artifact_refs, start=1):
        if type(raw_ref) is not dict:
            raise RuntimeError("APEX Load artifact collection returned an invalid reference")
        ref = ArtifactRef.model_validate(raw_ref)
        if ref.key is None:
            raise RuntimeError("APEX Load artifact collection returned a reference without a key")
        packet: dict[str, Any] = {
            "id": f"apex-load-artifact-{index}",
            "source": "apex_load",
            "title": ref.name,
            "ref": f"/v1/artifacts/{quote(ref.key, safe='/-._~')}",
        }
        if ref.summary is not None:
            packet["summary"] = ref.summary
        packets.append(packet)
    return packets


async def _sync_connection(
    *,
    repository: ApexLoadNativeRepository,
    resolver: ConnectionResolver,
    claim: NativeConnectionClaim,
) -> int:
    resolved = None
    try:
        resolved = await resolver.resolve_with_metadata(
            PortKind.EXECUTION_ENGINE,
            connection_id=claim.connection_id,
            project_id=claim.project_id,
            expected_provider="apex_load",
        )
        if not resolved.persisted or resolved.connection_id != claim.connection_id:
            raise ValueError("APEX Load connection affinity changed")
        connection_version = resolved.connection_version
        if (
            not isinstance(connection_version, datetime)
            or connection_version != claim.connection_version
        ):
            raise ValueError("APEX Load connection generation changed before reconciliation")
        adapter = _native_control(resolved.adapter)
        remote_project_id = adapter.native_project_id
        if remote_project_id is None:  # pragma: no cover - _native_control invariant
            raise ValueError("APEX Load provider project binding is missing")
        run_snapshot = await adapter.scan_native_runs()
        synced = await repository.sync_runs(
            connection_id=claim.connection_id,
            project_id=claim.project_id,
            remote_project_id=remote_project_id,
            connection_version=connection_version,
            claim_token=claim.claim_token,
            runs=run_snapshot.items,
            complete=run_snapshot.complete,
        )
        # Scenario/schedule mirrors are an audit aid. A transient authoring API
        # failure must not block terminal-run reporting after run reconciliation.
        try:
            scenarios = await adapter.list_native_scenarios()
            await repository.sync_scenarios(
                connection_id=claim.connection_id,
                project_id=claim.project_id,
                remote_project_id=remote_project_id,
                connection_version=connection_version,
                claim_token=claim.claim_token,
                scenarios=scenarios,
            )
            for scenario in scenarios:
                scenario_id = scenario.get("id")
                if type(scenario_id) is not str:
                    raise ValueError("APEX Load scenario response has no id")
                schedules = await adapter.list_native_schedules(scenario_id)
                await repository.sync_schedules(
                    connection_id=claim.connection_id,
                    project_id=claim.project_id,
                    remote_project_id=remote_project_id,
                    connection_version=connection_version,
                    claim_token=claim.claim_token,
                    scenario_remote_id=scenario_id,
                    schedules=schedules,
                )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning(
                "apex_load_native.authoring_mirror_failed",
                connection_id=claim.connection_id,
                error_type=safe_type_name(exc),
            )
        return synced
    finally:
        if resolved is not None:
            await close_adapter(resolved.adapter)


async def _launch_reporting_pipeline(
    *,
    repository: ApexLoadNativeRepository,
    resolver: ConnectionResolver,
    pipeline_service: PipelineReadService,
    claim: NativeRunLaunchClaim,
) -> bool:
    resolved = None
    try:
        resolved = await resolver.resolve_with_metadata(
            PortKind.EXECUTION_ENGINE,
            connection_id=claim.connection_id,
            project_id=claim.project_id,
            expected_provider="apex_load",
        )
        if not resolved.persisted or resolved.connection_id != claim.connection_id:
            raise ValueError("APEX Load launch connection affinity changed")
        adapter = _native_control(resolved.adapter)
        if (
            resolved.connection_version != claim.connection_version
            or adapter.native_project_id != claim.remote_project_id
        ):
            await repository.mark_launch_claim_stale(claim, error_kind="connection_version_drift")
            return False
        metrics: Any = {}
        try:
            metrics = await adapter.native_run_metrics(claim.remote_id)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning(
                "apex_load_native.metrics_unavailable",
                connection_id=claim.connection_id,
                remote_run_id=claim.remote_id,
                error_type=safe_type_name(exc),
            )

        async def collect_and_index_artifacts(thread_id: str) -> list[dict[str, Any]]:
            artifact_resolution = await resolver.resolve_with_metadata(
                PortKind.ARTIFACT_STORE,
                project_id=claim.project_id,
            )
            try:
                artifact_refs = await adapter.collect_native_artifacts(
                    claim.remote_id,
                    claim.idempotency_key,
                    artifact_resolution.adapter,
                )
                await record_artifact_references(
                    [
                        ArtifactReferenceInput(artifact_key=ref["key"], kind=ref["kind"])
                        for ref in artifact_refs
                    ],
                    connection_id=artifact_resolution.connection_id,
                    thread_id=thread_id,
                    project_id=claim.project_id,
                    app_id=None,
                )
                return _artifact_context_packets(artifact_refs)
            finally:
                await close_adapter(artifact_resolution.adapter)

        result = await pipeline_service.start_run(
            title=f"APEX Load report: {claim.remote_id}",
            request=(
                "Analyze the completed APEX Load native run and produce reporting plus postmortem."
            ),
            idempotency_key=claim.idempotency_key,
            project_id=claim.project_id,
            phases=["reporting", "postmortem"],
            external_results=_external_results(
                claim,
                console_url=adapter.native_console_url(claim.remote_id),
                metrics=metrics,
            ),
            principal_id="apex-load-reconciler",
            before_run_start=collect_and_index_artifacts,
        )
        thread_id = result.get("thread_id") if type(result) is dict else None
        run_id = result.get("run_id") if type(result) is dict else None
        if type(thread_id) is not str or type(run_id) is not str:
            raise RuntimeError("pipeline launch returned invalid identifiers")
        return await repository.mark_pipeline_launched(claim, thread_id=thread_id, run_id=run_id)
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        logger.warning(
            "apex_load_native.pipeline_launch_failed",
            connection_id=claim.connection_id,
            remote_run_id=claim.remote_id,
            error_type=safe_type_name(exc),
        )
        await repository.release_launch_claim(claim, error_kind=safe_type_name(exc))
        return False
    finally:
        if resolved is not None:
            await close_adapter(resolved.adapter)


async def reconcile_apex_load_native_once(
    *,
    repository: ApexLoadNativeRepository | None = None,
    resolver: ConnectionResolver | None = None,
    pipeline_service: PipelineReadService | None = None,
    heartbeat: Callable[[], None] | None = None,
) -> int:
    """Poll opted-in connections and launch one idempotent analysis pipeline per terminal run."""

    repository = repository or get_apex_load_native_repository()
    resolver = resolver or get_connection_resolver()
    pipeline_service = pipeline_service or PipelineReadService(loopback_client())
    synced = 0
    if heartbeat is not None:
        heartbeat()
    visited_connection_ids: set[str] = set()
    while len(visited_connection_ids) < MAX_NATIVE_CONNECTIONS_PER_PASS:
        claims = await repository.claim_enabled_connections(
            limit=1,
            exclude_connection_ids=frozenset(visited_connection_ids),
        )
        if not claims:
            break
        claim = claims[0]
        visited_connection_ids.add(claim.connection_id)
        if heartbeat is not None:
            heartbeat()
        try:
            async with asyncio.timeout(NATIVE_CONNECTION_RECONCILE_TIMEOUT_S):
                synced += await _sync_connection(
                    repository=repository,
                    resolver=resolver,
                    claim=claim,
                )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning(
                "apex_load_native.connection_reconcile_failed",
                connection_id=claim.connection_id,
                error_type=safe_type_name(exc),
            )
        finally:
            await repository.release_connection_claim(claim)
    visited_run_ids: set[str] = set()
    while len(visited_run_ids) < MAX_NATIVE_PIPELINE_LAUNCHES_PER_PASS:
        claims = await repository.claim_terminal_runs(
            limit=1,
            exclude_run_ids=frozenset(visited_run_ids),
        )
        if not claims:
            break
        claim = claims[0]
        visited_run_ids.add(claim.id)
        if heartbeat is not None:
            heartbeat()
        try:
            async with asyncio.timeout(NATIVE_PIPELINE_LAUNCH_TIMEOUT_S):
                await _launch_reporting_pipeline(
                    repository=repository,
                    resolver=resolver,
                    pipeline_service=pipeline_service,
                    claim=claim,
                )
        except TimeoutError:
            logger.warning(
                "apex_load_native.pipeline_launch_timed_out",
                connection_id=claim.connection_id,
                remote_run_id=claim.remote_id,
            )
            await repository.release_launch_claim(claim, error_kind="TimeoutError")
    return synced


async def run_apex_load_native_reconciler(
    stop: asyncio.Event,
    heartbeat: Callable[[], None] | None = None,
) -> None:
    while not stop.is_set():
        try:
            await reconcile_apex_load_native_once(heartbeat=heartbeat)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning(
                "apex_load_native.reconciler_failed",
                error_type=safe_type_name(exc),
            )
        if heartbeat is not None:
            heartbeat()
        try:
            await asyncio.wait_for(stop.wait(), timeout=RECONCILE_INTERVAL_S)
        except TimeoutError:
            pass

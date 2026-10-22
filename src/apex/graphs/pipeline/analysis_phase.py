"""Checkpointed analysis submission, polling, collection and recovery."""

import asyncio
import hashlib
import json
from datetime import UTC, datetime
from typing import Any

from langchain_core.runnables import RunnableConfig
from langgraph.types import Command, interrupt

from apex.adapters.registry import PortKind
from apex.domain.diagnostics import bounded_diagnostic
from apex.domain.pipeline import ArtifactRef, Phase, utcnow_iso
from apex.graphs.pipeline.configurable import PipelineConfigurable
from apex.graphs.pipeline.gates import parse_gate_decision, resolve_actor
from apex.graphs.pipeline.state import PipelineState
from apex.ports.analysis import AnalysisJob, AnalysisResult
from apex.services.artifact_references import persist_artifact_with_intent
from apex.services.connections import ConnectionResolver, DbConnectionStore, close_adapter
from apex.services.native_analysis_submissions import (
    NativeAnalysisScope,
    NativeAnalysisSubmissionUncertain,
    claim_native_analysis,
    record_native_analysis_receipt,
)


def _make_analysis_resolver():
    return ConnectionResolver(store=DbConnectionStore())


def _entry(state):
    return state["phase_results"]["reporting"]


def _update(state, **fields) -> dict[str, Any]:
    return {"phase_results": {"reporting": {"attempt": _entry(state)["attempt"], **fields}}}


def _ordered(cfg):
    return (
        sorted(
            cfg.analysis.selections,
            key=lambda s: {"lra_engine": 0, "internal_rca": 0, "perfreportforge": 1, "other": 2}[
                s.tool
            ],
        )
        if cfg.analysis
        else []
    )


def _execution_evidence(state: PipelineState, cfg: PipelineConfigurable) -> dict[str, Any]:
    """Read inputs already frozen in the durable run and accepted execution handoff."""
    execution = state.get("phase_results", {}).get("execution", {})
    external = execution.get("external") is True
    # An imported execution can replace a prior managed execution without
    # clearing its top-level handle (e.g. a reporting-only rerun). Only the
    # server-verified human handoff can bind imported evidence to native RCA.
    handle = {} if external else execution.get("engine_handle") or state.get("engine_handle") or {}
    verified_native = execution.get("native_source_verified") is True
    return {
        "test_scope": cfg.test_scope_snapshot,
        "attribution": "run-level",
        "test_summary": execution.get("test_summary"),
        "source_uri": execution.get("source_uri"),
        "artifact_ids": execution.get("artifact_ids", []),
        "native_run_id": handle.get("external_run_id")
        or (execution.get("native_run_id") if verified_native else None),
        "native_connection_id": handle.get("connection_id")
        or (execution.get("native_connection_id") if verified_native else None),
    }


def _submission_request(state, config, cfg, row):
    # Keep already-checkpointed requests readable. New ledgers refer to the
    # immutable upstream handoff instead of duplicating its potentially large
    # evidence once per connector. Verify the digest before every remote call.
    if row.get("request_json"):
        return json.loads(row["request_json"])
    evidence = _execution_evidence(state, cfg)
    digest = hashlib.sha256(json.dumps(evidence, sort_keys=True).encode()).hexdigest()
    if digest != row["input_digest"]:
        raise ValueError("Analysis input evidence changed after preparation")
    return {
        "schema_version": 1,
        "run_id": config.get("configurable", {})["thread_id"],
        "phase": "reporting",
        "attempt": _entry(state)["attempt"],
        "project_id": cfg.project_id,
        "engine": cfg.engine,
        "evidence": evidence,
        "input_digest": digest,
        "idempotency_key": row["idempotency_key"],
    }


def prepare_analysis(state: PipelineState, config: RunnableConfig) -> dict:
    cfg = PipelineConfigurable.from_state_for_phase(state, config, Phase.REPORTING)
    entry = _entry(state)
    if entry.get("analysis_ledger") is not None or not cfg.analysis or not cfg.analysis.selections:
        return {}
    evidence = _execution_evidence(state, cfg)
    digest = hashlib.sha256(json.dumps(evidence, sort_keys=True).encode()).hexdigest()
    ledger = []
    thread_id = config.get("configurable", {})["thread_id"]
    for selection in _ordered(cfg):
        key = hashlib.sha256(
            f"{thread_id}:reporting:{entry['attempt']}:{selection.connection_id}:{digest}:1".encode()
        ).hexdigest()
        ledger.append(
            {
                **selection.model_dump(),
                "input_digest": digest,
                "attempt": 1,
                "idempotency_key": key,
                "status": "pending",
                "job_id": None,
                "started_at": None,
                "poll_count": 0,
                "error": None,
                "provider_terminal_failure": False,
            }
        )
    return _update(state, analysis_ledger=ledger)


def route_analysis(state: PipelineState) -> str:
    entry = _entry(state)
    if state.get("run_aborted"):
        return "finalize"
    rows = entry.get("analysis_ledger", [])
    if any(row["status"] == "failed" for row in rows):
        return "analysis_recovery"
    return "report_agent" if all(row["status"] == "succeeded" for row in rows) else "analysis_step"


async def analysis_step(state: PipelineState, config: RunnableConfig) -> dict:
    cfg = PipelineConfigurable.from_state_for_phase(state, config, Phase.REPORTING)
    ledger = [dict(row) for row in _entry(state)["analysis_ledger"]]
    row = next(row for row in ledger if row["status"] != "succeeded")
    resolver = _make_analysis_resolver()
    adapter = store = None
    artifacts = []
    raw: dict[str, Any] = {}
    native_submit_started = False
    native_scope = None
    assert cfg.analysis is not None
    try:
        request = _submission_request(state, config, cfg, row)
        if row["started_at"] is None:
            row["started_at"] = utcnow_iso()
            return _update(state, analysis_ledger=ledger)
        elapsed = (datetime.now(UTC) - datetime.fromisoformat(row["started_at"])).total_seconds()
        if elapsed > cfg.analysis.timeout_s:
            raise ValueError("Analysis deadline expired")
        native = row["tool"] == "internal_rca"
        adapter, _ = await resolver.resolve_with_connection_id(
            PortKind.EXECUTION_ENGINE if native else PortKind.ANALYSIS,
            connection_id=row["connection_id"],
            project_id=cfg.traffic_project_id if native else cfg.project_id,
            expected_provider="apex_load" if native else row["tool"],
        )
        if native:
            native_id = request["evidence"]["native_run_id"]
            if not native_id:
                raise ValueError("Internal RCA requires the native APEX Load run identity")
            if request["evidence"].get("native_connection_id") != row["connection_id"]:
                raise ValueError("Internal RCA requires verified execution connection provenance")
            current_evidence = _execution_evidence(state, cfg)
            if any(
                current_evidence.get(field) != request["evidence"].get(field)
                for field in ("native_run_id", "native_connection_id")
            ):
                raise ValueError("Internal RCA execution provenance changed after preparation")
            external = state.get("phase_results", {}).get("execution", {}).get("external") is True
            handle = {} if external else state.get("engine_handle") or {}
            if handle.get("connection_id") and handle["connection_id"] != row["connection_id"]:
                raise ValueError("Internal RCA must use the execution run's connection")
            if row["job_id"]:
                raw = await adapter.get_native_analysis(native_id, row["job_id"])
            else:
                native_scope = NativeAnalysisScope(
                    thread_id=request["run_id"],
                    phase_attempt=_entry(state)["attempt"],
                    project_id=cfg.traffic_project_id,
                    connection_id=row["connection_id"],
                    native_run_id=native_id,
                    input_digest=row["input_digest"],
                    idempotency_key=row["idempotency_key"],
                )
                received_job_id = await claim_native_analysis(native_scope)
                if received_job_id is not None:
                    row.update(job_id=received_job_id, status="queued")
                    return _update(state, analysis_ledger=ledger)
                native_submit_started = True
                raw = await adapter.submit_native_analysis(
                    native_id, idempotency_key=row["idempotency_key"]
                )
            job = AnalysisJob.model_validate(
                {
                    "job_id": raw.get("analysis_id"),
                    "status": "succeeded" if raw.get("state") == "completed" else raw.get("state"),
                    "error": raw.get("error"),
                }
            )
            if native_scope is not None:
                try:
                    await record_native_analysis_receipt(native_scope, job.job_id)
                except Exception:
                    # If the graph can checkpoint despite a receipt-store outage,
                    # retain the validated identity instead of losing it again.
                    row.update(job_id=job.job_id, status="queued")
                    raise
        elif row["job_id"]:
            job = await adapter.status(row["job_id"])
        else:
            cap = await adapter.capabilities()
            if not cap.available or cfg.engine not in cap.engines:
                raise ValueError("Analysis connector does not support this engine")
            if "test_summary" not in cap.evidence_kinds:
                raise ValueError("Analysis connector does not accept test summary evidence")
            job = await adapter.submit(request)
        if row["job_id"] is None:
            row.update(job_id=job.job_id, status="queued")
            return _update(state, analysis_ledger=ledger)
        if job.job_id != row["job_id"]:
            raise ValueError("Analysis provider returned a different job identity")
        if job.status in {"failed", "cancelled"}:
            row["provider_terminal_failure"] = True
            raise ValueError(job.error or f"Analysis {job.status}")
        row["status"] = "collecting" if job.status == "succeeded" else job.status
        if job.status == "succeeded":
            if native:
                assessment = raw.get("assessment")
                if not assessment or raw.get("archive_changed"):
                    raise ValueError("RCA returned no current assessment")
                result = AnalysisResult(
                    schema_version=1,
                    summary="APEX Load RCA assessment",
                    tool="internal_rca",
                    tool_version="native-v2",
                    input_digest=row["input_digest"],
                    findings=[assessment],
                )
            else:
                result = await adapter.result(row["job_id"])
            if result.tool != row["tool"]:
                raise ValueError("Analysis results belong to a different tool")
            if result.input_digest != row["input_digest"]:
                raise ValueError("Analysis results belong to different input evidence")
            participant_ids = (
                {p.id for p in cfg.test_scope.participants} if cfg.test_scope else set()
            )
            for finding in result.findings:
                participant_id = finding.get("participant_id")
                if participant_id is not None and participant_id not in participant_ids:
                    raise ValueError("Analysis finding references a system outside this test")
            content = result.model_dump_json()
            store, store_id = await resolver.resolve_with_connection_id(
                PortKind.ARTIFACT_STORE,
                connection_id=cfg.connections.get("artifact_store"),
                project_id=cfg.project_id,
            )
            artifact_id = "analysis-" + row["idempotency_key"]
            stored = await persist_artifact_with_intent(
                store,
                artifact_key=f"analysis/{request['run_id']}/{row['idempotency_key']}.json",
                connection_id=store_id,
                kind="analysis",
                thread_id=request["run_id"],
                project_id=cfg.project_id,
                app_id=cfg.app_id,
                payload=content.encode(),
                content_type="application/json",
            )
            artifacts = [
                ArtifactRef(
                    id=artifact_id,
                    kind="analysis",
                    name=f"{row['tool']} analysis",
                    key=stored.key,
                    uri=stored.uri,
                    artifact_connection_id=store_id,
                    media_type="application/json",
                    summary=result.summary[:2000],
                ).model_dump(mode="json")
            ]
            row.update(
                status="succeeded",
                result_preview=json.dumps(
                    {"findings": result.findings, "warnings": result.warnings, "gaps": result.gaps}
                )[:2000],
                result_artifact_id=artifact_id,
                result_content_sha256=hashlib.sha256(content.encode()).hexdigest(),
                result_summary=result.summary[:2000],
                completed_at=utcnow_iso(),
                error=None,
            )
        else:
            row["poll_count"] += 1
            await asyncio.sleep(min(5 * 2 ** min(row["poll_count"] - 1, 3), 30))
    except Exception as exc:
        import httpx

        from apex.adapters.apex_load.engine import _AmbiguousResponseError, _TransportRequestError

        transient = (
            isinstance(exc, (_TransportRequestError, _AmbiguousResponseError))
            or isinstance(exc, (httpx.TransportError, httpx.HTTPStatusError))
            and (
                not isinstance(exc, httpx.HTTPStatusError)
                or exc.response.status_code in {408, 429, 500, 502, 503, 504}
            )
        )
        row["error"] = bounded_diagnostic(exc, max_chars=4096)
        if isinstance(exc, NativeAnalysisSubmissionUncertain) or (
            native_submit_started and row["job_id"] is None
        ):
            # Native frozen-input deduplication excludes failed/cancelled jobs
            # (and may repeat budget-limited assessments). An uncertain POST
            # must not silently create another job on the next graph step.
            row["submission_uncertain"] = True
            row["status"] = "failed"
            row["error"] = (
                "Native RCA submission outcome is unconfirmed. A remote job may exist; "
                "retry explicitly starts a new attempt and may create another job. " + row["error"]
            )[:4096]
        elif transient:
            row["poll_count"] += 1
            await asyncio.sleep(min(5 * 2 ** min(row["poll_count"], 3), 30))
        else:
            row["status"] = "failed"
    finally:
        try:
            if store is not None:
                await close_adapter(store)
        finally:
            try:
                if adapter is not None:
                    await close_adapter(adapter)
            finally:
                await resolver.close()
    import structlog

    structlog.get_logger(__name__).info(
        "analysis_job_transition",
        tool=row["tool"],
        connection_id=row["connection_id"],
        status=row["status"],
        attempt=row["attempt"],
        poll_count=row["poll_count"],
    )
    update = _update(state, analysis_ledger=ledger)
    if artifacts:
        update["artifacts"] = artifacts
        update["phase_results"]["reporting"]["artifact_ids"] = [a["id"] for a in artifacts]
    return update


def analysis_recovery(state: PipelineState, config: RunnableConfig):
    PipelineConfigurable.from_state_for_phase(state, config, Phase.REPORTING)
    entry = _entry(state)
    ledger = [dict(row) for row in entry.get("analysis_ledger", [])]
    row = next((row for row in ledger if row["status"] == "failed"), None)
    if row is None and (
        entry.get("report_failure") is not True
        or entry.get("status") != "running"
        or any(item["status"] != "succeeded" for item in ledger)
    ):
        raise ValueError("Reporting recovery requires failed report assembly or analysis")
    reason = (
        row.get("error")
        if row is not None
        else (entry.get("errors") or [entry.get("reasoning_digest")])[-1]
    )
    reason = bounded_diagnostic(reason or "Report assembly failed", max_chars=4096)
    payload = {
        "schema_version": 1,
        "kind": "analysis_recovery",
        "phase": "reporting",
        "actions": ["retry", "assign_hitl", "abort"],
        "message": f"{row['tool']} analysis needs attention"
        if row is not None
        else "Reporting & Analysis report assembly needs attention",
        "error": reason,
    }
    while True:
        decision = parse_gate_decision(interrupt(payload), payload["actions"])
        if row is None and decision["action"] == "retry":
            revision = entry.get("revise_count", 0)
            if type(revision) is not int or not 0 <= revision < 10:
                payload["error"] = "Report retry limit reached; assign HITL or abort"
                continue
            return Command(
                goto="report_agent",
                update=_update(
                    state, status="running", report_failure=False, revise_count=revision + 1
                ),
            )
        if decision["action"] is not None:
            break
        payload["error"] = decision["error"]
    action = decision["action"]
    if action == "retry":
        assert row is not None
        # Resume a known job after collection/polling/deadline failures. Only an
        # explicitly failed remote job or operator-accepted ambiguous native
        # submission authorizes a new submission attempt.
        if row.get("provider_terminal_failure") or row.get("submission_uncertain"):
            row["attempt"] += 1
            row["idempotency_key"] = hashlib.sha256(
                f"{row['idempotency_key']}:{row['attempt']}".encode()
            ).hexdigest()
            if row.get("request_json"):
                request = json.loads(row["request_json"])
                request["idempotency_key"] = row["idempotency_key"]
                row["request_json"] = json.dumps(request)
            row.update(job_id=None, provider_terminal_failure=False, submission_uncertain=False)
        row.update(status="pending", started_at=None, error=None)
        return Command(goto="analysis_step", update=_update(state, analysis_ledger=ledger))
    if action == "assign_hitl":
        return Command(
            goto="open_human_task",
            update=_update(
                state,
                executor="hitl",
                report_failure=False,
                ownership_override={
                    "owner": "hitl",
                    "actor": resolve_actor(config),
                    "at": utcnow_iso(),
                    "reason": (decision.get("note") or "").strip() or reason,
                },
            ),
        )
    if action == "abort":
        return Command(
            goto="finalize", update={"run_aborted": True, **_update(state, status="aborted")}
        )
    raise ValueError("Unsupported analysis recovery action")

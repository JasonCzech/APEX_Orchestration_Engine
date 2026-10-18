"""Exact cycle bindings for the checkpointed engine lifecycle, never ad-hoc starts."""

import hashlib
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select, update

from apex.auth.identity import ConsumerIdentity, ConsumerType, Role, ScopeRef
from apex.domain.cycle_dispatch import CycleDispatchRead
from apex.domain.cycle_executions import ExecutionSnapshot, PreflightCheck, execution_checks
from apex.domain.integrations import LoadTestSpec
from apex.graphs.pipeline.configurable import PipelineConfigurable
from apex.persistence.db import get_sessionmaker
from apex.persistence.models import (
    ApiConsumer,
    Connection,
    ConsumerScope,
    CycleExecutionDispatch,
    CycleExecutionPreparation,
    CycleScriptValidation,
    EngineRun,
)
from apex.services.connections import close_adapter, get_connection_resolver
from apex.services.cycle_scripts import read_verified_script
from apex.settings import get_settings

TERMINAL_DISPATCH = frozenset({"completed", "failed", "aborted"})


async def dispatch_context(
    session: Any, identity: ConsumerIdentity, cycle_id: str, preparation_id: str
):
    # Shared validation routines own the same scoped source and target contract
    # used by preparation and provider validation. Imported lazily to keep the
    # checkpoint runtime independent of router import order.
    from apex.routers.cycle_executions import context
    from apex.routers.cycle_script_validation import validation_context, validation_view

    cycle, snapshot, artifact, blockers = await validation_context(
        cycle_id, preparation_id, session, identity
    )
    if identity.role.at_least(Role.OPERATOR):
        try:
            current_actor = await _current_actor(session, identity.consumer_id)
            if not current_actor.allows_scope(project_id=cycle.project_id, app_id=cycle.app_id):
                raise ValueError("Scope changed")
        except ValueError:
            blockers.append(
                "Dispatch requires a persisted, enabled operator consumer authorized for this "
                "application. Bootstrap and development identities cannot own durable launches."
            )
    target, connections, inventory = await context(session, cycle)
    connection = next((c for c in connections if c.id == snapshot.connection_id), None)
    checks = [
        c
        for c in execution_checks(
            snapshot,
            cycle.versions,
            target_version=target.version if target else None,
            target_valid=target is not None,
            connection_valid=connection is not None,
            connection_version=connection.runtime_version.isoformat() if connection else None,
            inventory_id=inventory.id if inventory else None,
            inventory_at=inventory.scanned_at if inventory else None,
        )
        if c.key not in {"provider_access", "script_binding", "launch_authority"}
    ]
    if snapshot.launch_authority != "pipeline":
        blockers.append(
            "The native schedule owns this preparation; pipeline dispatch would "
            "duplicate its launch."
        )
    if not get_settings().cycles_enabled:
        blockers.append("Cycle execution is disabled.")
    engine = await session.scalar(select(Connection).where(Connection.id == snapshot.connection_id))
    options = engine.options if engine is not None else {}
    if (
        not options
        or options.get("native_control_enabled") is not True
        or not options.get("project_id")
    ):
        blockers.append(
            "Enable APEX Load native control with an explicit provider project to "
            "check capacity before launch."
        )
    if options and options.get("release_mapping"):
        blockers.append(
            "Mapped release execution requires an approved release binding; this "
            "cycle contract does not yet provide one."
        )
    latest = await session.scalar(
        select(CycleScriptValidation)
        .where(CycleScriptValidation.preparation_id == preparation_id)
        .order_by(CycleScriptValidation.created_at.desc(), CycleScriptValidation.id)
        .limit(1)
    )
    if latest is None or not validation_view(latest, blockers).current:
        blockers.append(
            "Validate this preparation's exact stored source successfully within the "
            "last 24 hours before dispatch."
        )
    if not blockers and not any(c.status == "blocked" for c in checks) and target is not None:
        from apex.adapters.apex_load.engine import qualify_approved_http_source

        script = next(v for v in cycle.versions if v["id"] == snapshot.script_version_id)
        source = script["content"].get("source")
        if (
            type(source) is not str
            or hashlib.sha256(source.encode()).hexdigest() != snapshot.script_source_checksum
        ):
            blockers.append("Approved source bytes no longer match the preparation checksum.")
        else:
            try:
                qualify_approved_http_source(
                    binding_spec(snapshot, target.base_url, "preview-execution-a1"), source
                )
            except ValueError as exc:
                blockers.append(str(exc))
    checks.extend(
        PreflightCheck(
            key=f"dispatch_{index}", label="Dispatch prerequisite", status="blocked", detail=detail
        )
        for index, detail in enumerate(blockers)
    )
    checks.append(
        PreflightCheck(
            key="provider_access",
            label="Live provider checks",
            status="not_checked",
            detail=(
                "Launch revalidates exact source, current approvals and provider capacity "
                "before starting. Capacity is not reserved by this preview."
            ),
        )
    )
    return cycle, snapshot, artifact, target, checks


def dispatch_read(
    row: CycleExecutionDispatch, engine: EngineRun | None = None
) -> CycleDispatchRead:
    detail = {
        "reserved": (
            "Launch intent saved. No pipeline acknowledgement has been recorded; resume "
            "this same intent."
        ),
        "uncertain": (
            "Pipeline acknowledgement is uncertain. Resume this same intent to reconcile "
            "it; do not create another launch."
        ),
        "submitted": (
            "Pipeline accepted. Provider validation, provisioning and execution continue "
            "in the linked run."
        ),
        "running": (
            "The linked engine execution is active. Use the run's Abort action to request "
            "a confirmed stop."
        ),
        "cleanup_pending": (
            "Execution is awaiting result collection or confirmed cleanup. "
            "This cycle still owns the environment; inspect the linked run."
        ),
        "completed": (
            "The engine confirmed completion and cleanup. Inspect the linked result for "
            "test acceptance."
        ),
        "failed": (
            "The linked execution failed. Inspect its recorded reason before preparing a "
            "new execution."
        ),
        "aborted": "The linked execution was confirmed aborted.",
    }[row.status]
    if row.status == "aborted" and row.thread_id is None:
        detail = "Launch canceled before submission. No execution was started by this operation."
    return CycleDispatchRead.model_validate(
        {
            "id": row.id,
            "preparation_id": row.preparation_id,
            "request_key": row.request_key,
            "status": row.status,
            "created_by": row.created_by,
            "canceled_by": row.canceled_by,
            "created_at": row.created_at,
            "updated_at": row.updated_at,
            "thread_id": row.thread_id,
            "run_id": row.run_id,
            "engine_run_id": engine.id if engine else None,
            "external_run_id": engine.external_run_id if engine else None,
            "detail": detail,
        }
    )


async def sync_dispatch(
    session: Any, row: CycleExecutionDispatch, pipeline: Any = None
) -> EngineRun | None:
    engine = (
        await session.scalar(
            select(EngineRun).where(EngineRun.thread_id == row.thread_id, EngineRun.attempt == 1)
        )
        if row.thread_id
        else None
    )
    if row.status in TERMINAL_DISPATCH:
        return engine
    status = row.status
    if engine is not None:
        # Terminal projection alone is insufficient while cleanup is unconfirmed.
        if engine.status in TERMINAL_DISPATCH and engine.completion_kind in {
            "collection_teardown_v1",
            "cleanup_teardown_v1",
        }:
            status = engine.status
        elif (
            engine.status in {"failed", "aborted"}
            and not engine.external_run_id
            and not engine.handle.get("external_run_id")
        ):
            # A definitive source/preflight failure writes a terminal empty
            # reservation without provisioning a provider resource.
            status = engine.status
        elif engine.status == "running":
            status = "running"
        elif engine.status == "collecting" or engine.status in TERMINAL_DISPATCH:
            status = "cleanup_pending"
        elif engine.status in {"ready", "provisioning"}:
            status = "submitted"
    elif row.thread_id and pipeline is not None:
        try:
            view = await pipeline.get_pipeline(row.thread_id)
            values = view.get("values", {})
            entry = values.get("phase_results", {}).get("execution", {})
            # A checkpointed terminal execution with no durable provider
            # reservation proves failure happened before external effects.
            if view.get("thread_status") in {"error", "idle"} and entry.get("status") in {
                "failed",
                "aborted",
            }:
                status = entry["status"]
            elif view.get("thread_status") == "error":
                status = "failed"
        except Exception:
            pass  # Missing/unreadable is uncertainty, never proof that load stopped.
    if status != row.status:
        values: dict[str, Any] = {"status": status, "updated_at": datetime.now(UTC)}
        if status in TERMINAL_DISPATCH:
            values["active_environment_id"] = None
        await session.execute(
            update(CycleExecutionDispatch)
            .where(CycleExecutionDispatch.id == row.id, CycleExecutionDispatch.status == row.status)
            .values(**values)
            .execution_options(synchronize_session=False)
        )
        await session.commit()
        await session.refresh(row)
    return engine


async def _current_actor(session: Any, consumer_id: str) -> ConsumerIdentity:
    row = await session.scalar(select(ApiConsumer).where(ApiConsumer.id == consumer_id))
    if row is None or not row.enabled or row.deleted_at is not None:
        raise ValueError("The dispatch author no longer has an active consumer identity")
    scopes = (
        await session.scalars(select(ConsumerScope).where(ConsumerScope.consumer_id == consumer_id))
    ).all()
    actor = ConsumerIdentity(
        consumer_id=row.id,
        name=row.name,
        consumer_type=ConsumerType(row.consumer_type),
        role=Role(row.role),
        scopes=[ScopeRef(project_id=s.project_id, app_id=s.app_id) for s in scopes],
    )
    if not actor.role.at_least(Role.OPERATOR):
        raise ValueError("The dispatch author no longer has execution permission")
    return actor


async def load_dispatch_binding(
    cfg: PipelineConfigurable, thread_id: str, attempt: int, *, read_source: bool = False
) -> tuple[LoadTestSpec, str | None]:
    """Revalidate the durable authority immediately before external side effects."""
    if not cfg.cycle_dispatch_id or attempt != 1:
        raise ValueError(
            "A cycle dispatch permits exactly one execution attempt; prepare a new "
            "reviewed binding to rerun"
        )
    async with get_sessionmaker()() as session:
        row = await session.scalar(
            select(CycleExecutionDispatch).where(CycleExecutionDispatch.id == cfg.cycle_dispatch_id)
        )
        if row is None or row.thread_id != thread_id or row.active_environment_id is None:
            raise ValueError("Cycle dispatch does not own this pipeline or active environment")
        actor = await _current_actor(session, row.created_by)
        cycle, snapshot, artifact, target, checks = await dispatch_context(
            session, actor, row.cycle_id, row.preparation_id
        )
        if any(c.status == "blocked" for c in checks):
            raise ValueError(
                "Cycle dispatch prerequisites changed; review the preparation's current checks"
            )
        if (
            cfg.project_id != cycle.project_id
            or cfg.app_id != cycle.app_id
            or cfg.environment_id != snapshot.environment_id
            or cfg.engine != "apex_load"
            or cfg.connections.get("execution_engine") != snapshot.connection_id
            or cfg.environment_target_version != snapshot.environment_version
            or target is None
            or cfg.environment_target != target.base_url
            or cfg.selected_phases() != ["execution"]
            or cfg.load_test
            or artifact is None
            or cfg.connections.get("artifact_store") != artifact.connection_id
        ):
            raise ValueError("Pipeline configuration no longer matches its exact cycle binding")
        from apex.graphs.pipeline.execution_phase import execution_idempotency_key

        spec = binding_spec(
            snapshot, target.base_url, execution_idempotency_key(thread_id, attempt)
        )
        if not read_source:
            return spec, None
        if artifact is None:
            raise ValueError("Approved source artifact is missing")
        from apex.routers.cycle_scripts import resolve_store

        store = await resolve_store(get_connection_resolver(), artifact, cycle.project_id)
        try:
            data = await read_verified_script(
                store, artifact.artifact_key, snapshot.script_source_checksum
            )
            return spec, data.decode("utf-8")
        finally:
            await close_adapter(store)


def binding_spec(snapshot: ExecutionSnapshot, target: str, request_key: str) -> LoadTestSpec:
    return LoadTestSpec.model_validate(
        {
            "title": snapshot.plan_title,
            **snapshot.workload,
            "script_refs": [],
            "target_environment": target,
            "idempotency_key": request_key,
        }
    )


async def verified_dispatch_runs(session: Any, cycle: Any, runs: list[EngineRun]) -> set[str]:
    """Verify immutable launch receipts, never infer source provenance from thread membership."""
    from apex.adapters.apex_load.engine import approved_workload_digest
    from apex.graphs.pipeline.execution_phase import execution_idempotency_key

    threads = {run.thread_id for run in runs if run.attempt == 1}
    if not threads:
        return set()
    rows = (
        await session.execute(
            select(CycleExecutionDispatch, CycleExecutionPreparation)
            .join(
                CycleExecutionPreparation,
                CycleExecutionPreparation.id == CycleExecutionDispatch.preparation_id,
            )
            .where(
                CycleExecutionDispatch.cycle_id == cycle.id,
                CycleExecutionDispatch.thread_id.in_(threads),
            )
        )
    ).all()
    by_thread = {dispatch.thread_id: (dispatch, preparation) for dispatch, preparation in rows}
    verified = set()
    for run in runs:
        if run.attempt != 1 or run.thread_id not in by_thread:
            continue
        dispatch, preparation = by_thread[run.thread_id]
        try:
            snapshot = ExecutionSnapshot.model_validate(preparation.snapshot)
            cfg = PipelineConfigurable.model_validate(dispatch.pipeline_config)
            request_key = execution_idempotency_key(run.thread_id, 1)
            spec = binding_spec(snapshot, cfg.environment_target or "", request_key)
            handle = run.handle
            extras = handle.get("extras", {})
            if (
                run.engine == "apex_load"
                and run.project_id == cycle.project_id
                and run.app_id == cycle.app_id
                and run.ownership_known
                and run.scope_ownership_known
                and cfg.cycle_dispatch_id == dispatch.id
                and cfg.project_id == cycle.project_id
                and cfg.app_id == cycle.app_id
                and cfg.environment_id == snapshot.environment_id == cycle.environment_id
                and cfg.environment_target_version == snapshot.environment_version
                and cfg.connections.get("execution_engine")
                == snapshot.connection_id
                == handle.get("connection_id")
                and handle.get("idempotency_key") == request_key
                and run.external_run_id
                and handle.get("external_run_id") == run.external_run_id
                and extras.get("approved_script_id")
                and extras.get("approved_artifact_sha256") == snapshot.script_source_checksum
                and extras.get("approved_workload_digest") == approved_workload_digest(spec)
            ):
                verified.add(run.id)
        except (ValueError, TypeError, AttributeError):
            continue
    return verified

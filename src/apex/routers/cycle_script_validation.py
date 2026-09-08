"""Exact-source provider validation. No provision, upload, schedule or start calls."""

import asyncio
from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import func, or_, select, update

from apex.adapters.registry import PortKind
from apex.app.dependencies import CurrentIdentity
from apex.domain.cycle_executions import ExecutionSnapshot, _approved
from apex.domain.diagnostics import bounded_diagnostic, contains_credential_material
from apex.domain.input_limits import RecordId
from apex.domain.integrations import ValidationReport
from apex.persistence.models import (
    Connection,
    CycleExecutionPreparation,
    CycleScriptArtifact,
    CycleScriptValidation,
)
from apex.routers.cycle_executions import reserve_revision
from apex.routers.cycle_scripts import Resolver, resolve_store
from apex.routers.cycles import Operator, Session, enabled, find
from apex.services.connections import close_adapter
from apex.services.cycle_scripts import read_verified_script

router = APIRouter(prefix="/cycles", tags=["cycles"], dependencies=[Depends(enabled)])


class ValidateCycleScript(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    expected_revision: int = Field(ge=1)
    idempotency_key: RecordId

    @model_validator(mode="after")
    def safe(self) -> "ValidateCycleScript":
        if contains_credential_material(self.model_dump()):
            raise ValueError("Validation metadata must not contain credentials")
        return self


class ScriptValidationRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    preparation_id: str
    request_key: str
    status: str
    issues: list[str]
    warnings: list[str]
    created_by: str
    created_at: datetime
    finished_at: datetime | None
    current: bool = False


class ScriptValidationWorkspace(BaseModel):
    revision: int
    blockers: list[str]
    validations: list[ScriptValidationRead]
    validation_scope: str = (
        "Native script source only; no workload, capacity or target certification"
    )


async def validation_context(cycle_id, preparation_id, session, identity):
    cycle = await find(session, identity, cycle_id)
    preparation = await session.scalar(
        select(CycleExecutionPreparation).where(
            CycleExecutionPreparation.id == preparation_id,
            CycleExecutionPreparation.cycle_id == cycle.id,
        )
    )
    if preparation is None:
        raise HTTPException(404, "Execution preparation not found")
    snapshot = ExecutionSnapshot.model_validate(preparation.snapshot)
    blockers: list[str] = []
    if snapshot.mode != "live":
        blockers.append(
            "Simulation preparations do not contact live providers. Prepare a live "
            "cycle for provider validation."
        )
    if snapshot.engine != "apex-load" or snapshot.script_format != "apex_native_json":
        blockers.append(
            "This adapter cannot validate imported source. LoadRunner requires a "
            "provider bundle/import validation workflow; local test-ID checks are "
            "not source validation."
        )
    try:
        for kind, version_id, checksum in (
            ("script", snapshot.script_version_id, snapshot.script_checksum),
            ("test_plan", snapshot.plan_version_id, snapshot.plan_checksum),
        ):
            version = _approved(cycle.versions, version_id, kind)
            if version["checksum"] != checksum:
                raise ValueError("Changed version")
    except ValueError:
        blockers.append(
            "The approved source or plan is no longer current. Prepare a new "
            "execution after review."
        )
    artifact = await session.scalar(
        select(CycleScriptArtifact).where(
            CycleScriptArtifact.id == snapshot.script_artifact_id,
            CycleScriptArtifact.cycle_id == cycle.id,
            CycleScriptArtifact.version_id == snapshot.script_version_id,
            CycleScriptArtifact.version_checksum == snapshot.script_checksum,
            CycleScriptArtifact.source_checksum == snapshot.script_source_checksum,
            CycleScriptArtifact.stored_at.is_not(None),
        )
    )
    if artifact is not None:
        storage_current = await session.scalar(
            select(Connection.id).where(
                Connection.id == artifact.connection_id,
                Connection.runtime_version == artifact.connection_version,
                Connection.kind == "artifact_store",
                Connection.provider == "s3",
                Connection.enabled.is_(True),
                or_(Connection.project_id == cycle.project_id, Connection.project_id.is_(None)),
            )
        )
        if storage_current is None:
            blockers.append(
                "The recorded artifact connection is unavailable or changed. Restore "
                "access to that generation before validating."
            )
    if artifact is None:
        blockers.append("Store the approved source and prepare a new execution that references it.")
    connection = await session.scalar(
        select(Connection).where(
            Connection.id == snapshot.connection_id,
            Connection.kind == "execution_engine",
            Connection.provider == "apex_load",
            Connection.enabled.is_(True),
            or_(Connection.project_id == cycle.project_id, Connection.project_id.is_(None)),
        )
    )
    if connection is None or connection.runtime_version.isoformat() != snapshot.connection_version:
        blockers.append(
            "The selected execution connection is unavailable or changed. Prepare a new execution."
        )
    return cycle, snapshot, artifact, blockers


def validation_view(row: CycleScriptValidation, blockers: list[str]) -> ScriptValidationRead:
    fresh = row.finished_at is not None and timedelta(0) <= datetime.now(
        UTC
    ) - row.finished_at <= timedelta(hours=24)
    return ScriptValidationRead.model_validate(row).model_copy(
        update={"current": row.status == "accepted" and fresh and not blockers}
    )


async def latest_validation_id(session, preparation_id):
    return await session.scalar(
        select(CycleScriptValidation.id)
        .where(CycleScriptValidation.preparation_id == preparation_id)
        .order_by(CycleScriptValidation.created_at.desc(), CycleScriptValidation.id)
        .limit(1)
    )


@router.get(
    "/{cycle_id}/executions/{preparation_id}/script-validations",
    response_model=ScriptValidationWorkspace,
    operation_id="getCycleScriptValidations",
)
async def validation_history(
    cycle_id: RecordId, preparation_id: RecordId, session: Session, identity: CurrentIdentity
):
    cycle, _, _, blockers = await validation_context(cycle_id, preparation_id, session, identity)
    rows = (
        await session.scalars(
            select(CycleScriptValidation)
            .where(
                CycleScriptValidation.cycle_id == cycle.id,
                CycleScriptValidation.preparation_id == preparation_id,
            )
            .order_by(CycleScriptValidation.created_at.desc(), CycleScriptValidation.id)
        )
    ).all()
    return ScriptValidationWorkspace(
        revision=cycle.revision,
        blockers=blockers,
        validations=[
            validation_view(row, blockers if index == 0 else ["Superseded validation attempt"])
            for index, row in enumerate(rows)
        ],
    )


@router.post(
    "/{cycle_id}/executions/{preparation_id}/script-validations",
    response_model=ScriptValidationRead,
    operation_id="validateCycleScript",
)
async def validate_script(
    cycle_id: RecordId,
    preparation_id: RecordId,
    body: ValidateCycleScript,
    session: Session,
    identity: Operator,
    resolver: Resolver,
):
    cycle, snapshot, artifact, blockers = await validation_context(
        cycle_id, preparation_id, session, identity
    )
    existing = await session.scalar(
        select(CycleScriptValidation).where(
            CycleScriptValidation.cycle_id == cycle.id,
            CycleScriptValidation.request_key == body.idempotency_key,
        )
    )
    if existing is not None:
        if existing.preparation_id != preparation_id:
            raise HTTPException(409, "Idempotency key belongs to a different preparation")
        if existing.status != "pending":
            latest_id = await latest_validation_id(session, preparation_id)
            return validation_view(
                existing,
                blockers + (["Superseded validation attempt"] if latest_id != existing.id else []),
            )
    if blockers or artifact is None:
        raise HTTPException(422, blockers or ["Stored source is unavailable"])
    project_id = cycle.project_id
    if existing is None:
        count = await session.scalar(
            select(func.count())
            .select_from(CycleScriptValidation)
            .where(CycleScriptValidation.cycle_id == cycle.id)
        )
        if count is not None and count >= 200:
            raise HTTPException(409, "Cycle validation history limit reached")
        await reserve_revision(session, cycle, body.expected_revision)
        record = CycleScriptValidation(
            cycle_id=cycle.id,
            preparation_id=preparation_id,
            request_key=body.idempotency_key,
            status="pending",
            issues=[],
            warnings=[],
            created_by=identity.consumer_id,
        )
        session.add(record)
        # Preserve the attempt even if provider IO or the response is interrupted.
        await session.commit()
        await session.refresh(record)
    else:
        record = existing
    session.expunge(record)
    session.expunge(artifact)
    await session.rollback()
    engine = None
    store = None
    status = "unavailable"
    report = ValidationReport(
        ok=False,
        issues=[
            "Provider validation could not be completed. Check storage and "
            "provider access, then retry with a new operation key."
        ],
    )
    try:
        async with asyncio.timeout(30):
            metadata = await resolver.resolve_metadata(
                PortKind.EXECUTION_ENGINE,
                connection_id=snapshot.connection_id,
                project_id=project_id,
                expected_provider="apex_load",
            )
            if (
                not metadata.persisted
                or metadata.config.id != snapshot.connection_id
                or metadata.connection_version is None
                or metadata.connection_version.isoformat() != snapshot.connection_version
                or metadata.internal_project_id not in (None, project_id)
            ):
                raise ValueError("Execution connection generation changed")
            store = await resolve_store(resolver, artifact, project_id)
            source = await read_verified_script(
                store, artifact.artifact_key, snapshot.script_source_checksum
            )
            engine = (await resolver.build_from_metadata(metadata)).adapter
            if not callable(getattr(engine, "validate_script_source", None)):
                raise ValueError("Adapter does not implement source validation")
            result = await engine.validate_script_source(source.decode("utf-8"))
            # This port requires an explicit Boolean outcome, never a default pass.
            if type(result) is not ValidationReport or "ok" not in result.model_fields_set:
                raise ValueError("Provider omitted validation outcome")
            report = ValidationReport.model_validate(result.model_dump(), strict=True)
            status = "accepted" if report.ok is True and not report.issues else "rejected"
    except Exception:
        status = "unavailable"
    finally:
        for adapter in (engine, store):
            if adapter is not None:
                try:
                    await close_adapter(adapter)
                except Exception:
                    status = "unavailable"
    if status == "unavailable":
        report = ValidationReport(
            ok=False,
            issues=[
                "Provider validation could not be completed. Check storage and "
                "provider access, then retry with a new operation key."
            ],
        )
    await session.execute(
        update(CycleScriptValidation)
        .where(CycleScriptValidation.id == record.id, CycleScriptValidation.status == "pending")
        .values(
            status=status,
            issues=[bounded_diagnostic(i, max_chars=2048) for i in report.issues],
            warnings=[bounded_diagnostic(w, max_chars=2048) for w in report.warnings],
            finished_at=datetime.now(UTC),
        )
    )
    await session.commit()
    _, _, _, current_blockers = await validation_context(
        cycle_id, preparation_id, session, identity
    )
    result_row = await session.scalar(
        select(CycleScriptValidation).where(CycleScriptValidation.id == record.id)
    )
    if result_row is None:
        raise HTTPException(503, "Validation record is unavailable; inspect operation history")
    latest_id = await latest_validation_id(session, preparation_id)
    return validation_view(
        result_row,
        current_blockers
        + (["Superseded validation attempt"] if latest_id != result_row.id else []),
    )

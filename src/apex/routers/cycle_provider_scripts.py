"""Read provider exports into versioned drafts; never create provider resources."""

import asyncio
import json
from datetime import datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict
from sqlalchemy import or_, select, update

from apex.adapters.registry import PortKind
from apex.app.dependencies import CurrentIdentity
from apex.domain.cycle_executions import _approved
from apex.domain.cycles import VersionCreate, add_version, cycle_progress, version_digest
from apex.domain.input_limits import NoNulStr, RecordId
from apex.domain.provider_scripts import (
    ImportProviderScript,
    ProviderScriptExport,
    ProviderScriptSummary,
)
from apex.persistence.models import Connection, CycleProviderScriptImport, TestCycle
from apex.routers.cycle_executions import ConnectionChoice
from apex.routers.cycle_scripts import Resolver
from apex.routers.cycles import Operator, Session, enabled, find
from apex.services.connections import close_adapter

router = APIRouter(prefix="/cycles", tags=["cycles"], dependencies=[Depends(enabled)])


class ProviderImportRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    version_id: str
    connection_id: str
    connection_version: datetime
    remote_script_id: str
    remote_name: str
    remote_project_id: str
    remote_version: str
    source_checksum: str
    imported_by: str
    imported_at: datetime
    content_scope: Literal["provider_export"] = "provider_export"
    executable_content_verified: Literal[False] = False


class ProviderScriptsRead(BaseModel):
    revision: int
    connections: list[ConnectionChoice]
    scripts: list[ProviderScriptSummary]
    preview: ProviderScriptExport | None = None
    connection_version: datetime | None = None
    total: int = 0
    imports: list[ProviderImportRead]
    blockers: list[str]


async def choices(session, cycle):
    rows = (
        (
            await session.execute(
                select(
                    Connection.id, Connection.name, Connection.provider, Connection.runtime_version
                )
                .where(
                    Connection.kind == "execution_engine",
                    Connection.provider == "apex_load",
                    Connection.enabled.is_(True),
                    or_(Connection.project_id == cycle.project_id, Connection.project_id.is_(None)),
                )
                .order_by(Connection.name, Connection.id)
            )
        )
        .mappings()
        .all()
    )
    return [ConnectionChoice.model_validate(row) for row in rows]


async def read_provider(resolver, project_id, connection_id, generation, remote_id):
    adapter = None
    result = None
    try:
        async with asyncio.timeout(30):
            metadata = await resolver.resolve_metadata(
                PortKind.EXECUTION_ENGINE,
                connection_id=connection_id,
                project_id=project_id,
                expected_provider="apex_load",
            )
            if (
                not metadata.persisted
                or metadata.config.id != connection_id
                or metadata.connection_version != generation
                or metadata.internal_project_id not in (None, project_id)
            ):
                raise ValueError("Provider connection generation changed")
            adapter = (await resolver.build_from_metadata(metadata)).adapter
            if remote_id is None:
                values = await adapter.list_script_exports()
                if type(values) is not list or len(values) > 5000:
                    raise ValueError("Provider library is unbounded")
                result = [ProviderScriptSummary.model_validate(v.model_dump()) for v in values]
            else:
                value = await adapter.read_script_export(remote_id)
                result = ProviderScriptExport.model_validate(value.model_dump())
    except Exception:
        result = None
    finally:
        if adapter is not None:
            try:
                await close_adapter(adapter)
            except Exception:
                result = None
    if result is None:
        raise HTTPException(
            503,
            "Provider export could not be read safely. Check connection access, project, "
            "source size and redacted data; then refresh.",
        )
    return result


@router.get(
    "/{cycle_id}/provider-scripts",
    response_model=ProviderScriptsRead,
    operation_id="getCycleProviderScripts",
)
async def browse(
    cycle_id: RecordId,
    session: Session,
    identity: CurrentIdentity,
    resolver: Resolver,
    connection_id: RecordId | None = None,
    remote_script_id: Annotated[NoNulStr | None, Query(min_length=1, max_length=255)] = None,
    offset: int = Query(0, ge=0, le=5000),
    limit: int = Query(20, ge=1, le=100),
):
    cycle = await find(session, identity, cycle_id)
    connections = await choices(session, cycle)
    imports = (
        await session.scalars(
            select(CycleProviderScriptImport)
            .where(CycleProviderScriptImport.cycle_id == cycle.id)
            .order_by(CycleProviderScriptImport.imported_at.desc())
        )
    ).all()
    result = ProviderScriptsRead(
        revision=cycle.revision,
        connections=connections,
        scripts=[],
        imports=[ProviderImportRead.model_validate(row) for row in imports],
        blockers=[],
    )
    if cycle.mode != "live":
        result.connections = []
        result.blockers = [
            "Use a live cycle to read a provider library. "
            "Simulation does not contact live providers."
        ]
        return result
    if not connection_id:
        return result
    connection = next((c for c in connections if c.id == connection_id), None)
    if connection is None:
        raise HTTPException(422, "Select an enabled APEX Load connection for this project")
    project_id = cycle.project_id
    await session.rollback()
    payload = await read_provider(
        resolver, project_id, connection.id, connection.runtime_version, remote_script_id
    )
    result.connection_version = connection.runtime_version
    if isinstance(payload, ProviderScriptExport):
        result.preview = payload
    else:
        result.total = len(payload)
        result.scripts = payload[offset : offset + limit]
    return result


@router.post(
    "/{cycle_id}/provider-scripts/import",
    response_model=ProviderImportRead,
    operation_id="importCycleProviderScript",
)
async def import_script(
    cycle_id: RecordId,
    body: ImportProviderScript,
    session: Session,
    identity: Operator,
    resolver: Resolver,
):
    cycle = await find(session, identity, cycle_id)
    fingerprint = version_digest(
        body.model_dump(mode="json", exclude={"expected_revision", "idempotency_key"})
    )
    existing = await session.scalar(
        select(CycleProviderScriptImport).where(
            CycleProviderScriptImport.cycle_id == cycle.id,
            CycleProviderScriptImport.request_key == body.idempotency_key,
        )
    )
    if existing:
        if existing.request_fingerprint != fingerprint:
            raise HTTPException(409, "Idempotency key belongs to a different import")
        return ProviderImportRead.model_validate(existing)
    if cycle.mode != "live":
        raise HTTPException(422, "Simulation cycles do not import from live providers")
    if cycle.revision != body.expected_revision:
        raise HTTPException(409, "Cycle changed; refresh before importing")
    connection = next(
        (
            c
            for c in await choices(session, cycle)
            if c.id == body.connection_id and c.runtime_version == body.connection_version
        ),
        None,
    )
    if connection is None:
        raise HTTPException(409, "Connection changed; refresh the provider preview")
    project_id = cycle.project_id
    # Validate dependencies before network IO; validate again before the atomic write.
    try:
        _approved(cycle.versions, body.plan_version_id, "test_plan")
    except ValueError:
        raise HTTPException(422, "Select a current approved test plan") from None
    await session.rollback()
    exported = await read_provider(
        resolver, project_id, body.connection_id, body.connection_version, body.remote_script_id
    )
    if (
        not isinstance(exported, ProviderScriptExport)
        or exported.source_checksum != body.expected_source_checksum
    ):
        raise HTTPException(
            409, "Provider source changed; inspect a fresh preview before importing"
        )
    cycle = await find(session, identity, cycle_id)
    if cycle.revision != body.expected_revision or len(cycle.versions) >= 200:
        raise HTTPException(
            409, "Cycle changed or reached its version limit; refresh before importing"
        )
    try:
        version = add_version(
            cycle.versions,
            VersionCreate(
                expected_revision=body.expected_revision,
                kind="script",
                title=body.title,
                dependencies=[body.plan_version_id],
                content={
                    "engine": "apex-load",
                    "format": "apex_native_json",
                    "source": exported.source,
                    "parameters": body.parameters,
                    "coverage": body.coverage,
                    "validation": (
                        "Provider export captured; executable content and "
                        "provider validation remain unverified."
                    ),
                },
                change_summary=(
                    f"Imported provider export {exported.name} ({exported.id}) as a new draft."
                ),
            ),
            identity.consumer_id,
        )
    except ValueError:
        raise HTTPException(
            422,
            "Export cannot become a bounded, credential-free draft with the selected dependencies",
        ) from None
    versions = [*cycle.versions, version]
    if len(json.dumps(versions).encode()) > 2_000_000:
        raise HTTPException(413, "Cycle content limit reached")
    changed = await session.scalar(
        update(TestCycle)
        .where(TestCycle.id == cycle.id, TestCycle.revision == body.expected_revision)
        .values(
            versions=versions,
            progress=cycle_progress(versions).model_dump(),
            revision=body.expected_revision + 1,
        )
        .returning(TestCycle.id)
        .execution_options(synchronize_session=False)
    )
    if changed is None:
        await session.rollback()
        raise HTTPException(409, "Cycle changed; refresh before importing")
    record = CycleProviderScriptImport(
        cycle_id=cycle.id,
        version_id=version["id"],
        request_key=body.idempotency_key,
        request_fingerprint=fingerprint,
        connection_id=body.connection_id,
        connection_version=body.connection_version,
        remote_script_id=exported.id,
        remote_name=exported.name,
        remote_project_id=exported.project_id,
        remote_version=exported.version,
        source_checksum=exported.source_checksum,
        imported_by=identity.consumer_id,
    )
    session.add(record)
    await session.commit()
    await session.refresh(record)
    return ProviderImportRead.model_validate(record)

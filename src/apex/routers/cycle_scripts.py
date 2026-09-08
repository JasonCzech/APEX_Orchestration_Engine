"""Artifact-backed script versions with scoped reads and explicit storage affinity."""

import asyncio
import hashlib
from datetime import UTC, datetime
from typing import Annotated
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import or_, select, update

from apex.adapters.registry import PortKind
from apex.app.dependencies import CurrentIdentity
from apex.domain.cycle_executions import _approved
from apex.domain.input_limits import RecordId
from apex.persistence.models import Connection, CycleScriptArtifact
from apex.routers.cycle_executions import ConnectionChoice, reserve_revision
from apex.routers.cycles import Operator, Session, enabled, find
from apex.services.connections import ConnectionResolver, close_adapter, get_connection_resolver
from apex.services.cycle_scripts import read_verified_script, script_bytes, store_verified_script

router = APIRouter(prefix="/cycles", tags=["cycles"], dependencies=[Depends(enabled)])
Resolver = Annotated[ConnectionResolver, Depends(get_connection_resolver)]


class StoreCycleScript(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_revision: int = Field(ge=1)
    connection_id: RecordId


class ScriptArtifactRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    version_id: str
    version_checksum: str
    source_checksum: str
    size_bytes: int
    connection_id: str
    connection_version: datetime
    stored_at: datetime | None
    created_at: datetime
    created_by: str


class ScriptLibraryRead(BaseModel):
    artifacts: list[ScriptArtifactRead]
    connections: list[ConnectionChoice]
    revision: int


@router.get("/{cycle_id}/scripts", response_model=ScriptLibraryRead, operation_id="getCycleScripts")
async def library(cycle_id: RecordId, session: Session, identity: CurrentIdentity):
    cycle = await find(session, identity, cycle_id)
    rows = (
        await session.scalars(
            select(CycleScriptArtifact)
            .where(CycleScriptArtifact.cycle_id == cycle.id)
            .order_by(CycleScriptArtifact.created_at.desc())
        )
    ).all()
    connections = (
        (
            await session.execute(
                select(
                    Connection.id, Connection.name, Connection.provider, Connection.runtime_version
                )
                .where(
                    Connection.kind == "artifact_store",
                    Connection.enabled.is_(True),
                    Connection.provider == "s3",
                    or_(Connection.project_id == cycle.project_id, Connection.project_id.is_(None)),
                )
                .order_by(Connection.name, Connection.id)
            )
        )
        .mappings()
        .all()
    )
    return ScriptLibraryRead(
        artifacts=[ScriptArtifactRead.model_validate(r) for r in rows],
        connections=[ConnectionChoice.model_validate(c) for c in connections],
        revision=cycle.revision,
    )


async def resolve_store(resolver: ConnectionResolver, record: CycleScriptArtifact, project_id: str):
    metadata = await resolver.resolve_metadata(
        PortKind.ARTIFACT_STORE, connection_id=record.connection_id, project_id=project_id
    )
    if (
        not metadata.persisted
        or metadata.config.id != record.connection_id
        or metadata.connection_version != record.connection_version
        or metadata.config.provider != "s3"
        or metadata.internal_project_id not in (None, project_id)
    ):
        raise ValueError("The recorded persistent artifact connection is no longer available")
    return (await resolver.build_from_metadata(metadata)).adapter


@router.post(
    "/{cycle_id}/scripts/{version_id}/artifact",
    response_model=ScriptArtifactRead,
    operation_id="storeCycleScript",
)
async def store_script(
    cycle_id: RecordId,
    version_id: RecordId,
    body: StoreCycleScript,
    session: Session,
    identity: Operator,
    resolver: Resolver,
):
    cycle = await find(session, identity, cycle_id)
    try:
        version = _approved(cycle.versions, version_id, "script")
        if version["content"]["format"] not in {"apex_native_json", "web_http_html"}:
            raise ValueError(
                "Store native JSON or Web HTTP/HTML source; "
                "external import references must first be materialized"
            )
        data = script_bytes(version["content"]["source"])
    except ValueError as error:
        raise HTTPException(422, str(error)) from None
    source_checksum = hashlib.sha256(data).hexdigest()
    project_id = cycle.project_id
    record = await session.scalar(
        select(CycleScriptArtifact).where(
            CycleScriptArtifact.cycle_id == cycle.id, CycleScriptArtifact.version_id == version_id
        )
    )
    if record is not None:
        if (
            record.connection_id != body.connection_id
            or record.source_checksum != source_checksum
            or record.version_checksum != version["checksum"]
        ):
            raise HTTPException(409, "This script version already has a different storage binding")
    else:
        connection = await session.scalar(
            select(Connection).where(
                Connection.id == body.connection_id,
                Connection.kind == "artifact_store",
                Connection.enabled.is_(True),
                Connection.provider == "s3",
                or_(Connection.project_id == project_id, Connection.project_id.is_(None)),
            )
        )
        if connection is None:
            raise HTTPException(422, "Select an enabled persistent artifact store for this project")
        await reserve_revision(session, cycle, body.expected_revision)
        artifact_id = uuid4().hex
        record = CycleScriptArtifact(
            id=artifact_id,
            cycle_id=cycle.id,
            version_id=version_id,
            version_checksum=version["checksum"],
            source_checksum=source_checksum,
            size_bytes=len(data),
            artifact_key=f"cycle-scripts/{artifact_id}/{source_checksum}",
            connection_id=connection.id,
            connection_version=connection.runtime_version,
            created_by=identity.consumer_id,
        )
        session.add(record)
        # Commit intent before object IO. Retries reuse the key and exact bytes.
        await session.commit()
        await session.refresh(record)
    # Detach before releasing the read transaction; no DB connection during network IO.
    session.expunge(record)
    await session.rollback()
    store = None
    failed = False
    try:
        async with asyncio.timeout(30):
            store = await resolve_store(resolver, record, project_id)
            if record.stored_at is None:
                await store_verified_script(store, record.artifact_key, data)
            else:
                # Replays verify existing bytes; never overwrite a verified object.
                await read_verified_script(store, record.artifact_key, record.source_checksum)
    except Exception:
        failed = True
    finally:
        if store is not None:
            try:
                await close_adapter(store)
            except Exception:
                failed = True
    if failed:
        raise HTTPException(
            503,
            "Script storage could not be verified. Check the recorded artifact connection "
            "and retry this version; no test was launched.",
        )
    await session.execute(
        update(CycleScriptArtifact)
        .where(CycleScriptArtifact.id == record.id, CycleScriptArtifact.stored_at.is_(None))
        .values(stored_at=datetime.now(UTC))
    )
    await session.commit()
    result = await session.scalar(
        select(CycleScriptArtifact).where(CycleScriptArtifact.id == record.id)
    )
    return ScriptArtifactRead.model_validate(result)


@router.get(
    "/{cycle_id}/scripts/{version_id}/content",
    operation_id="downloadCycleScript",
    response_class=Response,
)
async def download_script(
    cycle_id: RecordId,
    version_id: RecordId,
    session: Session,
    identity: CurrentIdentity,
    resolver: Resolver,
):
    cycle = await find(session, identity, cycle_id)
    project_id = cycle.project_id
    record = await session.scalar(
        select(CycleScriptArtifact).where(
            CycleScriptArtifact.cycle_id == cycle.id,
            CycleScriptArtifact.version_id == version_id,
            CycleScriptArtifact.stored_at.is_not(None),
        )
    )
    if record is None:
        raise HTTPException(404, "Stored script not found")
    session.expunge(record)
    await session.rollback()
    store = None
    data = None
    try:
        async with asyncio.timeout(30):
            store = await resolve_store(resolver, record, project_id)
            data = await read_verified_script(store, record.artifact_key, record.source_checksum)
    except Exception:
        pass
    finally:
        if store is not None:
            try:
                await close_adapter(store)
            except Exception:
                data = None
    if data is None:
        raise HTTPException(
            503,
            "Stored script could not be verified; "
            "check artifact availability and connection generation",
        )
    return Response(
        data,
        media_type="application/octet-stream",
        headers={
            "Content-Disposition": 'attachment; filename="script-source.txt"',
            "Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff",
        },
    )

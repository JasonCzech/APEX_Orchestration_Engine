"""Scoped Confluence source previews and immutable scope-version capture."""

import asyncio
import re
from datetime import UTC, datetime
from typing import Annotated
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import or_, select

from apex.adapters.registry import PortKind
from apex.domain.cycle_sources import CaptureSource
from apex.domain.cycles import VersionCreate, add_version, version_digest
from apex.domain.input_limits import NoNulStr, RecordId
from apex.domain.integrations import DocHit, DocRef, DocScope, DocumentContent
from apex.persistence.models import Connection
from apex.routers.cycle_executions import ConnectionChoice
from apex.routers.cycle_scripts import Resolver
from apex.routers.cycles import (
    CurrentIdentity,
    CycleRead,
    Operator,
    Session,
    enabled,
    find,
    save_versions,
    view,
)
from apex.services.connections import close_adapter

router = APIRouter(prefix="/cycles", tags=["cycles"], dependencies=[Depends(enabled)])


class SourcePreview(BaseModel):
    revision: int
    connections: list[ConnectionChoice]
    connection_version: datetime | None = None
    hits: list[DocHit] = []
    document: DocumentContent | None = None
    checksum: str | None = None
    blockers: list[str] = []


async def source_connections(session: Session, project_id: str):
    rows = (
        (
            await session.execute(
                select(
                    Connection.id, Connection.name, Connection.provider, Connection.runtime_version
                )
                .where(
                    Connection.kind == "documents",
                    Connection.provider == "confluence",
                    Connection.enabled.is_(True),
                    or_(Connection.project_id == project_id, Connection.project_id.is_(None)),
                )
                .order_by(Connection.name, Connection.id)
            )
        )
        .mappings()
        .all()
    )
    return [ConnectionChoice.model_validate(row) for row in rows]


async def provider_read(
    resolver: Resolver,
    project_id: str,
    connection: ConnectionChoice,
    ref: DocRef | None,
    query: str,
):
    adapter = None
    try:
        async with asyncio.timeout(30):
            metadata = await resolver.resolve_metadata(
                PortKind.DOCUMENTS,
                project_id=project_id,
                connection_id=connection.id,
                expected_provider="confluence",
            )
            if (
                not metadata.persisted
                or metadata.config.id != connection.id
                or metadata.connection_version != connection.runtime_version
                or metadata.internal_project_id not in (None, project_id)
            ):
                raise ValueError("Connection scope or generation changed")
            adapter = await resolver.build_from_metadata(metadata)
            if ref is not None:
                document = DocumentContent.model_validate(await adapter.fetch(ref))
                if document.ref.source != "confluence" or not document.ref.revision:
                    raise ValueError("Source revision unavailable")
                return document
            hits = await adapter.search(query, scope=DocScope(project_id=project_id), k=20)
            if not isinstance(hits, list) or len(hits) > 20:
                raise ValueError("Invalid source list")
            return [DocHit.model_validate(hit) for hit in hits]
    except Exception:
        raise HTTPException(
            503,
            "Confluence source unavailable; check connection, space permissions, "
            "revision and supported page content",
        ) from None
    finally:
        if adapter is not None:
            await close_adapter(adapter)


@router.get("/{cycle_id}/sources", response_model=SourcePreview, operation_id="getCycleSources")
async def read_sources(
    cycle_id: RecordId,
    session: Session,
    identity: CurrentIdentity,
    resolver: Resolver,
    connection_id: RecordId | None = None,
    title: Annotated[NoNulStr, Query(max_length=500)] = "",
    page_id: Annotated[NoNulStr | None, Query(min_length=1, max_length=20)] = None,
    revision: Annotated[NoNulStr | None, Query(min_length=1, max_length=20)] = None,
):
    cycle = await find(session, identity, cycle_id)
    if any(
        value is not None and re.fullmatch(r"[1-9][0-9]{0,19}", value) is None
        for value in (page_id, revision)
    ):
        raise HTTPException(422, "Page and revision must be positive numeric identifiers")
    connections = await source_connections(session, cycle.project_id)
    result = SourcePreview(revision=cycle.revision, connections=connections)
    if cycle.mode != "live":
        result.connections = []
        result.blockers = ["Simulation does not retrieve live Confluence pages."]
        return result
    if connection_id is None:
        return result
    connection = next((c for c in connections if c.id == connection_id), None)
    if connection is None:
        raise HTTPException(404, "Source connection not found")
    project = cycle.project_id
    await session.rollback()
    ref = DocRef(id=page_id, source="confluence", revision=revision) if page_id else None
    data = await provider_read(resolver, project, connection, ref, title)
    result.connection_version = connection.runtime_version
    if isinstance(data, DocumentContent):
        result.document = data
        result.checksum = version_digest(data.model_dump(mode="json"))
    else:
        result.hits = data
    return result


@router.post(
    "/{cycle_id}/scopes/{version_id}/sources",
    response_model=CycleRead,
    operation_id="captureCycleSource",
)
async def capture_source(
    cycle_id: RecordId,
    version_id: RecordId,
    body: CaptureSource,
    session: Session,
    identity: Operator,
    resolver: Resolver,
):
    cycle = await find(session, identity, cycle_id)
    fingerprint = version_digest(
        {
            **body.model_dump(mode="json", exclude={"expected_revision", "idempotency_key"}),
            "version_id": version_id,
        }
    )
    for version in cycle.versions:
        records = version["content"].get("_source_evidence")
        if not isinstance(records, list):
            continue
        for record in records:
            if not isinstance(record, dict):
                continue
            if (
                record.get("capture_version_id") == version["id"]
                and record.get("request_key") == body.idempotency_key
            ):
                if record["request_fingerprint"] != fingerprint:
                    raise HTTPException(
                        409, "Operation key already used for another source capture"
                    )
                return view(cycle, identity)
    if (
        cycle.mode != "live"
        or cycle.revision != body.expected_revision
        or len(cycle.versions) >= 200
    ):
        raise HTTPException(409, "Use a current live cycle with available version capacity")
    source = next(
        (
            v
            for v in cycle.versions
            if v["id"] == version_id and v["kind"] == "scope" and not v["archived"]
        ),
        None,
    )
    if source is None:
        raise HTTPException(404, "Active scope version not found")
    connection = next(
        (
            c
            for c in await source_connections(session, cycle.project_id)
            if c.id == body.connection_id and c.runtime_version == body.connection_version
        ),
        None,
    )
    if connection is None:
        raise HTTPException(409, "Source connection changed; refresh the preview")
    if body.ref.revision is None:
        raise HTTPException(422, "Preview an exact page revision before capture")
    project = cycle.project_id
    await session.rollback()
    document = await provider_read(resolver, project, connection, body.ref, "")
    if (
        not isinstance(document, DocumentContent)
        or version_digest(document.model_dump(mode="json")) != body.expected_checksum
    ):
        raise HTTPException(409, "Source content changed; inspect a fresh preview")
    cycle = await find(session, identity, cycle_id)
    if cycle.revision != body.expected_revision:
        raise HTTPException(409, "Cycle changed; refresh before capture")
    new_id = uuid4().hex
    record = {
        "capture_version_id": new_id,
        "request_key": body.idempotency_key,
        "request_fingerprint": fingerprint,
        "label": body.title,
        "title": document.title,
        "connection_id": connection.id,
        "connection_version": connection.runtime_version.isoformat(),
        "captured_at": datetime.now(UTC).isoformat(),
        "captured_by": identity.consumer_id,
        "checksum": body.expected_checksum,
        "document": document.model_dump(mode="json"),
    }
    records = [
        r
        for r in (source["content"].get("_source_evidence") or [])
        if not (r["connection_id"] == connection.id and r["document"]["ref"]["id"] == body.ref.id)
    ]
    if len(records) >= 32:
        raise HTTPException(409, "Scope source limit reached")
    try:
        version = add_version(
            cycle.versions,
            VersionCreate(
                expected_revision=body.expected_revision,
                kind="scope",
                title=source["title"],
                content={**source["content"], "_source_evidence": [*records, record]},
                dependencies=source["dependencies"],
                replaces=version_id,
                change_summary="Captured Confluence revision; renew scope and downstream review.",
            ),
            identity.consumer_id,
            trusted_evidence=True,
        )
    except ValueError:
        raise HTTPException(
            409, "Scope is superseded, dependencies are stale, or source exceeds content limits"
        ) from None
    version["id"] = new_id
    return await save_versions(
        session, cycle, body.expected_revision, [*cycle.versions, version], identity
    )

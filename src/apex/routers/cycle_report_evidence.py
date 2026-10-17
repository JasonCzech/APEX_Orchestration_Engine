"""Freeze a reviewed history projection into a new report draft, without publication."""

import re
from datetime import UTC, datetime
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select

from apex.domain.cycles import VersionCreate, add_version, version_digest
from apex.domain.input_limits import RecordId
from apex.domain.report_evidence import FreezeReportEvidence
from apex.persistence.models import CycleRunLink, EngineRun
from apex.routers.cycle_executions import run_scope
from apex.routers.cycles import CycleRead, Operator, Session, enabled, find, save_versions, view
from apex.services.cycle_results import result_view

router = APIRouter(prefix="/cycles", tags=["cycles"], dependencies=[Depends(enabled)])


@router.post(
    "/{cycle_id}/reports/{version_id}/evidence",
    response_model=CycleRead,
    operation_id="freezeCycleReportEvidence",
)
async def freeze_evidence(
    cycle_id: RecordId,
    version_id: RecordId,
    body: FreezeReportEvidence,
    session: Session,
    identity: Operator,
):
    cycle = await find(session, identity, cycle_id)
    fingerprint = version_digest({"version_id": version_id, "results": body.expected_results})
    for version in cycle.versions:
        snapshot = version["content"].get("_execution_evidence")
        if not isinstance(snapshot, dict) or snapshot.get("capture_version_id") != version["id"]:
            continue
        if snapshot.get("request_key") == body.idempotency_key:
            if snapshot.get("request_fingerprint") != fingerprint:
                raise HTTPException(409, "Operation key already used for different report evidence")
            return view(cycle, identity)
    if cycle.revision != body.expected_revision or len(cycle.versions) >= 200:
        raise HTTPException(409, "Cycle changed or version limit reached; refresh before capture")
    source = next(
        (v for v in cycle.versions if v["id"] == version_id and v["kind"] == "report"), None
    )
    if source is None or source["archived"]:
        raise HTTPException(404, "Active report version not found")
    if version_digest(source["content"]) != source["checksum"]:
        raise HTTPException(409, "Report content no longer matches its version checksum")
    rows = (
        await session.scalars(
            select(EngineRun)
            .join(CycleRunLink, CycleRunLink.engine_run_id == EngineRun.id)
            .where(
                CycleRunLink.cycle_id == cycle.id,
                EngineRun.id.in_(body.expected_results),
                *run_scope(cycle),
            )
        )
    ).all()
    results = sorted((result_view(row) for row in rows), key=lambda r: r.id)
    if {r.id: r.evidence_checksum for r in results} != body.expected_results:
        raise HTTPException(
            409, "Execution evidence changed or became inaccessible; inspect fresh results"
        )
    snapshot = {
        "capture_version_id": uuid4().hex,
        "request_key": body.idempotency_key,
        "request_fingerprint": fingerprint,
        "captured_at": datetime.now(UTC).isoformat(),
        "captured_by": identity.consumer_id,
        "source": "engine_run_history",
        "results": [r.model_dump(mode="json") for r in results],
    }
    captured_label = datetime.fromisoformat(snapshot["captured_at"]).strftime(
        "%Y-%m-%d %H:%M:%S UTC"
    )
    title_suffix = f" (evidence captured {captured_label})"
    base_title = re.sub(
        r" \(evidence captured \d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2} UTC\)$",
        "",
        source["title"],
    )
    captured_title = base_title[: 255 - len(title_suffix)] + title_suffix
    try:
        version = add_version(
            cycle.versions,
            VersionCreate(
                expected_revision=body.expected_revision,
                kind="report",
                title=captured_title,
                content={**source["content"], "_execution_evidence": snapshot},
                dependencies=source["dependencies"],
                replaces=version_id,
                change_summary="Captured execution summaries; renewed report review required.",
            ),
            identity.consumer_id,
            trusted_evidence=True,
        )
    except ValueError:
        raise HTTPException(
            409, "Report is superseded, dependencies are stale, or snapshot exceeds limits"
        ) from None
    version["id"] = snapshot["capture_version_id"]
    return await save_versions(
        session, cycle, body.expected_revision, [*cycle.versions, version], identity
    )

"""Unrestricted administrator model pricing and retained change history."""

import base64
import hashlib
import hmac
import json
import secrets
from datetime import UTC, datetime
from decimal import Decimal
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Path, Query
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy import func, select, tuple_
from sqlalchemy.ext.asyncio import AsyncSession

from apex.app.dependencies import CurrentIdentity, SettingsDep
from apex.persistence.db import get_session
from apex.persistence.models import (
    AgentEvent,
    ModelAdminAction,
    ModelCatalogEntry,
    ModelPricingGeneration,
    PricingCorrectionJob,
)
from apex.services.model_pricing import (
    check_write,
    cost_with_rates,
    digest,
    effective_cost,
    intervals,
    locked_model,
    make_generation,
    provider_for,
    utc,
)


def require_pricing_admin(identity: CurrentIdentity) -> None:
    """Authorize before request validation or database-dependent dependencies."""
    if not identity.is_unscoped:
        raise HTTPException(403, "Requires an unrestricted platform administrator")


router = APIRouter(
    prefix="/analytics/models",
    tags=["model-pricing"],
    dependencies=[Depends(require_pricing_admin)],
)
Session = Annotated[AsyncSession, Depends(get_session)]
Rate = Annotated[Decimal, Field(ge=0, max_digits=18, decimal_places=9, allow_inf_nan=False)]
ModelId = Annotated[str, Path(min_length=1, max_length=32, pattern=r"^[^\x00]*$")]


class Rates(BaseModel):
    input: Rate
    output: Rate
    cache_read: Rate
    cache_creation: Rate


class ModelWrite(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    expected_version: int = Field(ge=1)
    reason: str = Field(min_length=1, max_length=1000, pattern=r"^[^\x00]+$")
    idempotency_key: str = Field(min_length=1, max_length=128, pattern=r"^[^\x00]+$")


class PriceWrite(ModelWrite):
    rates: Rates
    effective_from: AwareDatetime | None = None


class CorrectionWrite(ModelWrite):
    rates: Rates
    effective_from: AwareDatetime
    effective_to: AwareDatetime
    preview_token: str | None = Field(default=None, max_length=4096)


class PriceIntervalResponse(BaseModel):
    id: str
    generation_id: str
    effective_from: datetime
    effective_to: datetime | None
    input: str
    output: str
    cache_read: str
    cache_creation: str
    currency: Literal["USD"]
    unit: Literal["million_tokens"]


class ModelPricingResponse(BaseModel):
    id: str
    provider: str
    model_key: str
    display_name: str
    version: int
    availability: Literal["available", "archived", "historical_only"]
    archived_at: datetime | None
    archived_by: str | None
    archive_reason: str | None
    active_pricing_generation_id: str | None
    active_correction_id: str | None
    current_price: PriceIntervalResponse | None
    created_at: datetime
    updated_at: datetime


class ModelPricingListResponse(BaseModel):
    items: list[ModelPricingResponse]
    total: int
    limit: int
    offset: int


class PriceGenerationResponse(BaseModel):
    id: str
    model_id: str
    number: int
    created_by: str | None
    creator_name: str | None
    reason: str
    source: str
    job_id: str | None
    published_at: datetime | None
    created_at: datetime
    intervals: list[PriceIntervalResponse]


class PriceHistoryResponse(BaseModel):
    items: list[PriceGenerationResponse]
    total: int


class CorrectionPreviewResponse(BaseModel):
    preview_token: str
    calculated_at: datetime
    events: int
    total_tokens: int
    previously_unpriced: int
    before_cost_usd: str
    after_cost_usd: str
    delta_cost_usd: str
    expected_version: int
    base_generation_id: str | None


class CorrectionJobResponse(BaseModel):
    id: str
    model_id: str
    generation_id: str
    state: Literal["queued", "running", "succeeded", "failed", "superseded"]
    effective_from: datetime
    effective_to: datetime
    reason: str
    actor_id: str
    processed_count: int
    retry_count: int
    failure_code: str | None
    preview_totals: dict[str, Any]
    final_totals: dict[str, Any]
    created_at: datetime
    updated_at: datetime


def admin(identity: Any) -> None:
    require_pricing_admin(identity)
    from apex.settings import get_settings

    if not get_settings().model_pricing_admin_enabled:
        raise HTTPException(
            503, "Model pricing administration is disabled until deployment rollout completes"
        )


def serial(row: Any) -> dict[str, Any]:
    return {
        column.name: (format(value, "f") if isinstance(value, Decimal) else value)
        for column in row.__table__.columns
        for value in [getattr(row, column.name)]
    }


async def model_response(
    session: AsyncSession, row: ModelCatalogEntry, settings: Any
) -> dict[str, Any]:
    await session.flush()
    await session.refresh(row)
    now = datetime.now(UTC)
    timeline = await intervals(session, row.active_pricing_generation_id)
    current = next(
        (
            i
            for i in timeline
            if utc(i.effective_from) <= now
            and (i.effective_to is None or now < utc(i.effective_to))
        ),
        None,
    )
    active_job = await session.scalar(
        select(PricingCorrectionJob)
        .where(
            PricingCorrectionJob.model_id == row.id,
            PricingCorrectionJob.state.in_(["queued", "running", "failed"]),
        )
        .order_by(PricingCorrectionJob.created_at.desc())
        .limit(1)
    )
    return serial(row) | {
        "active_correction_id": active_job.id if active_job else None,
        "availability": "archived"
        if row.archived_at
        else "available"
        if row.model_key in settings.llm.allowed_models
        and row.provider == provider_for(row.model_key)
        else "historical_only",
        "current_price": serial(current) if current else None,
    }


@router.get("", operation_id="listModelPricing", response_model=ModelPricingListResponse)
async def list_models(
    identity: CurrentIdentity,
    session: Session,
    settings: SettingsDep,
    status: Literal["active", "archived", "all"] = "active",
    q: str = Query("", max_length=255, pattern=r"^[^\x00]*$"),
    provider: str | None = Query(None, max_length=64, pattern=r"^[^\x00]*$"),
    missing_pricing: bool = False,
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0, le=10000),
) -> dict[str, Any]:
    admin(identity)
    filters = []
    if status != "all":
        filters.append(
            ModelCatalogEntry.archived_at.is_(None)
            if status == "active"
            else ModelCatalogEntry.archived_at.is_not(None)
        )
    if status == "active":
        configured = [(provider_for(key), key) for key in settings.llm.allowed_models]
        filters.append(
            tuple_(ModelCatalogEntry.provider, ModelCatalogEntry.model_key).in_(configured)
        )
    if q:
        filters.append(ModelCatalogEntry.model_key.contains(q, autoescape=True))
    if provider:
        filters.append(ModelCatalogEntry.provider == provider)
    if missing_pricing:
        from apex.persistence.models import ModelPriceInterval

        now = datetime.now(UTC)
        present = (
            select(ModelPriceInterval.id)
            .where(
                ModelPriceInterval.generation_id == ModelCatalogEntry.active_pricing_generation_id,
                ModelPriceInterval.effective_from <= now,
                (
                    ModelPriceInterval.effective_to.is_(None)
                    | (ModelPriceInterval.effective_to > now)
                ),
            )
            .exists()
        )
        filters.append(~present)
    total = await session.scalar(
        select(func.count()).select_from(ModelCatalogEntry).where(*filters)
    )
    rows = (
        await session.scalars(
            select(ModelCatalogEntry)
            .where(*filters)
            .order_by(ModelCatalogEntry.model_key, ModelCatalogEntry.id)
            .limit(limit)
            .offset(offset)
        )
    ).all()
    return {
        "items": [await model_response(session, row, settings) for row in rows],
        "total": total,
        "limit": limit,
        "offset": offset,
    }


@router.get("/{model_id}", operation_id="getModelPricing", response_model=ModelPricingResponse)
async def detail(
    model_id: ModelId, identity: CurrentIdentity, session: Session, settings: SettingsDep
) -> dict[str, Any]:
    admin(identity)
    row = await session.get(ModelCatalogEntry, model_id)
    if row is None:
        raise HTTPException(404, "Model not found")
    return await model_response(session, row, settings)


@router.get(
    "/{model_id}/prices", operation_id="getModelPriceHistory", response_model=PriceHistoryResponse
)
async def history(
    model_id: ModelId,
    identity: CurrentIdentity,
    session: Session,
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0, le=10000),
) -> dict[str, Any]:
    admin(identity)
    if await session.get(ModelCatalogEntry, model_id) is None:
        raise HTTPException(404, "Model not found")
    rows = (
        await session.scalars(
            select(ModelPricingGeneration)
            .where(ModelPricingGeneration.model_id == model_id)
            .order_by(ModelPricingGeneration.number.desc())
            .limit(limit)
            .offset(offset)
        )
    ).all()
    total = await session.scalar(
        select(func.count())
        .select_from(ModelPricingGeneration)
        .where(ModelPricingGeneration.model_id == model_id)
    )
    return {
        "items": [
            serial(row) | {"intervals": [serial(i) for i in await intervals(session, row.id)]}
            for row in rows
        ],
        "total": total,
    }


async def receipt(
    session: AsyncSession, model_id: str, body: ModelWrite, action: str
) -> dict[str, Any] | None:
    old = await session.scalar(
        select(ModelAdminAction).where(
            ModelAdminAction.model_id == model_id,
            ModelAdminAction.idempotency_key == body.idempotency_key,
        )
    )
    if old:
        if old.payload_hash != digest(body.model_dump(mode="json")) or old.action != action:
            raise HTTPException(409, "Idempotency key was used for a different request")
        return old.result
    return None


async def record_action(
    session: AsyncSession,
    model_id: str,
    identity: Any,
    body: ModelWrite,
    action: str,
    result: dict[str, Any],
) -> None:
    # JSON-safe retained receipt, committed atomically with the change.
    from fastapi.encoders import jsonable_encoder

    session.add(
        ModelAdminAction(
            model_id=model_id,
            actor_id=identity.consumer_id,
            reason=body.reason,
            action=action,
            idempotency_key=body.idempotency_key,
            payload_hash=digest(body.model_dump(mode="json")),
            result=jsonable_encoder(result),
        )
    )
    await session.commit()


@router.post(
    "/{model_id}/prices", operation_id="setModelPrice", response_model=ModelPricingResponse
)
async def set_price(
    model_id: ModelId,
    body: PriceWrite,
    identity: CurrentIdentity,
    session: Session,
    settings: SettingsDep,
) -> dict[str, Any]:
    admin(identity)
    row = await locked_model(session, model_id)
    old = await receipt(session, model_id, body, "price_change")
    if old is not None:
        return old
    await check_write(session, row, body.expected_version)
    now = datetime.now(UTC)
    start = body.effective_from or now
    if start < now and body.effective_from is not None:
        raise HTTPException(422, "Past prices require a historical correction preview")
    timeline = await intervals(session, row.active_pricing_generation_id)
    boundaries = [
        utc(t) for i in timeline for t in (i.effective_from, i.effective_to) if t and utc(t) > start
    ]
    generation = await make_generation(
        session,
        row,
        body.model_dump() | {"effective_from": start},
        identity,
        end=min(boundaries) if boundaries else None,
        source="price_change",
    )
    generation.published_at = now
    row.active_pricing_generation_id = generation.id
    row.version += 1
    result = await model_response(session, row, settings)
    await record_action(session, model_id, identity, body, "price_change", result)
    return result


async def archive_change(
    model_id: str,
    body: ModelWrite,
    identity: Any,
    session: AsyncSession,
    settings: Any,
    archive: bool,
) -> dict[str, Any]:
    admin(identity)
    row = await locked_model(session, model_id)
    action = "archive" if archive else "restore"
    old = await receipt(session, model_id, body, action)
    if old is not None:
        return old
    if row.version != body.expected_version:
        raise HTTPException(409, "Model changed; reload first")
    if (
        archive
        and row.model_key == settings.llm.default_model
        and row.provider == provider_for(settings.llm.default_model)
    ):
        raise HTTPException(409, "Change the configured default model before archiving it")
    row.archived_at = datetime.now(UTC) if archive else None
    row.archived_by = identity.consumer_id if archive else None
    row.archive_reason = body.reason if archive else None
    row.version += 1
    result = await model_response(session, row, settings)
    await record_action(session, model_id, identity, body, action, result)
    return result


@router.post(
    "/{model_id}/archive", operation_id="archiveAnalyticsModel", response_model=ModelPricingResponse
)
async def archive(
    model_id: ModelId,
    body: ModelWrite,
    identity: CurrentIdentity,
    session: Session,
    settings: SettingsDep,
) -> dict[str, Any]:
    return await archive_change(model_id, body, identity, session, settings, True)


@router.post(
    "/{model_id}/restore", operation_id="restoreAnalyticsModel", response_model=ModelPricingResponse
)
async def restore(
    model_id: ModelId,
    body: ModelWrite,
    identity: CurrentIdentity,
    session: Session,
    settings: SettingsDep,
) -> dict[str, Any]:
    return await archive_change(model_id, body, identity, session, settings, False)


def correction_payload(body: CorrectionWrite) -> dict[str, Any]:
    return body.model_dump(mode="json", exclude={"preview_token"})


# Production instances share the authentication pepper. Local development without
# configured auth uses a process secret: restart simply requires a fresh preview.
_LOCAL_PREVIEW_SECRET = secrets.token_bytes(32)


def preview_key() -> bytes:
    from apex.settings import get_settings

    auth = get_settings().auth
    secret = auth.api_key_hash_pepper
    return secret.encode() if secret else _LOCAL_PREVIEW_SECRET


def preview_hash(row: ModelCatalogEntry, body: CorrectionWrite) -> str:
    return digest(
        {
            "model": row.id,
            "version": row.version,
            "generation": row.active_pricing_generation_id,
            "body": correction_payload(body),
        }
    )


def sign_preview(
    row: ModelCatalogEntry, body: CorrectionWrite, identity: Any, snapshot: dict[str, Any]
) -> str:
    from fastapi.encoders import jsonable_encoder

    payload = base64.urlsafe_b64encode(
        json.dumps(
            {
                "request": preview_hash(row, body),
                "actor": identity.consumer_id,
                "snapshot": jsonable_encoder(snapshot),
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).decode()
    signature = hmac.new(
        preview_key(), b"apex-model-correction-preview-v1:" + payload.encode(), hashlib.sha256
    ).hexdigest()
    return payload + "." + signature


def confirmed_preview(
    row: ModelCatalogEntry, body: CorrectionWrite, identity: Any
) -> dict[str, Any]:
    try:
        payload, signature = (body.preview_token or "").rsplit(".", 1)
        expected = hmac.new(
            preview_key(), b"apex-model-correction-preview-v1:" + payload.encode(), hashlib.sha256
        ).hexdigest()
        if not hmac.compare_digest(signature, expected):
            raise ValueError("Invalid signature")
        decoded = json.loads(base64.urlsafe_b64decode(payload))
        if (
            decoded["request"] != preview_hash(row, body)
            or decoded["actor"] != identity.consumer_id
        ):
            raise ValueError("Preview does not match request")
        snapshot = CorrectionPreviewResponse.model_validate(
            decoded["snapshot"] | {"preview_token": body.preview_token}
        )
        return snapshot.model_dump(mode="json")
    except (ValueError, KeyError, TypeError, ValidationError) as exc:
        raise HTTPException(409, "Preview is stale; preview this correction again") from exc


def validate_correction(body: CorrectionWrite) -> None:
    if body.effective_to <= body.effective_from or body.effective_to > datetime.now(UTC):
        raise HTTPException(422, "Correction requires a past, non-empty interval")


@router.post(
    "/{model_id}/corrections/preview",
    operation_id="previewModelCorrection",
    response_model=CorrectionPreviewResponse,
)
async def preview(
    model_id: ModelId, body: CorrectionWrite, identity: CurrentIdentity, session: Session
) -> dict[str, Any]:
    admin(identity)
    validate_correction(body)
    row = await session.get(ModelCatalogEntry, model_id)
    if row is None:
        raise HTTPException(404, "Model not found")
    await check_write(session, row, body.expected_version)
    calculated_at = datetime.now(UTC)
    query = select(AgentEvent, effective_cost()).where(
        AgentEvent.model_catalog_id == model_id,
        AgentEvent.at >= body.effective_from,
        AgentEvent.at < body.effective_to,
    )
    count, unpriced, before, after, tokens = 0, 0, Decimal(0), Decimal(0), 0
    async for event, cost in await session.stream(query.execution_options(yield_per=500)):
        count += 1
        unpriced += cost is None
        before += cost or Decimal(0)
        after += cost_with_rates(event, body.rates.model_dump())
        tokens += event.total_tokens
    snapshot = {
        "calculated_at": calculated_at,
        "events": count,
        "total_tokens": tokens,
        "previously_unpriced": unpriced,
        "before_cost_usd": str(before),
        "after_cost_usd": str(after),
        "delta_cost_usd": str(after - before),
        "expected_version": row.version,
        "base_generation_id": row.active_pricing_generation_id,
    }
    return snapshot | {"preview_token": sign_preview(row, body, identity, snapshot)}


@router.post(
    "/{model_id}/corrections",
    operation_id="createModelCorrection",
    response_model=CorrectionJobResponse,
    status_code=202,
)
async def correct(
    model_id: ModelId, body: CorrectionWrite, identity: CurrentIdentity, session: Session
) -> dict[str, Any]:
    admin(identity)
    validate_correction(body)
    row = await locked_model(session, model_id)
    old = await session.scalar(
        select(PricingCorrectionJob).where(
            PricingCorrectionJob.model_id == model_id,
            PricingCorrectionJob.idempotency_key == body.idempotency_key,
        )
    )
    hashed = digest(correction_payload(body))
    if old:
        if old.payload_hash != hashed:
            raise HTTPException(409, "Idempotency key was used for a different correction")
        return serial(old)
    await check_write(session, row, body.expected_version)
    reviewed_snapshot = confirmed_preview(row, body, identity)

    generation = await make_generation(
        session,
        row,
        body.model_dump(),
        identity,
        end=body.effective_to,
        source="historical_correction",
    )
    job = PricingCorrectionJob(
        model_id=model_id,
        generation_id=generation.id,
        base_generation_id=row.active_pricing_generation_id,
        expected_version=row.version,
        effective_from=body.effective_from,
        effective_to=body.effective_to,
        actor_id=identity.consumer_id,
        reason=body.reason,
        idempotency_key=body.idempotency_key,
        payload_hash=hashed,
        preview_totals=reviewed_snapshot,
    )
    session.add(job)
    await session.flush()
    generation.job_id = job.id
    result = serial(job)
    await session.commit()
    return result


@router.get(
    "/{model_id}/corrections/{job_id}",
    operation_id="getModelCorrection",
    response_model=CorrectionJobResponse,
)
async def job_status(
    model_id: ModelId, job_id: ModelId, identity: CurrentIdentity, session: Session
) -> dict[str, Any]:
    admin(identity)
    job = await session.get(PricingCorrectionJob, job_id)
    if job is None or job.model_id != model_id:
        raise HTTPException(404, "Correction not found")
    return serial(job)


@router.post(
    "/{model_id}/corrections/{job_id}/retry",
    operation_id="retryModelCorrection",
    response_model=CorrectionJobResponse,
)
async def retry(
    model_id: ModelId, job_id: ModelId, identity: CurrentIdentity, session: Session
) -> dict[str, Any]:
    admin(identity)
    row = await locked_model(session, model_id)
    job = await session.get(PricingCorrectionJob, job_id)
    if job is None or job.model_id != model_id:
        raise HTTPException(404, "Correction not found")
    if job.state != "failed" or row.active_pricing_generation_id != job.base_generation_id:
        raise HTTPException(409, "Correction cannot be retried")
    job.state, job.retry_count, job.failure_code, job.lease_expires_at = "queued", 0, None, None
    await session.flush()
    await session.refresh(job)
    result = serial(job)
    await session.commit()
    return result

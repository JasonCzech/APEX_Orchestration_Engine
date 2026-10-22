"""Versioned model pricing. Published timelines and original events are immutable."""

from __future__ import annotations

import asyncio
import hashlib
import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any
from uuid import uuid4

import structlog
from fastapi import HTTPException
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from apex.persistence.models import (
    AgentEvent,
    ModelAdminAction,
    ModelCatalogEntry,
    ModelPriceInterval,
    ModelPricingGeneration,
    PricingCorrectionJob,
    UsageCostAdjustment,
)
from apex.services.pricing import (
    MODEL_PRICING_ALIASES,
    MODEL_PRICING_USD_PER_MTOK,
    coerce_token_count,
    normalize_cache_token_counts,
)

RATE_KEYS = ("input", "output", "cache_read", "cache_creation")
logger = structlog.get_logger(__name__)


def utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def provider_for(model: str) -> str:
    if ":" in model:
        return model.split(":", 1)[0]
    if "/" in model:
        return model.split("/", 1)[0]
    return (
        "anthropic"
        if model.startswith("claude-")
        else "openai"
        if model.startswith("gpt-")
        else "unknown"
    )


def cost_with_rates(usage: Any, rates: Any) -> Decimal:
    def value(key: str) -> Any:
        return usage.get(key) if isinstance(usage, dict) else getattr(usage, key)

    inputs = coerce_token_count(value("input_tokens"))
    reads, writes = normalize_cache_token_counts(
        inputs, value("cache_read_tokens"), value("cache_creation_tokens")
    )
    amounts = (inputs - reads - writes, coerce_token_count(value("output_tokens")), reads, writes)
    return (
        sum(
            Decimal(n) * Decimal(rates[k] if isinstance(rates, dict) else getattr(rates, k))
            for n, k in zip(amounts, RATE_KEYS, strict=True)
        )
        / Decimal(1_000_000)
    ).quantize(Decimal("0.000001"))


def effective_cost() -> Any:
    adjustment = (
        select(UsageCostAdjustment.cost_usd)
        .join(
            ModelPricingGeneration, ModelPricingGeneration.id == UsageCostAdjustment.generation_id
        )
        .where(
            UsageCostAdjustment.event_id == AgentEvent.id,
            ModelPricingGeneration.published_at.is_not(None),
        )
        .order_by(ModelPricingGeneration.number.desc())
        .limit(1)
        .correlate(AgentEvent)
        .scalar_subquery()
    )
    return func.coalesce(adjustment, AgentEvent.cost_usd)


def digest(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, default=str, separators=(",", ":")).encode()
    ).hexdigest()


async def locked_model(session: AsyncSession, model_id: str) -> ModelCatalogEntry:
    row = await session.scalar(
        select(ModelCatalogEntry)
        .where(ModelCatalogEntry.id == model_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if row is None:
        raise HTTPException(404, "Model not found")
    return row


async def ensure_model(
    session: AsyncSession, key: str, provider: str | None = None, *, seed: bool = False
) -> ModelCatalogEntry:
    provider = provider or provider_for(key)
    # Native upsert avoids rolling back the caller's event transaction on races.
    from sqlalchemy.dialects.postgresql import insert as pg_insert
    from sqlalchemy.dialects.sqlite import insert as sqlite_insert

    insert = pg_insert if session.get_bind().dialect.name == "postgresql" else sqlite_insert
    await session.execute(
        insert(ModelCatalogEntry)
        .values(id=uuid4().hex, provider=provider, model_key=key, display_name=key, version=1)
        .on_conflict_do_nothing(index_elements=["provider", "model_key"])
    )
    row = await session.scalar(
        select(ModelCatalogEntry)
        .where(ModelCatalogEntry.provider == provider, ModelCatalogEntry.model_key == key)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    assert row is not None
    # Only first reconciliation seeds a model; administrator-created gaps stay gaps.
    existing = await session.scalar(
        select(ModelPricingGeneration.id).where(ModelPricingGeneration.model_id == row.id).limit(1)
    )
    if seed and existing is None:
        rates = MODEL_PRICING_USD_PER_MTOK.get(MODEL_PRICING_ALIASES.get(key, key))
        if rates:
            generation = ModelPricingGeneration(
                id=uuid4().hex,
                model_id=row.id,
                number=1,
                reason=f"Initial legacy pricing seed ({MODEL_PRICING_ALIASES.get(key, key)})",
                source="seed",
                published_at=datetime.now(UTC),
            )
            session.add(generation)
            await session.flush()
            session.add(
                ModelPriceInterval(
                    generation_id=generation.id,
                    effective_from=datetime(1970, 1, 1, tzinfo=UTC),
                    **rates,
                )
            )
            row.active_pricing_generation_id = generation.id
    await session.flush()
    return row


async def intervals(session: AsyncSession, generation_id: str | None) -> list[ModelPriceInterval]:
    if generation_id is None:
        return []
    return list(
        (
            await session.scalars(
                select(ModelPriceInterval)
                .where(ModelPriceInterval.generation_id == generation_id)
                .order_by(ModelPriceInterval.effective_from)
            )
        ).all()
    )


async def price_event(session: AsyncSession, values: dict[str, Any]) -> None:
    """Called in the event transaction, retaining the model lock through insertion."""
    values["cost_usd"] = None
    values["extra"] = {k: v for k, v in values.get("extra", {}).items() if k != "pricing"}
    if values.get("consumer_id"):
        from apex.domain.durable_evidence import sanitize_durable_text
        from apex.persistence.models import ApiConsumer

        name = await session.scalar(
            select(ApiConsumer.name).where(ApiConsumer.id == values["consumer_id"])
        )
        if name:
            values["consumer_name_snapshot"] = sanitize_durable_text(name, 255)
    if not values.get("model"):
        return
    row = await ensure_model(session, values["model"], values.get("provider"))
    values["model_catalog_id"] = row.id
    at = utc(values["at"])
    for interval in await intervals(session, row.active_pricing_generation_id):
        if utc(interval.effective_from) <= at and (
            interval.effective_to is None or at < utc(interval.effective_to)
        ):
            values["cost_usd"] = cost_with_rates(values, interval)
            values["pricing_interval_id"] = interval.id
            values["extra"]["pricing"] = {key: str(getattr(interval, key)) for key in RATE_KEYS}
            break


async def make_generation(
    session: AsyncSession,
    model: ModelCatalogEntry,
    body: dict[str, Any],
    actor: Any,
    *,
    end: datetime | None,
    source: str,
) -> ModelPricingGeneration:
    old = await intervals(session, model.active_pricing_generation_id)
    start = utc(body["effective_from"])
    number = (
        await session.scalar(
            select(func.max(ModelPricingGeneration.number)).where(
                ModelPricingGeneration.model_id == model.id
            )
        )
        or 0
    ) + 1
    generation = ModelPricingGeneration(
        id=uuid4().hex,
        model_id=model.id,
        number=number,
        created_by=actor.consumer_id,
        creator_name=actor.name,
        reason=body["reason"],
        source=source,
    )
    session.add(generation)
    await session.flush()
    for interval in old:
        left, right = (
            utc(interval.effective_from),
            utc(interval.effective_to) if interval.effective_to else None,
        )
        rates = {key: getattr(interval, key) for key in RATE_KEYS}
        if (right is not None and right <= start) or (end is not None and left >= end):
            session.add(
                ModelPriceInterval(
                    generation_id=generation.id, effective_from=left, effective_to=right, **rates
                )
            )
        else:
            if left < start:
                session.add(
                    ModelPriceInterval(
                        generation_id=generation.id,
                        effective_from=left,
                        effective_to=start,
                        **rates,
                    )
                )
            if end is not None and (right is None or right > end):
                session.add(
                    ModelPriceInterval(
                        generation_id=generation.id, effective_from=end, effective_to=right, **rates
                    )
                )
    session.add(
        ModelPriceInterval(
            generation_id=generation.id, effective_from=start, effective_to=end, **body["rates"]
        )
    )
    await session.flush()
    return generation


async def check_write(session: AsyncSession, row: ModelCatalogEntry, version: int) -> None:
    if row.version != version:
        raise HTTPException(409, "Model changed; reload and review again")
    pending = await session.scalar(
        select(PricingCorrectionJob.id)
        .where(
            PricingCorrectionJob.model_id == row.id,
            PricingCorrectionJob.state.in_(["queued", "running", "failed"]),
        )
        .limit(1)
    )
    if pending:
        raise HTTPException(409, "A pricing correction is pending; finish or retry it first")


async def reconcile_models(session: AsyncSession, allowed: list[str]) -> None:
    for key in sorted(allowed, key=lambda key: (provider_for(key), key)):
        await ensure_model(session, key, seed=True)
    await session.commit()
    # Bounded resumable batches; never infer user identity from names or thread owner.
    events = list(
        (
            await session.scalars(
                select(AgentEvent)
                .where(AgentEvent.model_catalog_id.is_(None), AgentEvent.model.is_not(None))
                .order_by(AgentEvent.id)
                .limit(500)
            )
        ).all()
    )
    # All workers acquire model locks in the same identity order. Recheck after
    # acquiring the lock because another worker may have backfilled this batch
    # while we waited; otherwise the retained correction insert can collide.
    events.sort(
        key=lambda event: (
            event.provider or provider_for(event.model or ""),
            event.model or "",
            event.id,
        )
    )
    for event in events:
        row = await ensure_model(session, event.model or "", event.provider, seed=True)
        await session.refresh(event)
        if event.model_catalog_id is not None:
            continue
        event.model_catalog_id = row.id
        # A previously unlinked event may arrive after a correction publishes.
        # Attach the published correction without rewriting its original snapshot.
        correction = await session.scalar(
            select(PricingCorrectionJob)
            .join(
                ModelPricingGeneration,
                ModelPricingGeneration.id == PricingCorrectionJob.generation_id,
            )
            .where(
                PricingCorrectionJob.model_id == row.id,
                PricingCorrectionJob.state == "succeeded",
                PricingCorrectionJob.effective_from <= event.at,
                PricingCorrectionJob.effective_to > event.at,
            )
            .order_by(ModelPricingGeneration.number.desc())
            .limit(1)
        )
        if correction:
            for interval in await intervals(session, correction.generation_id):
                if utc(interval.effective_from) <= utc(event.at) and (
                    interval.effective_to is None or utc(event.at) < utc(interval.effective_to)
                ):
                    session.add(
                        UsageCostAdjustment(
                            event_id=event.id,
                            generation_id=correction.generation_id,
                            interval_id=interval.id,
                            cost_usd=cost_with_rates(event, interval),
                            job_id=correction.id,
                        )
                    )
                    break
    await session.commit()


async def process_correction(session: AsyncSession, job_id: str, owner: str) -> None:
    job = await session.scalar(
        select(PricingCorrectionJob).where(PricingCorrectionJob.id == job_id).with_for_update()
    )
    if job is None or job.state not in ("queued", "running"):
        return
    now = datetime.now(UTC)
    if job.lease_owner != owner and job.lease_expires_at and utc(job.lease_expires_at) > now:
        return
    job.state, job.lease_owner, job.lease_expires_at = "running", owner, now + timedelta(seconds=60)
    await session.commit()
    # Lock model before job, consistently with administrative writes and event writers.
    row = await locked_model(session, job.model_id)
    await session.refresh(job, with_for_update=True)
    if job.lease_owner != owner or job.state != "running":
        await session.rollback()
        return
    if row.active_pricing_generation_id != job.base_generation_id:
        job.state = "superseded"
        await session.commit()
        return
    priced = select(UsageCostAdjustment.event_id).where(
        UsageCostAdjustment.generation_id == job.generation_id
    )
    events = list(
        (
            await session.scalars(
                select(AgentEvent)
                .where(
                    AgentEvent.model_catalog_id == row.id,
                    AgentEvent.at >= job.effective_from,
                    AgentEvent.at < job.effective_to,
                    ~AgentEvent.id.in_(priced),
                )
                .order_by(AgentEvent.id)
                .limit(500)
            )
        ).all()
    )
    timeline = await intervals(session, job.generation_id)
    for event in events:
        interval = next(
            i
            for i in timeline
            if utc(i.effective_from) <= utc(event.at)
            and (i.effective_to is None or utc(event.at) < utc(i.effective_to))
        )
        session.add(
            UsageCostAdjustment(
                event_id=event.id,
                generation_id=job.generation_id,
                interval_id=interval.id,
                cost_usd=cost_with_rates(event, interval),
                job_id=job.id,
            )
        )
        job.progress_cursor = event.id
    job.processed_count += len(events)
    job.lease_expires_at = datetime.now(UTC) + timedelta(seconds=60)
    if not events:
        # Empty catch-up under model lock guarantees no concurrent insertion was missed.
        generation = await session.get(ModelPricingGeneration, job.generation_id)
        assert generation is not None
        generation.published_at = datetime.now(UTC)
        row.active_pricing_generation_id = generation.id
        row.version += 1
        job.state = "succeeded"
        total = await session.scalar(
            select(func.sum(UsageCostAdjustment.cost_usd)).where(
                UsageCostAdjustment.job_id == job.id
            )
        )
        job.final_totals = {"events": job.processed_count, "cost_usd": str(total or Decimal(0))}
        session.add(
            ModelAdminAction(
                model_id=row.id,
                actor_id=job.actor_id,
                action="correction_published",
                reason=job.reason,
                idempotency_key="publish:" + job.id,
                payload_hash=job.payload_hash,
                result=job.final_totals,
            )
        )
    await session.commit()


async def run_pricing_worker(stop: asyncio.Event, heartbeat: Any = None) -> None:
    from apex.persistence.db import get_sessionmaker
    from apex.settings import get_settings

    owner = uuid4().hex
    while not stop.is_set():
        job_id = None
        try:
            async with get_sessionmaker()() as session:
                await reconcile_models(session, get_settings().llm.allowed_models)
                job_id = await session.scalar(
                    select(PricingCorrectionJob.id)
                    .where(
                        PricingCorrectionJob.state.in_(["queued", "running"]),
                        or_(
                            PricingCorrectionJob.lease_owner == owner,
                            PricingCorrectionJob.lease_expires_at.is_(None),
                            PricingCorrectionJob.lease_expires_at < datetime.now(UTC),
                        ),
                    )
                    .order_by(PricingCorrectionJob.created_at)
                    .limit(1)
                )
                if job_id:
                    await process_correction(session, job_id, owner)
        except Exception:
            logger.warning("pricing.worker_failed", job_id=job_id)
            if job_id:
                try:
                    async with get_sessionmaker()() as session:
                        job = await session.scalar(
                            select(PricingCorrectionJob)
                            .where(PricingCorrectionJob.id == job_id)
                            .with_for_update()
                        )
                        if job and job.lease_owner == owner and job.state == "running":
                            job.retry_count += 1
                            job.failure_code = "recalculation_failed"
                            job.state = "failed" if job.retry_count >= 3 else "queued"
                            job.lease_expires_at = None
                            await session.commit()
                except Exception:
                    logger.warning("pricing.job_failure_record_failed")
        if heartbeat:
            heartbeat()
        try:
            await asyncio.wait_for(stop.wait(), timeout=0.1 if job_id else 5)
        except TimeoutError:
            pass


async def admit_models(
    configurable: dict[str, Any], *, thread_id: str | None, actor_id: str, resume: bool = False
) -> dict[str, str]:
    """Resolve choices at admission; continuation can retain only identical choices."""
    from apex.domain.pipeline import Phase
    from apex.persistence.db import get_sessionmaker
    from apex.persistence.models import ModelRunAdmission
    from apex.settings import get_settings

    settings = get_settings()
    supplied = configurable.get("model_by_phase") or {}
    phases = list(Phase)
    active_phases = phases
    if configurable.get("phase_execution") is not None:
        from apex.graphs.pipeline.configurable import PipelineConfigurable

        # Admission also receives sparse API inputs and runtime-only fields.
        # Validate the ownership/window contract independently of those fields.
        execution = PipelineConfigurable.model_validate(
            {
                "phase_execution": configurable["phase_execution"],
                "rerun_phases": configurable.get("rerun_phases"),
            }
        )
        phases = [phase for phase in phases if execution.executor(phase) == "langgraph"]
        active_phases = execution.agent_phases()
    resolved = {
        phase.value: supplied.get(phase.value, settings.llm.default_model) for phase in phases
    }
    selected = {phase.value: resolved[phase.value] for phase in active_phases}
    if not selected:
        return resolved
    async with get_sessionmaker()() as session:
        # Resume is only requested by the trusted facade after reading the durable
        # checkpoint. Retain every admitted selection, so a rejected concurrent
        # launch cannot overwrite the choices of the execution being resumed.
        selection_hash = digest(selected)
        admission = (
            await session.get(ModelRunAdmission, (thread_id, selection_hash)) if thread_id else None
        )
        if resume and admission is not None:
            return {**resolved, **admission.models}
        if any(key not in settings.llm.allowed_models for key in selected.values()):
            raise HTTPException(422, "Selected model is not allowlisted")
        for key in sorted(set(selected.values()), key=lambda key: (provider_for(key), key)):
            row = await ensure_model(session, key)
            if row.archived_at:
                raise HTTPException(409, f"Model {key} is archived; select an active model")
        if thread_id and admission is None:
            from sqlalchemy.dialects.postgresql import insert as pg_insert
            from sqlalchemy.dialects.sqlite import insert as sqlite_insert

            insert = pg_insert if session.get_bind().dialect.name == "postgresql" else sqlite_insert
            await session.execute(
                insert(ModelRunAdmission)
                .values(
                    thread_id=thread_id,
                    models_hash=selection_hash,
                    models=selected,
                    actor_id=actor_id,
                )
                .on_conflict_do_nothing(index_elements=["thread_id", "models_hash"])
            )
        await session.commit()
    # The hook writes this mapping back to the durable configuration. Keep
    # choices outside this rerun window for subsequent phase reruns.
    return resolved


async def initialize_pricing() -> None:
    from apex.persistence.db import get_sessionmaker
    from apex.settings import get_settings

    async with get_sessionmaker()() as session:
        await reconcile_models(session, get_settings().llm.allowed_models)

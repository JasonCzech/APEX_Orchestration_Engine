"""Durable claims prevent replaying native RCA creates after worker loss.

The native service deduplicates frozen inputs only while their analysis is live
or completed. A failed job cannot be looked up without its remote identity.
Commit the claim before POST and its validated receipt before graph checkpointing;
a replay with no receipt requires an explicit operator retry using a new key.
"""

import hashlib
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime

from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from apex.domain.diagnostics import contains_credential_material
from apex.domain.input_limits import NoNulStr
from apex.persistence.db import dispose_engine_instance_definitively
from apex.persistence.models import NativeAnalysisSubmission
from apex.ports.analysis import AnalysisJob
from apex.settings import database_asyncpg_uri, database_ssl_connect_args, get_settings


class NativeAnalysisSubmissionUncertain(RuntimeError):
    """A prior worker may have submitted the request, but no receipt is durable."""


class NativeAnalysisScope(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    thread_id: NoNulStr = Field(min_length=1, max_length=255)
    phase_attempt: int = Field(ge=1, le=1_000_000)
    project_id: NoNulStr | None = Field(default=None, max_length=256)
    connection_id: NoNulStr = Field(min_length=1, max_length=256)
    native_run_id: NoNulStr = Field(min_length=1, max_length=256)
    input_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    idempotency_key: str = Field(pattern=r"^[a-f0-9]{64}$")

    @model_validator(mode="after")
    def safe_scope(self):
        if contains_credential_material(self.model_dump(mode="json")):
            raise ValueError("Native analysis scope contains credential material")
        return self

    @property
    def digest(self) -> str:
        return hashlib.sha256(self.model_dump_json().encode()).hexdigest()


@asynccontextmanager
async def _receipt_session() -> AsyncIterator[AsyncSession]:
    database = get_settings().database
    engine = None
    try:
        engine = create_async_engine(
            database_asyncpg_uri(database.uri),
            poolclass=NullPool,
            connect_args=database_ssl_connect_args(database.uri, database.ssl_mode),
        )
        async with async_sessionmaker(engine, expire_on_commit=False)() as session:
            yield session
    except SQLAlchemyError:
        # Never include database connection strings or statement values.
        raise RuntimeError("Native analysis submission persistence is unavailable") from None
    finally:
        if engine is not None:
            await dispose_engine_instance_definitively(engine)


async def claim_native_analysis(scope: NativeAnalysisScope) -> str | None:
    """Return None only for the worker whose new reservation has committed."""
    async with _receipt_session() as session:
        dialect = session.get_bind().dialect.name
        if dialect not in {"postgresql", "sqlite"}:
            raise RuntimeError("Native analysis submission storage is unsupported")
        insert = pg_insert if dialect == "postgresql" else sqlite_insert
        statement = (
            insert(NativeAnalysisSubmission)
            .values(
                idempotency_key=scope.idempotency_key,
                scope_digest=scope.digest,
                thread_id=scope.thread_id,
                connection_id=scope.connection_id,
                native_run_id=scope.native_run_id,
            )
            .on_conflict_do_nothing(index_elements=["idempotency_key"])
            .returning(NativeAnalysisSubmission.idempotency_key)
        )
        inserted = (await session.scalar(statement)) is not None
        receipt = await session.scalar(
            select(NativeAnalysisSubmission).where(
                NativeAnalysisSubmission.idempotency_key == scope.idempotency_key
            )
        )
        if receipt is None or receipt.scope_digest != scope.digest:
            raise ValueError("Native analysis submission is bound to different inputs")
        job_id = receipt.job_id
        await session.commit()
        if inserted:
            return None
        if job_id is None:
            raise NativeAnalysisSubmissionUncertain(
                "A prior native RCA submission has no confirmed remote identity"
            )
        return AnalysisJob(job_id=job_id, status="queued").job_id


async def record_native_analysis_receipt(scope: NativeAnalysisScope, job_id: str) -> None:
    validated = AnalysisJob(job_id=job_id, status="queued")
    async with _receipt_session() as session:
        changed = await session.scalar(
            update(NativeAnalysisSubmission)
            .where(
                NativeAnalysisSubmission.idempotency_key == scope.idempotency_key,
                NativeAnalysisSubmission.scope_digest == scope.digest,
                NativeAnalysisSubmission.job_id.is_(None)
                | (NativeAnalysisSubmission.job_id == validated.job_id),
            )
            .values(job_id=validated.job_id, received_at=datetime.now(UTC))
            .returning(NativeAnalysisSubmission.idempotency_key)
        )
        if changed is None:
            raise ValueError("Native analysis receipt conflicts with its durable reservation")
        await session.commit()

"""Postgres atomic reservation contract for the native RCA crash boundary."""

import asyncio
import hashlib
import os
from contextlib import asynccontextmanager
from uuid import uuid4

import pytest
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from apex.persistence.models import NativeAnalysisSubmission
from apex.services import native_analysis_submissions as service

pytestmark = pytest.mark.skipif(
    not os.environ.get("APEX_TEST_DATABASE_URI"), reason="needs migrated postgres"
)


async def test_postgres_claim_serialization_and_receipt_recovery(monkeypatch):
    engine = create_async_engine(os.environ["APEX_TEST_DATABASE_URI"], poolclass=NullPool)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    scope = service.NativeAnalysisScope(
        thread_id="receipt-integration-" + uuid4().hex,
        phase_attempt=1,
        connection_id="native-fixture",
        native_run_id="native-run-fixture",
        input_digest="e" * 64,
        idempotency_key=hashlib.sha256(uuid4().bytes).hexdigest(),
    )

    @asynccontextmanager
    async def session_factory():
        async with factory() as session:
            yield session

    monkeypatch.setattr(service, "_receipt_session", session_factory)

    async def claim():
        try:
            return await service.claim_native_analysis(scope)
        except service.NativeAnalysisSubmissionUncertain:
            return "unconfirmed"

    try:
        results = await asyncio.gather(claim(), claim())
        assert results.count(None) == 1
        assert results.count("unconfirmed") == 1
        await service.record_native_analysis_receipt(scope, "recorded-native-job")
        assert await service.claim_native_analysis(scope) == "recorded-native-job"
    finally:
        async with factory.begin() as session:
            await session.execute(
                delete(NativeAnalysisSubmission).where(
                    NativeAnalysisSubmission.idempotency_key == scope.idempotency_key
                )
            )
        await engine.dispose()

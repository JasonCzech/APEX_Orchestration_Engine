"""Durable state transitions for idempotent work-item mutations."""

import asyncio
import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from apex.domain.durable_evidence import sanitize_durable_text
from apex.persistence.models import Connection, WorkItemMutation, WorkItemMutationTombstone

MUTATION_LEASE = timedelta(minutes=5)


class MutationPayloadConflictError(ValueError):
    """An idempotency key was reused with a different canonical payload."""


class MutationClaimedError(RuntimeError):
    """Another request or reconciler currently owns the mutation lease."""


class MutationRetiredError(RuntimeError):
    """A compact tombstone prevents replay after the live result was retired."""


class MutationConnectionChangedError(RuntimeError):
    """The connection changed after the adapter generation was resolved."""


@dataclass(frozen=True)
class MutationScope:
    tenant_scope: str
    consumer_id: str
    connection_id: str
    operation: str
    idempotency_key: str


class WorkItemMutationsRepository:
    """Short-transaction repository with lost-commit acknowledgement recovery."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def reserve(
        self,
        *,
        scope: MutationScope,
        payload_hash: str,
        payload: dict[str, Any],
        target_key: str | None,
        project_id: str | None,
        connection_version: datetime,
        fields_status: str,
        comment_status: str,
    ) -> WorkItemMutation:
        existing = await self.get_by_scope(scope)
        if existing is not None:
            self._validate_payload(existing, payload_hash, payload)
            self._validate_connection_version(existing, connection_version)
            return existing
        tombstone = await self.get_tombstone(scope)
        if tombstone is not None:
            if tombstone.payload_hash != payload_hash:
                raise MutationPayloadConflictError(
                    "idempotency key is already bound to a different retired payload"
                )
            raise MutationRetiredError(
                "idempotency key is retired and cannot safely repeat its provider mutation"
            )

        row_id = uuid4().hex
        row = WorkItemMutation(
            id=row_id,
            provider_marker=f"apex-idem-{row_id}",
            tenant_scope=scope.tenant_scope,
            consumer_id=scope.consumer_id,
            project_id=project_id,
            connection_id=scope.connection_id,
            connection_version=connection_version,
            operation=scope.operation,
            idempotency_key=scope.idempotency_key,
            payload_hash=payload_hash,
            payload=payload,
            target_key=target_key,
            fields_status=fields_status,
            comment_status=comment_status,
        )
        async with self._session_factory() as session:
            connection = await session.scalar(
                select(Connection).where(Connection.id == scope.connection_id).with_for_update()
            )
            if connection is None or not connection.enabled or connection.kind != "work_tracking":
                raise MutationConnectionChangedError(
                    "work-tracking connection is missing or disabled"
                )
            if _as_utc(connection.runtime_version) != _as_utc(connection_version):
                raise MutationConnectionChangedError(
                    "work-tracking connection changed during mutation reservation"
                )
            row.connection_version = connection_version
            session.add(row)
            try:
                await session.commit()
                return row
            except asyncio.CancelledError:
                await _rollback_quietly(session)
                raise
            except BaseException:
                await _rollback_quietly(session)
                # The insert may have committed before the connection failed, or
                # a concurrent request may have won the unique scope key.
                resolved = await self.get_by_scope(scope)
                if resolved is None:
                    raise
                self._validate_payload(resolved, payload_hash, payload)
                self._validate_connection_version(resolved, connection_version)
                return resolved

    async def get_by_scope(self, scope: MutationScope) -> WorkItemMutation | None:
        async with self._session_factory() as session:
            return await session.scalar(
                select(WorkItemMutation).where(
                    WorkItemMutation.tenant_scope == scope.tenant_scope,
                    WorkItemMutation.consumer_id == scope.consumer_id,
                    WorkItemMutation.connection_id == scope.connection_id,
                    WorkItemMutation.operation == scope.operation,
                    WorkItemMutation.idempotency_key == scope.idempotency_key,
                )
            )

    async def inspect(
        self,
        *,
        scope: MutationScope,
        payload_hash: str,
        payload: dict[str, Any],
        connection_version: datetime,
    ) -> WorkItemMutation | None:
        """Look up an existing key without reserving a new provider mutation."""

        existing = await self.get_by_scope(scope)
        if existing is not None:
            self._validate_payload(existing, payload_hash, payload)
            self._validate_connection_version(existing, connection_version)
            return existing
        tombstone = await self.get_tombstone(scope)
        if tombstone is None:
            return None
        if tombstone.payload_hash != payload_hash:
            raise MutationPayloadConflictError(
                "idempotency key is already bound to a different retired payload"
            )
        raise MutationRetiredError(
            "idempotency key is retired and cannot safely repeat its provider mutation"
        )

    async def get(self, mutation_id: str) -> WorkItemMutation | None:
        async with self._session_factory() as session:
            return await session.get(WorkItemMutation, mutation_id)

    async def get_tombstone(self, scope: MutationScope) -> WorkItemMutationTombstone | None:
        async with self._session_factory() as session:
            return await session.get(WorkItemMutationTombstone, mutation_scope_hash(scope))

    async def claim(
        self,
        mutation_id: str,
        *,
        ignore_backoff: bool,
    ) -> WorkItemMutation:
        token = uuid4().hex
        now = datetime.now(UTC)
        async with self._session_factory() as session:
            row = await session.scalar(
                select(WorkItemMutation).where(WorkItemMutation.id == mutation_id).with_for_update()
            )
            if row is None:
                raise RuntimeError("work-item mutation disappeared before it was claimed")
            if row.status in {"completed", "failed"}:
                return row
            claimed_at = _as_utc(row.claimed_at)
            if (
                row.status == "running"
                and claimed_at is not None
                and claimed_at > now - MUTATION_LEASE
            ):
                raise MutationClaimedError("work-item mutation is already in progress")
            next_attempt_at = _as_utc(row.next_attempt_at)
            if not ignore_backoff and next_attempt_at is not None and next_attempt_at > now:
                raise MutationClaimedError("work-item mutation retry is not due yet")
            row.status = "running"
            row.claim_token = token
            row.claimed_at = now
            row.next_attempt_at = None
            row.attempt_count = int(row.attempt_count or 0) + 1
            row.last_error = None
            try:
                await session.commit()
                return row
            except asyncio.CancelledError:
                await _rollback_quietly(session)
                raise
            except BaseException:
                await _rollback_quietly(session)
                resolved = await self.get(mutation_id)
                if resolved is not None and (
                    resolved.status in {"completed", "failed"}
                    or (resolved.status == "running" and resolved.claim_token == token)
                ):
                    return resolved
                raise

    async def mark_step(
        self,
        mutation_id: str,
        *,
        claim_token: str,
        step: str,
    ) -> WorkItemMutation:
        if step not in {"fields", "comment"}:
            raise ValueError(f"unknown work-item mutation step {step!r}")

        def mutate(row: WorkItemMutation) -> None:
            setattr(row, f"{step}_status", "completed")

        def resolved(row: WorkItemMutation) -> bool:
            return getattr(row, f"{step}_status") == "completed"

        return await self._transition(
            mutation_id,
            claim_token=claim_token,
            mutate=mutate,
            resolved=resolved,
            require_owner_for_resolution=True,
        )

    async def mark_provider_attempted(
        self,
        mutation_id: str,
        *,
        claim_token: str,
    ) -> WorkItemMutation:
        """Fence create POSTs behind a durable, at-most-once attempt record."""

        attempted_at = datetime.now(UTC)

        def mutate(row: WorkItemMutation) -> None:
            row.provider_attempted_at = attempted_at

        def resolved(row: WorkItemMutation) -> bool:
            return row.provider_attempted_at is not None

        return await self._transition(
            mutation_id,
            claim_token=claim_token,
            mutate=mutate,
            resolved=resolved,
            require_owner_for_resolution=True,
        )

    async def mark_comment_attempted(
        self,
        mutation_id: str,
        *,
        claim_token: str,
    ) -> WorkItemMutation:
        """Fence comment POSTs behind a durable, at-most-once attempt record."""

        attempted_at = datetime.now(UTC)

        def mutate(row: WorkItemMutation) -> None:
            row.comment_attempted_at = attempted_at

        def resolved(row: WorkItemMutation) -> bool:
            return row.comment_attempted_at is not None

        return await self._transition(
            mutation_id,
            claim_token=claim_token,
            mutate=mutate,
            resolved=resolved,
            require_owner_for_resolution=True,
        )

    async def renew(
        self,
        mutation_id: str,
        *,
        claim_token: str,
    ) -> WorkItemMutation:
        """Renew an owned lease while provider reconciliation is still active."""

        now = datetime.now(UTC)

        def mutate(row: WorkItemMutation) -> None:
            row.claimed_at = now

        return await self._transition(
            mutation_id,
            claim_token=claim_token,
            mutate=mutate,
            resolved=lambda _row: False,
        )

    async def complete(
        self,
        mutation_id: str,
        *,
        claim_token: str,
        result: dict[str, Any],
    ) -> WorkItemMutation:
        def mutate(row: WorkItemMutation) -> None:
            row.status = "completed"
            row.result = result
            row.claim_token = None
            row.claimed_at = None
            row.next_attempt_at = None
            row.last_error = None

        def resolved(row: WorkItemMutation) -> bool:
            return row.status == "completed" and row.result == result

        return await self._transition(
            mutation_id,
            claim_token=claim_token,
            mutate=mutate,
            resolved=resolved,
        )

    async def fail(
        self,
        mutation_id: str,
        *,
        claim_token: str,
        error_kind: str,
        error: str,
    ) -> WorkItemMutation:
        def mutate(row: WorkItemMutation) -> None:
            row.status = "failed"
            row.terminal_error = error_kind
            row.last_error = _safe_error_text(error)
            row.claim_token = None
            row.claimed_at = None
            row.next_attempt_at = None

        def resolved(row: WorkItemMutation) -> bool:
            return row.status == "failed" and row.terminal_error == error_kind

        return await self._transition(
            mutation_id,
            claim_token=claim_token,
            mutate=mutate,
            resolved=resolved,
        )

    async def release(
        self,
        mutation_id: str,
        *,
        claim_token: str,
        error: str,
        retry_delay: timedelta = timedelta(seconds=1),
    ) -> WorkItemMutation:
        retry_at = datetime.now(UTC) + retry_delay

        def mutate(row: WorkItemMutation) -> None:
            row.status = "pending"
            row.last_error = _safe_error_text(error)
            row.claim_token = None
            row.claimed_at = None
            row.next_attempt_at = retry_at

        def resolved(row: WorkItemMutation) -> bool:
            return row.status in {"pending", "completed", "failed"}

        return await self._transition(
            mutation_id,
            claim_token=claim_token,
            mutate=mutate,
            resolved=resolved,
        )

    async def defer_resolution(
        self,
        mutation_id: str,
        *,
        error: str,
        retry_delay: timedelta,
    ) -> WorkItemMutation:
        """Back off rows that fail before an adapter can be constructed."""

        now = datetime.now(UTC)
        retry_at = now + retry_delay
        async with self._session_factory() as session:
            row = await session.scalar(
                select(WorkItemMutation).where(WorkItemMutation.id == mutation_id).with_for_update()
            )
            if row is None:
                raise RuntimeError("work-item mutation disappeared while deferring resolution")
            if row.status in {"completed", "failed"}:
                return row
            claimed_at = _as_utc(row.claimed_at)
            if (
                row.status == "running"
                and claimed_at is not None
                and claimed_at > now - MUTATION_LEASE
            ):
                raise MutationClaimedError("work-item mutation is already in progress")
            row.status = "pending"
            row.claim_token = None
            row.claimed_at = None
            row.next_attempt_at = retry_at
            row.attempt_count = int(row.attempt_count or 0) + 1
            row.last_error = _safe_error_text(error)
            await session.commit()
            return row

    async def ready_ids(self, *, limit: int = 25) -> list[str]:
        now = datetime.now(UTC)
        stale_before = now - MUTATION_LEASE
        async with self._session_factory() as session:
            rows = await session.scalars(
                select(WorkItemMutation.id)
                .where(
                    or_(
                        (
                            (WorkItemMutation.status == "pending")
                            & or_(
                                WorkItemMutation.next_attempt_at.is_(None),
                                WorkItemMutation.next_attempt_at <= now,
                            )
                        ),
                        (
                            (WorkItemMutation.status == "running")
                            & or_(
                                WorkItemMutation.claimed_at.is_(None),
                                WorkItemMutation.claimed_at <= stale_before,
                            )
                        ),
                    )
                )
                .order_by(WorkItemMutation.created_at, WorkItemMutation.id)
                .limit(limit)
            )
            return list(rows)

    async def retire_terminal_before(
        self,
        cutoff: datetime,
        *,
        limit: int = 250,
    ) -> int:
        """Compact old terminal rows into permanent fixed-size key claims."""

        async with self._session_factory() as session:
            rows = list(
                (
                    await session.scalars(
                        select(WorkItemMutation)
                        .where(
                            WorkItemMutation.status.in_(("completed", "failed")),
                            WorkItemMutation.updated_at < cutoff,
                        )
                        .order_by(WorkItemMutation.updated_at, WorkItemMutation.id)
                        .limit(limit)
                        .with_for_update(skip_locked=True)
                    )
                ).all()
            )
            if not rows:
                return 0
            expected: list[tuple[str, str, str]] = []
            for row in rows:
                scope_hash = mutation_scope_hash(
                    MutationScope(
                        tenant_scope=row.tenant_scope,
                        consumer_id=row.consumer_id,
                        connection_id=row.connection_id,
                        operation=row.operation,
                        idempotency_key=row.idempotency_key,
                    )
                )
                expected.append((scope_hash, row.payload_hash, row.id))
                tombstone = await session.get(WorkItemMutationTombstone, scope_hash)
                if tombstone is None:
                    session.add(
                        WorkItemMutationTombstone(
                            scope_hash=scope_hash,
                            payload_hash=row.payload_hash,
                            outcome=row.status,
                        )
                    )
                elif tombstone.payload_hash != row.payload_hash:
                    raise RuntimeError("work-item mutation tombstone hash collision")
                await session.delete(row)
            try:
                await session.commit()
                return len(rows)
            except asyncio.CancelledError:
                await _rollback_quietly(session)
                raise
            except BaseException:
                await _rollback_quietly(session)
                if await self._retirement_matches(expected):
                    return len(rows)
                raise

    async def _retirement_matches(self, expected: list[tuple[str, str, str]]) -> bool:
        async with self._session_factory() as session:
            for scope_hash, payload_hash, mutation_id in expected:
                tombstone = await session.get(WorkItemMutationTombstone, scope_hash)
                live = await session.get(WorkItemMutation, mutation_id)
                if tombstone is None or tombstone.payload_hash != payload_hash or live is not None:
                    return False
            return True

    async def _transition(
        self,
        mutation_id: str,
        *,
        claim_token: str,
        mutate: Any,
        resolved: Any,
        require_owner_for_resolution: bool = False,
    ) -> WorkItemMutation:
        async with self._session_factory() as session:
            row = await session.scalar(
                select(WorkItemMutation).where(WorkItemMutation.id == mutation_id).with_for_update()
            )
            if row is None:
                raise RuntimeError("work-item mutation disappeared during state transition")
            if require_owner_for_resolution and (
                row.status != "running" or row.claim_token != claim_token
            ):
                raise MutationClaimedError("work-item mutation lease was lost")
            if resolved(row):
                return row
            if row.status != "running" or row.claim_token != claim_token:
                raise MutationClaimedError("work-item mutation lease was lost")
            mutate(row)
            try:
                await session.commit()
                return row
            except asyncio.CancelledError:
                await _rollback_quietly(session)
                raise
            except BaseException:
                await _rollback_quietly(session)
                authoritative = await self.get(mutation_id)
                if (
                    authoritative is not None
                    and (
                        not require_owner_for_resolution
                        or (
                            authoritative.status == "running"
                            and authoritative.claim_token == claim_token
                        )
                    )
                    and resolved(authoritative)
                ):
                    return authoritative
                raise

    @staticmethod
    def _validate_payload(
        row: WorkItemMutation, payload_hash: str, payload: dict[str, Any]
    ) -> None:
        if row.payload_hash != payload_hash or row.payload != payload:
            raise MutationPayloadConflictError(
                "idempotency key is already bound to a different work-item mutation payload"
            )

    @staticmethod
    def _validate_connection_version(row: WorkItemMutation, connection_version: datetime) -> None:
        if _as_utc(row.connection_version) != _as_utc(connection_version):
            raise MutationConnectionChangedError(
                "work-tracking connection changed after this mutation was reserved"
            )


async def _rollback_quietly(session: AsyncSession) -> None:
    try:
        await session.rollback()
    except BaseException:
        pass


def _as_utc(value: datetime | None) -> datetime | None:
    if value is None or value.tzinfo is not None:
        return value
    return value.replace(tzinfo=UTC)


def mutation_scope_hash(scope: MutationScope) -> str:
    """Fixed-size digest used by compact permanent tombstones."""

    encoded = json.dumps(
        {
            "tenant_scope": scope.tenant_scope,
            "consumer_id": scope.consumer_id,
            "connection_id": scope.connection_id,
            "operation": scope.operation,
            "idempotency_key": scope.idempotency_key,
        },
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _safe_error_text(error: str) -> str:
    """Keep diagnostics PostgreSQL-Text-safe and bounded."""

    return sanitize_durable_text(error, 4096) or ""

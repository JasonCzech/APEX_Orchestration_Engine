"""Durable idempotency coordinator for work-tracking provider mutations."""

import asyncio
import hashlib
import json
import re
import threading
from collections.abc import Callable, Coroutine
from datetime import UTC, datetime, timedelta
from functools import lru_cache
from inspect import isawaitable
from typing import Any
from uuid import uuid4

import structlog

from apex.adapters.registry import PortKind
from apex.auth.identity import ConsumerIdentity, Role
from apex.domain.diagnostics import contains_credential_material, safe_type_name
from apex.domain.durable_evidence import sanitize_durable_text
from apex.domain.integrations import Enrichment, WorkItem, WorkItemDraft
from apex.persistence.db import get_sessionmaker
from apex.persistence.models import WorkItemMutation
from apex.persistence.repositories.work_item_mutations import (
    MUTATION_LEASE,
    MutationClaimedError,
    MutationConnectionChangedError,
    MutationPayloadConflictError,
    MutationScope,
    WorkItemMutationsRepository,
)
from apex.ports.work_tracking import (
    IdempotentWorkTrackingMutationPort,
    WorkTrackingMutationRejectedError,
    WorkTrackingMutationTargetNotFoundError,
)
from apex.services.connections import ConnectionResolver, close_adapter, get_connection_resolver
from apex.services.work_items import validated_provider_work_item
from apex.settings import get_settings

logger = structlog.get_logger(__name__)

RECONCILE_INTERVAL_S = 5.0
TERMINAL_REPLAY_RETENTION = timedelta(days=30)
_RETRY_DELAY = timedelta(seconds=30)
_MAX_RETRY_DELAY = timedelta(hours=1)
_LEASE_HEARTBEAT_S = 60.0
_WORK_ITEM_TARGET_KEY = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9_.:-]{0,254}\Z")


class _PermanentMutationFailure(Exception):
    """A provider response proves that retrying the durable operation is unsafe/useless."""

    def __init__(self, error_kind: str) -> None:
        super().__init__(f"work-item mutation permanently failed ({error_kind})")
        self.error_kind = error_kind


class WorkItemMutationOutcomeAmbiguousError(RuntimeError):
    """A non-idempotent provider dispatch may or may not have happened.

    The durable fence deliberately prevents an automatic second POST. Operators
    can use the mutation id and provider marker to reconcile the provider without
    mistaking at-most-once dispatch for an exactly-once guarantee.
    """

    def __init__(self, row: WorkItemMutation, *, operation: str) -> None:
        self.mutation_id = sanitize_durable_text(row.id, 255) or "unknown"
        self.provider_marker = sanitize_durable_text(row.provider_marker, 255) or "unknown"
        self.operation = operation if operation in {"create", "comment"} else "mutation"
        super().__init__(
            f"work-item {self.operation} dispatch outcome is ambiguous; mutation "
            f"{self.mutation_id!r} will not be redispatched automatically (provider marker "
            f"{self.provider_marker!r})"
        )


class WorkItemMutationService:
    """Coordinates durable at-most-once dispatch with provider-side reconciliation."""

    def __init__(
        self,
        repository: WorkItemMutationsRepository | Any,
        *,
        ephemeral_repository: Any | None = None,
    ) -> None:
        self._repository = repository
        self._ephemeral_repository = ephemeral_repository or _EphemeralMutationRepository()

    async def create(
        self,
        *,
        adapter: Any,
        draft: WorkItemDraft,
        identity: ConsumerIdentity,
        project_id: str | None,
        connection_id: str,
        connection_persisted: bool,
        connection_version: datetime | None,
        idempotency_key: str,
    ) -> WorkItem:
        # Fail before persisting an intent that the reconciler could never run.
        _require_idempotent_adapter(adapter)
        draft = _validated_durable_draft(draft)
        repository = self._repository_for(connection_persisted)
        payload = {"draft": draft.model_dump(mode="json")}
        row = await repository.reserve(
            scope=_scope(
                identity=identity,
                project_id=project_id,
                connection_id=connection_id,
                operation="create",
                idempotency_key=idempotency_key,
            ),
            payload_hash=_payload_hash(payload),
            payload=payload,
            target_key=None,
            project_id=project_id,
            connection_version=_required_connection_version(
                connection_version, persisted=connection_persisted
            ),
            fields_status="skipped",
            comment_status="skipped",
        )
        return await self._run(repository, row, adapter)

    async def enrich(
        self,
        *,
        adapter: Any,
        key: str,
        enrichment: Enrichment,
        identity: ConsumerIdentity,
        project_id: str | None,
        connection_id: str,
        connection_persisted: bool,
        connection_version: datetime | None,
        idempotency_key: str,
    ) -> WorkItem:
        key = _validated_target_key(key)
        enrichment = _validated_durable_enrichment(enrichment)
        if not enrichment.fields and not enrichment.comment:
            raise ValueError("work-item enrichment must include fields or a comment")
        # Fail before persisting an intent that the reconciler could never run.
        _require_idempotent_adapter(adapter)
        repository = self._repository_for(connection_persisted)
        payload = {
            "key": key,
            "enrichment": enrichment.model_dump(mode="json"),
        }
        row = await repository.reserve(
            scope=_scope(
                identity=identity,
                project_id=project_id,
                connection_id=connection_id,
                operation="enrich",
                idempotency_key=idempotency_key,
            ),
            payload_hash=_payload_hash(payload),
            payload=payload,
            target_key=key,
            project_id=project_id,
            connection_version=_required_connection_version(
                connection_version, persisted=connection_persisted
            ),
            fields_status="pending" if enrichment.fields else "skipped",
            comment_status="pending" if enrichment.comment else "skipped",
        )
        return await self._run(repository, row, adapter)

    async def replay_create(
        self,
        *,
        draft: WorkItemDraft,
        identity: ConsumerIdentity,
        project_id: str | None,
        connection_id: str,
        connection_persisted: bool,
        connection_version: datetime | None,
        idempotency_key: str,
    ) -> WorkItem | None:
        """Replay a terminal result without constructing a provider adapter."""

        draft = _validated_durable_draft(draft)
        payload = {"draft": draft.model_dump(mode="json")}
        return await self._inspect_replay(
            connection_persisted=connection_persisted,
            project_id=project_id,
            scope=_scope(
                identity=identity,
                project_id=project_id,
                connection_id=connection_id,
                operation="create",
                idempotency_key=idempotency_key,
            ),
            payload=payload,
            connection_version=connection_version,
        )

    async def replay_enrich(
        self,
        *,
        key: str,
        enrichment: Enrichment,
        identity: ConsumerIdentity,
        project_id: str | None,
        connection_id: str,
        connection_persisted: bool,
        connection_version: datetime | None,
        idempotency_key: str,
    ) -> WorkItem | None:
        """Replay a terminal enrichment without constructing a provider adapter."""

        key = _validated_target_key(key)
        enrichment = _validated_durable_enrichment(enrichment)
        if not enrichment.fields and not enrichment.comment:
            raise ValueError("work-item enrichment must include fields or a comment")
        payload = {"key": key, "enrichment": enrichment.model_dump(mode="json")}
        return await self._inspect_replay(
            connection_persisted=connection_persisted,
            project_id=project_id,
            scope=_scope(
                identity=identity,
                project_id=project_id,
                connection_id=connection_id,
                operation="enrich",
                idempotency_key=idempotency_key,
            ),
            payload=payload,
            connection_version=connection_version,
        )

    async def _inspect_replay(
        self,
        *,
        connection_persisted: bool,
        project_id: str | None,
        scope: MutationScope,
        payload: dict[str, Any],
        connection_version: datetime | None,
    ) -> WorkItem | None:
        repository = self._repository_for(connection_persisted)
        row = await repository.inspect(
            scope=scope,
            payload_hash=_payload_hash(payload),
            payload=payload,
            project_id=project_id,
            connection_version=_required_connection_version(
                connection_version, persisted=connection_persisted
            ),
        )
        if row is None or row.status not in {"completed", "failed"}:
            return None
        if row.status == "failed":
            _raise_stored_failure(row)
        return _stored_result(row)

    async def resume(self, mutation_id: str, adapter: Any) -> WorkItem:
        """Resume one persisted row, used by the startup reconciler."""

        row = await self._repository.get(mutation_id)
        if row is None:
            raise RuntimeError("work-item mutation disappeared before reconciliation")
        return await self._run(self._repository, row, adapter, foreground=False)

    async def ready_ids(self) -> list[str]:
        return await self._repository.ready_ids()

    async def retire_terminal(self) -> int:
        return await self._repository.retire_terminal_before(
            datetime.now(UTC) - TERMINAL_REPLAY_RETENTION
        )

    async def defer_resolution(self, mutation_id: str, error: str) -> None:
        row = await self._repository.get(mutation_id)
        if row is None:
            return
        await self._repository.defer_resolution(
            mutation_id,
            error=error,
            retry_delay=_retry_delay(row.id, int(row.attempt_count or 0) + 1),
        )

    def _repository_for(self, connection_persisted: bool) -> Any:
        if connection_persisted:
            return self._repository
        if get_settings().is_locked_down:
            raise RuntimeError(
                "static work-tracking connections cannot execute mutations in locked mode"
            )
        # Local development has no persisted connection row for the FK-backed
        # durable table. Keep deterministic retry behavior within this process
        # without weakening the production connection lifecycle constraint.
        return self._ephemeral_repository

    async def _run(
        self,
        repository: Any,
        row: WorkItemMutation,
        adapter: Any,
        *,
        foreground: bool = True,
    ) -> WorkItem:
        if row.status == "completed":
            return _stored_result(row)
        if row.status == "failed":
            _raise_stored_failure(row)
        _require_idempotent_adapter(adapter)

        claimed = await repository.claim(
            row.id,
            ignore_backoff=foreground and int(row.attempt_count or 0) == 0,
        )
        if claimed.status == "completed":
            return _stored_result(claimed)
        if claimed.status == "failed":
            _raise_stored_failure(claimed)
        claim_token = claimed.claim_token
        if not claim_token:
            raise RuntimeError("claimed work-item mutation has no lease token")

        async def execute() -> WorkItem:
            if claimed.operation == "create":
                return await self._execute_create(repository, claimed, adapter, claim_token)
            if claimed.operation == "enrich":
                return await self._execute_enrich(repository, claimed, adapter, claim_token)
            raise RuntimeError(f"unknown work-item mutation operation {claimed.operation!r}")

        permanent_error_kind: str | None = None
        try:
            return await _run_with_lease_heartbeat(
                repository,
                claimed.id,
                claim_token,
                execute(),
            )
        except asyncio.CancelledError:
            await _release_after_interruption(
                repository,
                claimed.id,
                claim_token,
                "CancelledError",
                attempt_count=int(claimed.attempt_count or 0),
            )
            raise
        except _PermanentMutationFailure as exc:
            await repository.fail(
                claimed.id,
                claim_token=claim_token,
                error_kind=exc.error_kind,
                error=(
                    "WorkTrackingMutationTargetNotFoundError"
                    if exc.error_kind == "not_found"
                    else "WorkTrackingMutationRejectedError"
                ),
            )
            permanent_error_kind = exc.error_kind
        except BaseException as exc:
            await _release_after_interruption(
                repository,
                claimed.id,
                claim_token,
                safe_type_name(exc),
                attempt_count=int(claimed.attempt_count or 0),
            )
            raise
        if permanent_error_kind == "not_found":
            raise WorkTrackingMutationTargetNotFoundError(
                "work tracker rejected the mutation target"
            )
        if permanent_error_kind is not None:
            raise WorkTrackingMutationRejectedError("work tracker rejected the mutation")
        raise RuntimeError("work-item mutation ended without a result")  # pragma: no cover

    async def _execute_create(
        self,
        repository: Any,
        row: WorkItemMutation,
        adapter: IdempotentWorkTrackingMutationPort,
        claim_token: str,
    ) -> WorkItem:
        item = await adapter.find_item_by_idempotency_marker(row.provider_marker)
        if item is None:
            if row.provider_attempted_at is not None:
                # Jira search is explicitly not read-after-write consistent. Once
                # a POST may have reached the provider, only reconciliation may
                # complete this row; automatically issuing a second POST is unsafe.
                raise WorkItemMutationOutcomeAmbiguousError(row, operation="create")
            draft = None
            try:
                draft = WorkItemDraft.model_validate(row.payload["draft"])
            except ValueError:
                pass
            if draft is None:
                raise _PermanentMutationFailure("rejected")
            await _run_optional_validator(
                adapter,
                "validate_create_item_idempotent",
                draft,
                marker=row.provider_marker,
            )
            row = await repository.mark_provider_attempted(
                row.id,
                claim_token=claim_token,
            )
            permanent_error_kind = None
            ambiguous = False
            try:
                item = await adapter.create_item_idempotent(
                    draft,
                    marker=row.provider_marker,
                )
            except WorkTrackingMutationRejectedError:
                permanent_error_kind = "rejected"
            except asyncio.CancelledError:
                raise
            except BaseException:
                ambiguous = True
            if permanent_error_kind is not None:
                raise _PermanentMutationFailure(permanent_error_kind)
            if ambiguous:
                raise WorkItemMutationOutcomeAmbiguousError(row, operation="create")
        if item is None:
            raise RuntimeError("work tracker returned no created item")
        return await self._complete(repository, row.id, claim_token, item)

    async def _execute_enrich(
        self,
        repository: Any,
        row: WorkItemMutation,
        adapter: IdempotentWorkTrackingMutationPort,
        claim_token: str,
    ) -> WorkItem:
        key = row.target_key
        if not key:
            raise RuntimeError("enrichment mutation is missing its target key")
        enrichment = None
        try:
            enrichment = Enrichment.model_validate(row.payload["enrichment"])
        except ValueError:
            pass
        if enrichment is None:
            raise _PermanentMutationFailure("rejected")

        if row.fields_status != "completed" and row.fields_status != "skipped":
            # Jira issue edits and ADO JSON-patch add operations are exact
            # set/upsert writes. Reissuing the same canonical field payload is
            # safe when a response or step-state commit acknowledgement is lost.
            await _run_optional_validator(
                adapter,
                "validate_update_item_fields_idempotent",
                enrichment.fields,
            )
            permanent_error_kind = None
            try:
                await adapter.update_item_fields_idempotent(key, enrichment.fields)
            except WorkTrackingMutationTargetNotFoundError:
                permanent_error_kind = "not_found"
            except WorkTrackingMutationRejectedError:
                permanent_error_kind = "rejected"
            if permanent_error_kind is not None:
                raise _PermanentMutationFailure(permanent_error_kind)
            row = await repository.mark_step(
                row.id,
                claim_token=claim_token,
                step="fields",
            )

        if row.comment_status != "completed" and row.comment_status != "skipped":
            permanent_error_kind = None
            try:
                has_marker = await adapter.has_comment_idempotency_marker(key, row.provider_marker)
            except WorkTrackingMutationTargetNotFoundError:
                permanent_error_kind = "not_found"
                has_marker = False
            except WorkTrackingMutationRejectedError:
                permanent_error_kind = "rejected"
                has_marker = False
            if permanent_error_kind is not None:
                raise _PermanentMutationFailure(permanent_error_kind)
            if not has_marker:
                if row.comment_attempted_at is not None:
                    raise WorkItemMutationOutcomeAmbiguousError(row, operation="comment")
                if enrichment.comment is None:
                    raise RuntimeError("comment step has no durable comment payload")
                # The marker scan can be long enough for a different replica to
                # reclaim a stale lease. This durable transition both fences
                # ownership and ensures an eventually-consistent marker read can
                # never cause a second non-idempotent POST.
                row = await repository.mark_comment_attempted(
                    row.id,
                    claim_token=claim_token,
                )
                permanent_error_kind = None
                ambiguous = False
                try:
                    await adapter.add_item_comment_idempotent(
                        key,
                        enrichment.comment,
                        marker=row.provider_marker,
                    )
                except WorkTrackingMutationTargetNotFoundError:
                    permanent_error_kind = "not_found"
                except WorkTrackingMutationRejectedError:
                    permanent_error_kind = "rejected"
                except asyncio.CancelledError:
                    raise
                except BaseException:
                    ambiguous = True
                if permanent_error_kind is not None:
                    raise _PermanentMutationFailure(permanent_error_kind)
                if ambiguous:
                    raise WorkItemMutationOutcomeAmbiguousError(
                        row,
                        operation="comment",
                    )
            row = await repository.mark_step(
                row.id,
                claim_token=claim_token,
                step="comment",
            )

        # This read follows provider writes. Any failure, including malformed
        # provider data, remains retryable so the completed writes are reconciled.
        item = validated_provider_work_item(await adapter.get_item(key))
        if item.key != key:
            raise RuntimeError("work tracker returned a mismatched enrichment target")
        return await self._complete(repository, row.id, claim_token, item)

    async def _complete(
        self,
        repository: Any,
        mutation_id: str,
        claim_token: str,
        item: WorkItem,
    ) -> WorkItem:
        result = _durable_work_item_result(item)
        completed = await repository.complete(
            mutation_id,
            claim_token=claim_token,
            result=result,
        )
        return _stored_result(completed)


class _EphemeralMutationRepository:
    """Process-local development fallback for non-persisted static adapters."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._by_id: dict[str, WorkItemMutation] = {}
        self._by_scope: dict[tuple[str, str, str, str, str], str] = {}

    async def reserve(self, **values: Any) -> WorkItemMutation:
        scope: MutationScope = values["scope"]
        scope_key = _scope_key(scope)
        with self._lock:
            existing_id = self._by_scope.get(scope_key)
            if existing_id is not None:
                row = self._by_id[existing_id]
                if row.payload_hash != values["payload_hash"] or row.payload != values["payload"]:
                    raise MutationPayloadConflictError(
                        "idempotency key is already bound to a different work-item mutation payload"
                    )
                if row.project_id != values["project_id"]:
                    raise MutationConnectionChangedError(
                        "work-item mutation project binding changed"
                    )
                return row
            row_id = uuid4().hex
            row = WorkItemMutation(
                id=row_id,
                tenant_scope=scope.tenant_scope,
                consumer_id=scope.consumer_id,
                connection_id=scope.connection_id,
                connection_version=values["connection_version"],
                operation=scope.operation,
                idempotency_key=scope.idempotency_key,
                payload_hash=values["payload_hash"],
                payload=values["payload"],
                target_key=values["target_key"],
                project_id=values["project_id"],
                provider_marker=f"apex-idem-{row_id}",
                status="pending",
                fields_status=values["fields_status"],
                comment_status=values["comment_status"],
                attempt_count=0,
            )
            self._by_id[row_id] = row
            self._by_scope[scope_key] = row_id
            return row

    async def get(self, mutation_id: str) -> WorkItemMutation | None:
        with self._lock:
            return self._by_id.get(mutation_id)

    async def inspect(self, **values: Any) -> WorkItemMutation | None:
        scope: MutationScope = values["scope"]
        with self._lock:
            existing_id = self._by_scope.get(_scope_key(scope))
            if existing_id is None:
                return None
            row = self._by_id[existing_id]
            if row.payload_hash != values["payload_hash"] or row.payload != values["payload"]:
                raise MutationPayloadConflictError(
                    "idempotency key is already bound to a different work-item mutation payload"
                )
            if row.project_id != values["project_id"]:
                raise MutationConnectionChangedError("work-item mutation project binding changed")
            return row

    async def claim(self, mutation_id: str, *, ignore_backoff: bool) -> WorkItemMutation:
        now = datetime.now(UTC)
        with self._lock:
            row = self._by_id[mutation_id]
            if row.status in {"completed", "failed"}:
                return row
            if row.status == "running" and row.claimed_at is not None:
                claimed_at = row.claimed_at
                if claimed_at.tzinfo is None:
                    claimed_at = claimed_at.replace(tzinfo=UTC)
                if claimed_at > now - MUTATION_LEASE:
                    raise MutationClaimedError("work-item mutation is already in progress")
            if not ignore_backoff and row.next_attempt_at is not None and row.next_attempt_at > now:
                raise MutationClaimedError("work-item mutation retry is not due yet")
            row.status = "running"
            row.claim_token = uuid4().hex
            row.claimed_at = now
            row.next_attempt_at = None
            row.attempt_count = int(row.attempt_count or 0) + 1
            return row

    async def mark_step(self, mutation_id: str, *, claim_token: str, step: str) -> WorkItemMutation:
        with self._lock:
            row = self._owned(mutation_id, claim_token)
            setattr(row, f"{step}_status", "completed")
            return row

    async def mark_provider_attempted(
        self, mutation_id: str, *, claim_token: str
    ) -> WorkItemMutation:
        with self._lock:
            row = self._owned(mutation_id, claim_token)
            if row.provider_attempted_at is None:
                row.provider_attempted_at = datetime.now(UTC)
            return row

    async def mark_comment_attempted(
        self, mutation_id: str, *, claim_token: str
    ) -> WorkItemMutation:
        with self._lock:
            row = self._owned(mutation_id, claim_token)
            if row.comment_attempted_at is None:
                row.comment_attempted_at = datetime.now(UTC)
            return row

    async def renew(self, mutation_id: str, *, claim_token: str) -> WorkItemMutation:
        with self._lock:
            row = self._owned(mutation_id, claim_token)
            row.claimed_at = datetime.now(UTC)
            return row

    async def complete(
        self,
        mutation_id: str,
        *,
        claim_token: str,
        result: dict[str, Any],
    ) -> WorkItemMutation:
        with self._lock:
            row = self._owned(mutation_id, claim_token)
            row.status = "completed"
            row.result = result
            row.claim_token = None
            row.claimed_at = None
            return row

    async def fail(
        self,
        mutation_id: str,
        *,
        claim_token: str,
        error_kind: str,
        error: str,
    ) -> WorkItemMutation:
        with self._lock:
            row = self._owned(mutation_id, claim_token)
            row.status = "failed"
            row.terminal_error = error_kind
            row.last_error = _safe_error_text(error)
            row.claim_token = None
            row.claimed_at = None
            return row

    async def release(
        self,
        mutation_id: str,
        *,
        claim_token: str,
        error: str,
        retry_delay: timedelta,
    ) -> WorkItemMutation:
        with self._lock:
            row = self._by_id[mutation_id]
            if row.status == "running" and row.claim_token == claim_token:
                row.status = "pending"
                row.claim_token = None
                row.claimed_at = None
                row.next_attempt_at = datetime.now(UTC) + retry_delay
                row.last_error = _safe_error_text(error)
            return row

    async def defer_resolution(
        self,
        mutation_id: str,
        *,
        error: str,
        retry_delay: timedelta,
    ) -> WorkItemMutation:
        with self._lock:
            row = self._by_id[mutation_id]
            if row.status in {"completed", "failed"}:
                return row
            row.status = "pending"
            row.claim_token = None
            row.claimed_at = None
            row.next_attempt_at = datetime.now(UTC) + retry_delay
            row.attempt_count = int(row.attempt_count or 0) + 1
            row.last_error = _safe_error_text(error)
            return row

    def _owned(self, mutation_id: str, claim_token: str) -> WorkItemMutation:
        row = self._by_id[mutation_id]
        if row.status != "running" or row.claim_token != claim_token:
            raise MutationClaimedError("work-item mutation lease was lost")
        return row


def _scope(
    *,
    identity: ConsumerIdentity,
    project_id: str | None,
    connection_id: str,
    operation: str,
    idempotency_key: str,
) -> MutationScope:
    key = _validated_mutation_scope_text(
        idempotency_key,
        label="Idempotency-Key",
        max_chars=255,
    )
    safe_connection_id = _validated_mutation_scope_text(
        connection_id,
        label="resolved work-tracking connection id",
        max_chars=32,
    )
    consumer_id = _validated_mutation_scope_text(
        identity.consumer_id,
        label="consumer id",
        max_chars=255,
    )
    safe_project_id = (
        _validated_mutation_scope_text(project_id, label="project id", max_chars=255)
        if project_id is not None
        else None
    )
    if type(operation) is not str or operation not in {"create", "enrich"}:
        raise ValueError("work-item mutation operation is invalid")
    allowed = identity.role.at_least(Role.OPERATOR) and (
        identity.is_unscoped
        or (
            safe_project_id is not None
            and any(
                scope.project_id == safe_project_id and scope.app_id is None
                for scope in identity.scopes
            )
        )
    )
    if not allowed:
        raise PermissionError("work-item mutations require project-wide operator scope")

    # Bind the key to the stable provider target, not the caller's current
    # authorization breadth. An administrator may become project-scoped (or the
    # reverse) between an ambiguous response and its retry; that must not create
    # a second provider mutation for the same consumer, connection, and project.
    tenant = {"project_id": safe_project_id}
    return MutationScope(
        tenant_scope=hashlib.sha256(_canonical_json(tenant)).hexdigest(),
        consumer_id=consumer_id,
        connection_id=safe_connection_id,
        operation=operation,
        idempotency_key=key,
    )


def _validated_mutation_scope_text(value: Any, *, label: str, max_chars: int) -> str:
    """Validate an identity/idempotency component before normalization or scanning."""

    if (
        type(value) is not str
        or not 1 <= len(value) <= max_chars
        or value != value.strip()
        or any(ord(character) < 0x20 or ord(character) == 0x7F for character in value)
    ):
        raise ValueError(f"{label} must contain 1-{max_chars} canonical characters")
    if contains_credential_material(value):
        raise ValueError(f"{label} must not contain credential material")
    return value


def _required_connection_version(value: datetime | None, *, persisted: bool) -> datetime:
    if value is not None:
        return value
    if persisted:
        raise MutationConnectionChangedError(
            "persisted work-tracking connection has no immutable version"
        )
    return datetime.now(UTC)


def _validated_durable_draft(draft: WorkItemDraft) -> WorkItemDraft:
    """Revalidate even a model constructed without Pydantic validation."""

    validated: WorkItemDraft | None = None
    try:
        raw = draft.model_dump(mode="json", warnings="error")
        validated = WorkItemDraft.model_validate(raw)
    except Exception:
        pass
    if validated is None:
        raise ValueError("work item draft is invalid or contains credential material")
    return validated


def _validated_durable_enrichment(enrichment: Enrichment) -> Enrichment:
    """Revalidate before any enrichment value can enter durable JSONB."""

    validated: Enrichment | None = None
    try:
        raw = enrichment.model_dump(mode="json", warnings="error")
        validated = Enrichment.model_validate(raw)
    except Exception:
        pass
    if validated is None:
        raise ValueError("work item enrichment is invalid or contains credential material")
    return validated


def _validated_target_key(key: str) -> str:
    if (
        type(key) is not str
        or _WORK_ITEM_TARGET_KEY.fullmatch(key) is None
        or contains_credential_material(key)
    ):
        raise ValueError("work-item key must be a safe identifier of at most 255 characters")
    return key


def _as_utc_datetime(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def _payload_hash(payload: dict[str, Any]) -> str:
    return hashlib.sha256(_canonical_json(payload)).hexdigest()


def _canonical_json(value: Any) -> bytes:
    encoded: bytes | None = None
    try:
        encoded = json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError):
        pass
    if encoded is None:
        raise ValueError("work-item mutation payload must be canonical JSON")
    return encoded


def _scope_key(scope: MutationScope) -> tuple[str, str, str, str, str]:
    return (
        scope.tenant_scope,
        scope.consumer_id,
        scope.connection_id,
        scope.operation,
        scope.idempotency_key,
    )


def _require_idempotent_adapter(adapter: Any) -> None:
    if not isinstance(adapter, IdempotentWorkTrackingMutationPort):
        raise RuntimeError(
            "resolved work-tracking adapter does not implement durable mutation reconciliation"
        )


def _stored_result(row: WorkItemMutation) -> WorkItem:
    if row.status != "completed" or row.result is None:
        raise RuntimeError("completed work-item mutation has no durable result")
    result: WorkItem | None = None
    try:
        result = validated_provider_work_item(row.result)
    except Exception:
        # Legacy or corrupted rows must fail closed instead of replaying secrets.
        pass
    if result is None:
        raise RuntimeError("completed work-item mutation has an invalid durable result")
    if row.operation == "enrich" and result.key != row.target_key:
        raise RuntimeError("completed enrichment result does not match its target")
    return result


def _durable_work_item_result(item: WorkItem) -> dict[str, Any]:
    """Normalize provider output before PostgreSQL JSONB can persist it."""

    return validated_provider_work_item(item).model_dump(mode="json")


def _safe_error_text(error: str) -> str:
    return sanitize_durable_text(error, 4096) or ""


def _raise_stored_failure(row: WorkItemMutation) -> None:
    if row.terminal_error == "not_found":
        raise KeyError("work tracker rejected the previously attempted target")
    if row.terminal_error == "rejected":
        raise ValueError("work tracker rejected the previously attempted mutation")
    raise RuntimeError("work tracker mutation previously failed")


async def _release_after_interruption(
    repository: Any,
    mutation_id: str,
    claim_token: str,
    error: str,
    *,
    attempt_count: int,
) -> None:
    """Best-effort release; a lost lease is safely reclaimed after expiry."""

    release = asyncio.create_task(
        repository.release(
            mutation_id,
            claim_token=claim_token,
            error=error,
            retry_delay=_retry_delay(mutation_id, attempt_count),
        ),
        name=f"work-item-mutation-release-{mutation_id}",
    )
    interrupted = False
    while not release.done():
        try:
            await asyncio.shield(release)
        except asyncio.CancelledError:
            # Repeated caller cancellation must not detach a transaction that
            # still owns a connection or row lock. Finish it, then restore the
            # caller's cancellation outcome.
            interrupted = True
            continue
        except BaseException:
            break

    release_error: BaseException | None = None
    try:
        release.result()
    except BaseException as exc:
        release_error = exc
    if isinstance(release_error, Exception):
        logger.warning(
            "work_item_mutations.release_failed",
            mutation_id=mutation_id,
            error_type=safe_type_name(release_error),
        )
    if interrupted:
        raise asyncio.CancelledError from None
    if isinstance(release_error, asyncio.CancelledError):
        raise release_error
    if release_error is not None and not isinstance(release_error, Exception):
        raise release_error


async def _run_optional_validator(
    adapter: Any,
    name: str,
    *args: Any,
    **kwargs: Any,
) -> None:
    validator = getattr(adapter, name, None)
    if not callable(validator):
        return
    permanent_error_kind: str | None = None
    try:
        result = validator(*args, **kwargs)
        if isawaitable(result):
            await result
    except WorkTrackingMutationTargetNotFoundError:
        permanent_error_kind = "not_found"
    except (WorkTrackingMutationRejectedError, ValueError):
        permanent_error_kind = "rejected"
    if permanent_error_kind is not None:
        raise _PermanentMutationFailure(permanent_error_kind)


async def _run_with_lease_heartbeat(
    repository: Any,
    mutation_id: str,
    claim_token: str,
    operation: Coroutine[Any, Any, WorkItem],
) -> WorkItem:
    async def heartbeat() -> None:
        while True:
            await asyncio.sleep(max(_LEASE_HEARTBEAT_S, 0.001))
            await repository.renew(mutation_id, claim_token=claim_token)

    operation_task = asyncio.create_task(operation)
    heartbeat_task = asyncio.create_task(heartbeat())
    try:
        done, _pending = await asyncio.wait(
            {operation_task, heartbeat_task},
            return_when=asyncio.FIRST_COMPLETED,
        )
        if operation_task in done:
            return await operation_task
        operation_task.cancel()
        await asyncio.gather(operation_task, return_exceptions=True)
        await heartbeat_task
        raise RuntimeError("work-item mutation lease heartbeat stopped unexpectedly")
    finally:
        await _cancel_tasks_definitively(operation_task, heartbeat_task)


async def _cancel_tasks_definitively(*tasks: asyncio.Task[Any]) -> None:
    """Cancel owned tasks without detaching their provider/resource cleanup."""

    for task in tasks:
        if not task.done():
            task.cancel()
    interrupted = False
    current = asyncio.current_task()
    for task in tasks:
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                # `shield` also raises when the owned task reaches its expected
                # cancelled state. Distinguish that from another cancellation
                # request directed at this coordinator task.
                if current is not None and current.cancelling():
                    interrupted = True
                if task.done():
                    break
                continue
            except BaseException:
                break
        if task.done():
            try:
                task.result()
            except BaseException:
                pass
    if interrupted:
        raise asyncio.CancelledError from None


def _retry_delay(mutation_id: str, attempt_count: int) -> timedelta:
    base_seconds = max(_RETRY_DELAY.total_seconds(), 0.0)
    if base_seconds == 0:
        return timedelta(0)
    exponent = min(max(attempt_count - 1, 0), 10)
    ceiling = _MAX_RETRY_DELAY.total_seconds()
    unjittered = min(base_seconds * (2**exponent), ceiling)
    digest = hashlib.sha256(f"{mutation_id}:{attempt_count}".encode()).digest()
    jitter = 0.75 + int.from_bytes(digest[:2], "big") / 131_070
    return timedelta(seconds=min(unjittered * jitter, ceiling))


@lru_cache
def get_work_item_mutation_service() -> WorkItemMutationService:
    return WorkItemMutationService(WorkItemMutationsRepository(get_sessionmaker()))


async def reconcile_work_item_mutations_once(
    *,
    service: WorkItemMutationService | None = None,
    resolver: ConnectionResolver | None = None,
    heartbeat: Callable[[], None] | None = None,
) -> int:
    service = service or get_work_item_mutation_service()
    resolver = resolver or get_connection_resolver()
    reconciled = 0
    if heartbeat is not None:
        heartbeat()
    for mutation_id in await service.ready_ids():
        if heartbeat is not None:
            heartbeat()
        row = await service._repository.get(mutation_id)
        if row is None:
            continue
        resolved = None
        try:
            try:
                resolved = await resolver.resolve_with_metadata(
                    PortKind.WORK_TRACKING,
                    connection_id=row.connection_id,
                    project_id=row.project_id,
                )
                if not resolved.persisted:
                    raise RuntimeError("durable mutation resolved to a non-persisted connection")
                if resolved.connection_version is None or _as_utc_datetime(
                    resolved.connection_version
                ) != _as_utc_datetime(row.connection_version):
                    raise MutationConnectionChangedError(
                        "work-tracking connection version no longer matches the durable mutation"
                    )
            except asyncio.CancelledError:
                raise
            except MutationClaimedError:
                continue
            except Exception as exc:
                try:
                    await service.defer_resolution(
                        mutation_id,
                        safe_type_name(exc),
                    )
                except asyncio.CancelledError:
                    raise
                except MutationClaimedError:
                    continue
                except Exception as defer_exc:
                    logger.warning(
                        "work_item_mutations.resolution_defer_failed",
                        mutation_id=mutation_id,
                        error_type=safe_type_name(defer_exc),
                    )
                logger.warning(
                    "work_item_mutations.connection_resolution_failed",
                    mutation_id=mutation_id,
                    error_type=safe_type_name(exc),
                )
                continue
            try:
                await service.resume(mutation_id, resolved.adapter)
                reconciled += 1
            except asyncio.CancelledError:
                raise
            except MutationClaimedError:
                continue
            except Exception as exc:
                logger.warning(
                    "work_item_mutations.reconcile_failed",
                    mutation_id=mutation_id,
                    error_type=safe_type_name(exc),
                )
        finally:
            if resolved is not None:
                await close_adapter(resolved.adapter)
    try:
        await service.retire_terminal()
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        logger.warning(
            "work_item_mutations.retirement_failed",
            error_type=safe_type_name(exc),
        )
    return reconciled


async def run_work_item_mutation_reconciler(
    stop: asyncio.Event,
    heartbeat: Callable[[], None] | None = None,
) -> None:
    while not stop.is_set():
        try:
            await reconcile_work_item_mutations_once(heartbeat=heartbeat)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning(
                "work_item_mutations.reconciler_failed",
                error_type=safe_type_name(exc),
            )
        if heartbeat is not None:
            heartbeat()
        try:
            await asyncio.wait_for(stop.wait(), timeout=RECONCILE_INTERVAL_S)
        except TimeoutError:
            pass

"""Database-backed durability tests for the work-item mutation repository."""

import asyncio
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, cast

import pytest
from sqlalchemy import Table, create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from apex.domain.integrations import WorkItem
from apex.persistence.models import (
    Connection,
    HostMapping,
    WorkItemMutation,
    WorkItemMutationTombstone,
)
from apex.persistence.repositories import work_item_mutations as mutation_module
from apex.persistence.repositories.work_item_mutations import (
    MUTATION_LEASE,
    MutationClaimedError,
    MutationConnectionChangedError,
    MutationPayloadConflictError,
    MutationRetiredError,
    MutationScope,
    WorkItemMutationsRepository,
    canonical_mutation_payload_hash,
    mutation_scope_hash,
    mutation_tenant_scope,
    validate_mutation_reservation,
)


class _AsyncSessionFacade:
    """Small async facade that can inject one ambiguous commit outcome."""

    def __init__(self, factory: "_SessionFactory") -> None:
        self._factory = factory
        self._session = Session(factory.engine, expire_on_commit=False)

    async def __aenter__(self) -> "_AsyncSessionFacade":
        return self

    async def __aexit__(self, *_args: Any) -> None:
        self._session.close()

    def add(self, row: Any) -> None:
        self._session.add(row)

    async def get(self, model: Any, key: Any) -> Any:
        return self._session.get(model, key)

    async def scalar(self, statement: Any) -> Any:
        return self._session.scalar(statement)

    async def scalars(self, statement: Any) -> Any:
        return self._session.scalars(statement)

    async def delete(self, row: Any) -> None:
        self._session.delete(row)

    async def commit(self) -> None:
        mode, self._factory.commit_mode = self._factory.commit_mode, None
        if mode == "cancel":
            raise asyncio.CancelledError
        if mode == "before":
            raise RuntimeError("database rejected commit")
        self._session.commit()
        if mode == "after":
            raise RuntimeError("database committed, then disconnected")

    async def rollback(self) -> None:
        self._session.rollback()


class _SessionFactory:
    def __init__(self, engine: Engine) -> None:
        self.engine = engine
        self.commit_mode: str | None = None

    def __call__(self) -> _AsyncSessionFacade:
        return _AsyncSessionFacade(self)


@dataclass
class _Harness:
    engine: Engine
    factory: _SessionFactory
    repository: WorkItemMutationsRepository
    connection_version: datetime


@pytest.fixture
def harness() -> Iterator[_Harness]:
    engine = create_engine("sqlite://", poolclass=StaticPool)

    @event.listens_for(engine, "connect")
    def attach_apex(dbapi_connection: Any, _record: Any) -> None:
        dbapi_connection.execute("ATTACH DATABASE ':memory:' AS apex")
        dbapi_connection.execute("PRAGMA foreign_keys=ON")

    with engine.begin() as connection:
        cast(Table, Connection.__table__).create(connection)
        cast(Table, HostMapping.__table__).create(connection)
        cast(Table, WorkItemMutation.__table__).create(connection)
        cast(Table, WorkItemMutationTombstone.__table__).create(connection)

    version = datetime.now(UTC)
    with Session(engine) as session:
        session.add(
            Connection(
                id="connection-1",
                kind="work_tracking",
                provider="stub",
                name="repository-paths",
                enabled=True,
                runtime_version=version,
            )
        )
        session.commit()

    factory = _SessionFactory(engine)
    yield _Harness(
        engine=engine,
        factory=factory,
        repository=WorkItemMutationsRepository(factory),  # type: ignore[arg-type]
        connection_version=version,
    )
    engine.dispose()


def _scope(key: str, *, connection_id: str = "connection-1") -> MutationScope:
    return MutationScope(
        tenant_scope=mutation_tenant_scope("project-1"),
        consumer_id="consumer-1",
        connection_id=connection_id,
        operation="create",
        idempotency_key=key,
    )


def _payload(key: str) -> dict[str, Any]:
    return {
        "draft": {
            "title": key,
            "kind": "story",
            "description": "",
            "fields": {},
        }
    }


async def _reserve(
    harness: _Harness,
    key: str,
    *,
    fields_status: str = "skipped",
    comment_status: str = "skipped",
) -> WorkItemMutation:
    payload = _payload(key)
    return await harness.repository.reserve(
        scope=_scope(key),
        payload_hash=canonical_mutation_payload_hash(payload),
        payload=payload,
        target_key=None,
        project_id="project-1",
        connection_version=harness.connection_version,
        fields_status=fields_status,
        comment_status=comment_status,
    )


def _set_row(harness: _Harness, mutation_id: str, **values: Any) -> None:
    with Session(harness.engine, expire_on_commit=False) as session:
        row = session.get(WorkItemMutation, mutation_id)
        assert row is not None
        for name, value in values.items():
            setattr(row, name, value)
        session.commit()


class _NoIoFactory:
    def __init__(self) -> None:
        self.called = False

    def __call__(self) -> Any:
        self.called = True
        raise AssertionError("invalid mutation input must be rejected before repository I/O")


def _reservation_values(key: str = "direct-writer") -> dict[str, Any]:
    payload = _payload(key)
    return {
        "scope": _scope(key),
        "payload_hash": canonical_mutation_payload_hash(payload),
        "payload": payload,
        "target_key": None,
        "project_id": "project-1",
        "connection_version": datetime.now(UTC),
        "fields_status": "skipped",
        "comment_status": "skipped",
    }


async def test_reserve_rejects_inconsistent_or_credential_bearing_rows_before_io() -> None:
    credential = "ghp_0123456789abcdefghijklmnopqrstuvwxyz"
    credential_payload = _payload("credential")
    credential_payload["draft"]["title"] = credential
    invalid_rows: list[dict[str, Any]] = [
        {
            "scope": MutationScope(
                tenant_scope="a" * 64,
                consumer_id="consumer-1",
                connection_id="connection-1",
                operation="create",
                idempotency_key="direct-writer",
            )
        },
        {
            "scope": MutationScope(
                tenant_scope=mutation_tenant_scope("project-1"),
                consumer_id=credential,
                connection_id="connection-1",
                operation="create",
                idempotency_key="direct-writer",
            )
        },
        {"payload_hash": "a" * 64},
        {
            "payload": credential_payload,
            "payload_hash": canonical_mutation_payload_hash(credential_payload),
        },
        {"target_key": "PHX-1"},
        {"fields_status": "pending"},
        {"comment_status": "pending"},
        {"connection_version": datetime.now()},
    ]

    for changes in invalid_rows:
        values = _reservation_values()
        values.update(changes)
        factory = _NoIoFactory()
        repository = WorkItemMutationsRepository(cast(Any, factory))
        with pytest.raises(ValueError):
            await repository.reserve(**values)
        assert factory.called is False


def test_payload_validation_detaches_secret_bearing_model_error() -> None:
    credential = "ghp_0123456789abcdefghijklmnopqrstuvwxyz"
    payload = _payload("credential-context")
    payload["draft"]["title"] = credential
    values = _reservation_values("credential-context")
    values["payload"] = payload
    values["payload_hash"] = canonical_mutation_payload_hash(payload)

    with pytest.raises(ValueError) as exc_info:
        validate_mutation_reservation(**values)

    assert exc_info.value.__context__ is None
    assert exc_info.value.__cause__ is None
    assert credential not in repr(exc_info.value)


async def test_result_validation_detaches_secret_bearing_provider_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    credential = "provider-result-raw-secret-canary"

    def explode(_result: Any) -> WorkItem:
        raise RuntimeError(credential)

    monkeypatch.setattr(mutation_module, "validated_provider_work_item", explode)
    factory = _NoIoFactory()
    repository = WorkItemMutationsRepository(cast(Any, factory))
    result = WorkItem(key="PHX-1", title="safe").model_dump(mode="json")

    with pytest.raises(ValueError) as exc_info:
        await repository.complete("missing", claim_token="none", result=result)

    assert exc_info.value.__context__ is None
    assert exc_info.value.__cause__ is None
    assert credential not in repr(exc_info.value)
    assert factory.called is False


def test_reservation_validator_enforces_exact_enrichment_shape_and_steps() -> None:
    payload = {
        "key": "PHX-1",
        "enrichment": {
            "fields": {"status": "closed"},
            "comment": None,
        },
    }
    values = {
        "scope": MutationScope(
            tenant_scope=mutation_tenant_scope("project-1"),
            consumer_id="consumer-1",
            connection_id="connection-1",
            operation="enrich",
            idempotency_key="enrich-direct",
        ),
        "payload_hash": canonical_mutation_payload_hash(payload),
        "payload": payload,
        "target_key": "PHX-1",
        "project_id": "project-1",
        "connection_version": datetime.now(UTC),
        "fields_status": "pending",
        "comment_status": "skipped",
    }

    validate_mutation_reservation(**values)
    for field, invalid in (
        ("target_key", "PHX-2"),
        ("fields_status", "skipped"),
        ("comment_status", "pending"),
    ):
        changed = dict(values)
        changed[field] = invalid
        with pytest.raises(ValueError):
            validate_mutation_reservation(**changed)


async def test_inspect_and_complete_reject_unsafe_direct_values_before_io() -> None:
    factory = _NoIoFactory()
    repository = WorkItemMutationsRepository(cast(Any, factory))
    values = _reservation_values("inspect-direct")
    mismatched_scope = MutationScope(
        tenant_scope="a" * 64,
        consumer_id="consumer-1",
        connection_id="connection-1",
        operation="create",
        idempotency_key="inspect-direct",
    )
    with pytest.raises(ValueError, match="tenant scope"):
        await repository.inspect(
            scope=mismatched_scope,
            payload_hash=values["payload_hash"],
            payload=values["payload"],
            project_id=values["project_id"],
            connection_version=values["connection_version"],
        )

    credential = "ghp_0123456789abcdefghijklmnopqrstuvwxyz"
    result = WorkItem(key="PHX-1", title=credential).model_dump(mode="json")
    with pytest.raises(ValueError, match="canonical safe form"):
        await repository.complete("missing", claim_token="none", result=result)

    assert factory.called is False


async def test_reserve_and_inspect_enforce_durable_scope_identity(harness: _Harness) -> None:
    repository = harness.repository
    unused_payload = _payload("unused")
    assert (
        await repository.inspect(
            scope=_scope("unused"),
            payload_hash=canonical_mutation_payload_hash(unused_payload),
            payload=unused_payload,
            project_id="project-1",
            connection_version=harness.connection_version,
        )
        is None
    )

    row = await _reserve(harness, "identity")
    replay = await _reserve(harness, "identity")
    identity_payload = _payload("identity")
    inspected = await repository.inspect(
        scope=_scope("identity"),
        payload_hash=canonical_mutation_payload_hash(identity_payload),
        payload=identity_payload,
        project_id="project-1",
        connection_version=harness.connection_version,
    )
    assert replay.id == row.id
    assert inspected is not None and inspected.id == row.id

    different_payload = _payload("different")
    with pytest.raises(MutationPayloadConflictError):
        await repository.inspect(
            scope=_scope("identity"),
            payload_hash=canonical_mutation_payload_hash(different_payload),
            payload=different_payload,
            project_id="project-1",
            connection_version=harness.connection_version,
        )
    with pytest.raises(ValueError, match="tenant scope"):
        await repository.inspect(
            scope=_scope("identity"),
            payload_hash=canonical_mutation_payload_hash(identity_payload),
            payload=identity_payload,
            project_id="project-2",
            connection_version=harness.connection_version,
        )
    with pytest.raises(MutationConnectionChangedError, match="changed after"):
        await repository.inspect(
            scope=_scope("identity"),
            payload_hash=canonical_mutation_payload_hash(identity_payload),
            payload=identity_payload,
            project_id="project-1",
            connection_version=harness.connection_version + timedelta(seconds=1),
        )

    retired_scope = _scope("retired")
    retired_payload = _payload("retired")
    with Session(harness.engine) as session:
        session.add(
            WorkItemMutationTombstone(
                scope_hash=mutation_scope_hash(retired_scope),
                payload_hash=canonical_mutation_payload_hash(retired_payload),
                outcome="completed",
            )
        )
        session.commit()
    with pytest.raises(MutationRetiredError):
        await repository.inspect(
            scope=retired_scope,
            payload_hash=canonical_mutation_payload_hash(retired_payload),
            payload=retired_payload,
            project_id="project-1",
            connection_version=harness.connection_version,
        )
    with pytest.raises(MutationPayloadConflictError, match="retired payload"):
        await repository.inspect(
            scope=retired_scope,
            payload_hash=canonical_mutation_payload_hash(different_payload),
            payload=different_payload,
            project_id="project-1",
            connection_version=harness.connection_version,
        )


async def test_reserve_recovers_only_an_authoritative_commit(harness: _Harness) -> None:
    harness.factory.commit_mode = "after"
    committed = await _reserve(harness, "commit-after-disconnect")
    assert (await harness.repository.get(committed.id)) is not None

    harness.factory.commit_mode = "before"
    with pytest.raises(RuntimeError, match="rejected commit"):
        await _reserve(harness, "commit-rejected")
    assert await harness.repository.get_by_scope(_scope("commit-rejected")) is None

    harness.factory.commit_mode = "cancel"
    with pytest.raises(asyncio.CancelledError):
        await _reserve(harness, "commit-cancelled")
    assert await harness.repository.get_by_scope(_scope("commit-cancelled")) is None

    with Session(harness.engine) as session:
        connection = session.get(Connection, "connection-1")
        assert connection is not None
        connection.enabled = False
        session.commit()
    with pytest.raises(MutationConnectionChangedError, match="missing or disabled"):
        await _reserve(harness, "disabled")

    with Session(harness.engine) as session:
        connection = session.get(Connection, "connection-1")
        assert connection is not None
        connection.enabled = True
        connection.kind = "artifact_store"
        session.commit()
    with pytest.raises(MutationConnectionChangedError, match="missing or disabled"):
        await _reserve(harness, "wrong-kind")

    with pytest.raises(MutationConnectionChangedError, match="missing or disabled"):
        missing_payload = _payload("missing")
        await harness.repository.reserve(
            scope=_scope("missing", connection_id="missing-connection"),
            payload_hash=canonical_mutation_payload_hash(missing_payload),
            payload=missing_payload,
            target_key=None,
            project_id="project-1",
            connection_version=harness.connection_version,
            fields_status="skipped",
            comment_status="skipped",
        )


async def test_claim_enforces_terminal_lease_and_backoff_state(harness: _Harness) -> None:
    repository = harness.repository
    with pytest.raises(RuntimeError, match="disappeared before"):
        await repository.claim("missing", ignore_backoff=False)

    terminal = await _reserve(harness, "terminal")
    _set_row(harness, terminal.id, status="completed")
    assert (await repository.claim(terminal.id, ignore_backoff=False)).status == "completed"

    leased = await _reserve(harness, "leased")
    first = await repository.claim(leased.id, ignore_backoff=False)
    with pytest.raises(MutationClaimedError, match="already in progress"):
        await repository.claim(leased.id, ignore_backoff=True)
    _set_row(
        harness,
        leased.id,
        claimed_at=datetime.now(UTC) - MUTATION_LEASE - timedelta(seconds=1),
    )
    second = await repository.claim(leased.id, ignore_backoff=True)
    assert second.claim_token != first.claim_token
    assert second.attempt_count == 2

    backed_off = await _reserve(harness, "backed-off")
    _set_row(harness, backed_off.id, next_attempt_at=datetime.now(UTC) + timedelta(minutes=1))
    with pytest.raises(MutationClaimedError, match="not due"):
        await repository.claim(backed_off.id, ignore_backoff=False)


@pytest.mark.parametrize("mode", ["before", "cancel"])
async def test_claim_rolls_back_unacknowledged_commits(harness: _Harness, mode: str) -> None:
    row = await _reserve(harness, f"claim-{mode}")
    harness.factory.commit_mode = mode
    error = asyncio.CancelledError if mode == "cancel" else RuntimeError
    with pytest.raises(error):
        await harness.repository.claim(row.id, ignore_backoff=True)
    authoritative = await harness.repository.get(row.id)
    assert authoritative is not None and authoritative.status == "pending"


async def test_claim_recovers_a_commit_after_disconnect(harness: _Harness) -> None:
    row = await _reserve(harness, "claim-ambiguous")
    harness.factory.commit_mode = "after"
    claimed = await harness.repository.claim(row.id, ignore_backoff=True)
    assert claimed.status == "running"
    assert claimed.claim_token is not None


async def test_owned_transitions_are_fenced_and_ambiguity_safe(harness: _Harness) -> None:
    repository = harness.repository
    with pytest.raises(ValueError, match="unknown"):
        await repository.mark_step("missing", claim_token="none", step="bogus")
    with pytest.raises(RuntimeError, match="disappeared during"):
        await repository.complete(
            "missing",
            claim_token="none",
            result=WorkItem(key="MISSING-1", title="Missing").model_dump(mode="json"),
        )

    row = await _reserve(harness, "steps")
    claimed = await repository.claim(row.id, ignore_backoff=True)
    token = str(claimed.claim_token)
    assert (await repository.mark_step(row.id, claim_token=token, step="fields")).fields_status == (
        "completed"
    )
    assert (await repository.mark_step(row.id, claim_token=token, step="fields")).fields_status == (
        "completed"
    )

    harness.factory.commit_mode = "after"
    assert (
        await repository.mark_provider_attempted(row.id, claim_token=token)
    ).provider_attempted_at is not None

    harness.factory.commit_mode = "cancel"
    with pytest.raises(asyncio.CancelledError):
        await repository.mark_comment_attempted(row.id, claim_token=token)
    assert (await repository.get(row.id)).comment_attempted_at is None  # type: ignore[union-attr]

    await repository.mark_comment_attempted(row.id, claim_token=token)
    renewed = await repository.renew(row.id, claim_token=token)
    assert renewed.claimed_at is not None

    harness.factory.commit_mode = "after"
    result = WorkItem(key="PHX-1", title="Completed").model_dump(mode="json")
    completed = await repository.complete(row.id, claim_token=token, result=result)
    assert completed.status == "completed"
    replay = await repository.complete(row.id, claim_token="stale", result=result)
    assert replay.status == "completed"
    with pytest.raises(MutationClaimedError, match="lease was lost"):
        await repository.mark_step(row.id, claim_token=token, step="comment")

    failed_row = await _reserve(harness, "failed")
    failed_claim = await repository.claim(failed_row.id, ignore_backoff=True)
    failed = await repository.fail(
        failed_row.id,
        claim_token=str(failed_claim.claim_token),
        error_kind="provider",
        error="private\x00diagnostic",
    )
    assert failed.status == "failed"
    assert "\x00" not in str(failed.last_error)

    released_row = await _reserve(harness, "released")
    released_claim = await repository.claim(released_row.id, ignore_backoff=True)
    released = await repository.release(
        released_row.id,
        claim_token=str(released_claim.claim_token),
        error="retry\x00later",
        retry_delay=timedelta(minutes=1),
    )
    assert released.status == "pending"
    assert released.next_attempt_at is not None


async def test_transition_rejects_lost_owner_after_failed_commit(harness: _Harness) -> None:
    row = await _reserve(harness, "lost-transition")
    claimed = await harness.repository.claim(row.id, ignore_backoff=True)
    token = str(claimed.claim_token)

    harness.factory.commit_mode = "before"
    with pytest.raises(RuntimeError, match="rejected commit"):
        await harness.repository.mark_comment_attempted(row.id, claim_token=token)
    with pytest.raises(MutationClaimedError, match="lease was lost"):
        await harness.repository.mark_comment_attempted(row.id, claim_token="stolen")


async def test_defer_resolution_handles_terminal_active_and_stale_rows(harness: _Harness) -> None:
    repository = harness.repository
    with pytest.raises(RuntimeError, match="disappeared while deferring"):
        await repository.defer_resolution(
            "missing", error="missing", retry_delay=timedelta(seconds=1)
        )

    terminal = await _reserve(harness, "defer-terminal")
    _set_row(harness, terminal.id, status="failed")
    assert (
        await repository.defer_resolution(
            terminal.id, error="ignored", retry_delay=timedelta(seconds=1)
        )
    ).status == "failed"

    active = await _reserve(harness, "defer-active")
    await repository.claim(active.id, ignore_backoff=True)
    with pytest.raises(MutationClaimedError, match="already in progress"):
        await repository.defer_resolution(
            active.id, error="active", retry_delay=timedelta(seconds=1)
        )

    _set_row(
        harness,
        active.id,
        claimed_at=datetime.now(UTC) - MUTATION_LEASE - timedelta(seconds=1),
    )
    deferred = await repository.defer_resolution(
        active.id,
        error="adapter\x00unavailable",
        retry_delay=timedelta(minutes=1),
    )
    assert deferred.status == "pending"
    assert deferred.claim_token is None
    assert deferred.attempt_count == 2
    assert "\x00" not in str(deferred.last_error)


async def test_ready_ids_select_due_and_abandoned_work_only(harness: _Harness) -> None:
    now = datetime.now(UTC)
    values = {
        "pending-none": ("pending", None, None),
        "pending-due": ("pending", now - timedelta(seconds=1), None),
        "pending-future": ("pending", now + timedelta(minutes=1), None),
        "running-stale": ("running", None, now - MUTATION_LEASE - timedelta(seconds=1)),
        "running-unclaimed": ("running", None, None),
        "running-fresh": ("running", None, now),
        "completed": ("completed", None, None),
    }
    with Session(harness.engine) as session:
        for key, (status, next_attempt_at, claimed_at) in values.items():
            session.add(
                WorkItemMutation(
                    id=key,
                    provider_marker=f"marker-{key}",
                    tenant_scope="a" * 64,
                    consumer_id="consumer-1",
                    project_id="project-1",
                    connection_id="connection-1",
                    connection_version=harness.connection_version,
                    operation="update",
                    idempotency_key=key,
                    payload_hash=key.rjust(64, "0"),
                    payload={"key": key},
                    status=status,
                    fields_status="pending",
                    comment_status="pending",
                    next_attempt_at=next_attempt_at,
                    claimed_at=claimed_at,
                )
            )
        session.commit()

    ready = await harness.repository.ready_ids()
    assert set(ready) == {
        "pending-none",
        "pending-due",
        "running-stale",
        "running-unclaimed",
    }
    assert len(await harness.repository.ready_ids(limit=2)) == 2


async def _complete(harness: _Harness, key: str) -> WorkItemMutation:
    row = await _reserve(harness, key)
    claimed = await harness.repository.claim(row.id, ignore_backoff=True)
    return await harness.repository.complete(
        row.id,
        claim_token=str(claimed.claim_token),
        result=WorkItem(key=key, title=key).model_dump(mode="json"),
    )


async def test_retirement_recovers_commit_and_rejects_collisions(harness: _Harness) -> None:
    repository = harness.repository
    cutoff = datetime.now(UTC) + timedelta(seconds=1)
    assert await repository.retire_terminal_before(cutoff) == 0

    ambiguous = await _complete(harness, "retire-ambiguous")
    harness.factory.commit_mode = "after"
    assert await repository.retire_terminal_before(cutoff) == 1
    assert await repository.get(ambiguous.id) is None

    rejected = await _complete(harness, "retire-rejected")
    harness.factory.commit_mode = "before"
    with pytest.raises(RuntimeError, match="rejected commit"):
        await repository.retire_terminal_before(cutoff)
    assert await repository.get(rejected.id) is not None

    harness.factory.commit_mode = "cancel"
    with pytest.raises(asyncio.CancelledError):
        await repository.retire_terminal_before(cutoff)
    assert await repository.get(rejected.id) is not None

    rejected_scope = _scope("retire-rejected")
    with Session(harness.engine) as session:
        session.add(
            WorkItemMutationTombstone(
                scope_hash=mutation_scope_hash(rejected_scope),
                payload_hash="wrong".rjust(64, "0"),
                outcome="failed",
            )
        )
        session.commit()
    with pytest.raises(RuntimeError, match="hash collision"):
        await repository.retire_terminal_before(cutoff)


async def test_retirement_accepts_matching_preexisting_tombstone(harness: _Harness) -> None:
    row = await _complete(harness, "retire-existing")
    scope = _scope("retire-existing")
    with Session(harness.engine) as session:
        session.add(
            WorkItemMutationTombstone(
                scope_hash=mutation_scope_hash(scope),
                payload_hash=row.payload_hash,
                outcome="completed",
            )
        )
        session.commit()

    assert (
        await harness.repository.retire_terminal_before(datetime.now(UTC) + timedelta(seconds=1))
        == 1
    )
    assert await harness.repository.get(row.id) is None


async def test_rollback_failure_never_masks_the_original_database_error() -> None:
    class BrokenRollback:
        async def rollback(self) -> None:
            raise RuntimeError("rollback connection is gone")

    await mutation_module._rollback_quietly(cast(Any, BrokenRollback()))

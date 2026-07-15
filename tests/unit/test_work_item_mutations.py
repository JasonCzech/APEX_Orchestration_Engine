"""Idempotency and recovery regressions for work-item mutations."""

import asyncio
from types import SimpleNamespace
from typing import Any, cast

import pytest
from sqlalchemy import Table, create_engine, event
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from apex.auth.identity import ConsumerIdentity, ConsumerType, Role, ScopeRef
from apex.domain.integrations import Enrichment, WorkItem, WorkItemDraft
from apex.persistence.models import (
    Connection,
    HostMapping,
    WorkItemMutation,
    WorkItemMutationTombstone,
)
from apex.persistence.repositories import work_item_mutations as repository_mutation_module
from apex.persistence.repositories.work_item_mutations import (
    MUTATION_LEASE,
    MutationClaimedError,
    MutationConnectionChangedError,
    MutationPayloadConflictError,
    MutationRetiredError,
    MutationScope,
    WorkItemMutationsRepository,
)
from apex.services import work_item_mutations as mutation_module
from apex.services.work_item_mutations import (
    WorkItemMutationOutcomeAmbiguousError,
    WorkItemMutationService,
    reconcile_work_item_mutations_once,
)


class FakeIdempotentAdapter:
    provider = "fake"

    def __init__(self) -> None:
        self.created: dict[str, WorkItem] = {}
        self.items: dict[str, WorkItem] = {
            "PHX-1": WorkItem(key="PHX-1", title="Existing", status="open")
        }
        self.comment_markers: set[tuple[str, str]] = set()
        self.create_calls = 0
        self.field_calls = 0
        self.comment_calls = 0
        self.fail_create_after_commit = False
        self.fail_comment_after_commit = False
        self.block_create: asyncio.Event | None = None
        self.create_started = asyncio.Event()

    async def find_item_by_idempotency_marker(self, marker: str) -> WorkItem | None:
        return self.created.get(marker)

    async def create_item_idempotent(self, draft: WorkItemDraft, *, marker: str) -> WorkItem:
        self.create_calls += 1
        self.create_started.set()
        if self.block_create is not None:
            await self.block_create.wait()
        item = WorkItem(
            key=f"PHX-{900 + self.create_calls}",
            title=draft.title,
            kind=draft.kind,
            status="open",
            description=draft.description,
        )
        self.created[marker] = item
        self.items[item.key] = item
        if self.fail_create_after_commit:
            self.fail_create_after_commit = False
            raise RuntimeError("provider committed create, then disconnected")
        return item

    async def update_item_fields_idempotent(self, key: str, fields: dict[str, object]) -> None:
        await self.get_item(key)
        self.field_calls += 1
        status = fields.get("status")
        if isinstance(status, str):
            self.items[key] = self.items[key].model_copy(update={"status": status})

    async def has_comment_idempotency_marker(self, key: str, marker: str) -> bool:
        await self.get_item(key)
        return (key, marker) in self.comment_markers

    async def add_item_comment_idempotent(self, key: str, comment: str, *, marker: str) -> None:
        await self.get_item(key)
        self.comment_calls += 1
        self.comment_markers.add((key, marker))
        if self.fail_comment_after_commit:
            self.fail_comment_after_commit = False
            raise RuntimeError("provider committed comment, then disconnected")

    async def get_item(self, key: str) -> WorkItem:
        if key not in self.items:
            raise KeyError(key)
        return self.items[key]


class CancelledReserveRepository:
    async def reserve(self, **values: Any) -> Any:
        raise asyncio.CancelledError


class CommitThenDisconnectRepository(mutation_module._EphemeralMutationRepository):
    def __init__(self) -> None:
        super().__init__()
        self.disconnect_once = True

    async def complete(self, *args: Any, **kwargs: Any) -> Any:
        row = await super().complete(*args, **kwargs)
        if self.disconnect_once:
            self.disconnect_once = False
            raise RuntimeError("database committed result, then disconnected")
        return row


class CrashAfterDispatchFenceRepository(mutation_module._EphemeralMutationRepository):
    """Inject the process-death window after the fence commit and before provider IO."""

    def __init__(self, *, step: str) -> None:
        super().__init__()
        self.step = step
        self.crash_once = True

    async def mark_provider_attempted(self, *args: Any, **kwargs: Any) -> WorkItemMutation:
        row = await super().mark_provider_attempted(*args, **kwargs)
        if self.step == "create" and self.crash_once:
            self.crash_once = False
            raise RuntimeError("simulated process death after create fence")
        return row

    async def mark_comment_attempted(self, *args: Any, **kwargs: Any) -> WorkItemMutation:
        row = await super().mark_comment_attempted(*args, **kwargs)
        if self.step == "comment" and self.crash_once:
            self.crash_once = False
            raise RuntimeError("simulated process death after comment fence")
        return row


class RenewCountingRepository(mutation_module._EphemeralMutationRepository):
    def __init__(self) -> None:
        super().__init__()
        self.renewals = 0
        self.renewed = asyncio.Event()

    async def renew(self, mutation_id: str, *, claim_token: str) -> WorkItemMutation:
        self.renewals += 1
        self.renewed.set()
        return await super().renew(mutation_id, claim_token=claim_token)


class EventuallyConsistentCreateAdapter(FakeIdempotentAdapter):
    def __init__(self, invisible_lookups: int) -> None:
        super().__init__()
        self.invisible_lookups = invisible_lookups

    async def find_item_by_idempotency_marker(self, marker: str) -> WorkItem | None:
        item = await super().find_item_by_idempotency_marker(marker)
        if item is not None and self.invisible_lookups > 0:
            self.invisible_lookups -= 1
            return None
        return item


class EventuallyConsistentCommentAdapter(FakeIdempotentAdapter):
    def __init__(self, invisible_lookups: int) -> None:
        super().__init__()
        self.invisible_lookups = invisible_lookups

    async def has_comment_idempotency_marker(self, key: str, marker: str) -> bool:
        has_marker = await super().has_comment_idempotency_marker(key, marker)
        if has_marker and self.invisible_lookups > 0:
            self.invisible_lookups -= 1
            return False
        return has_marker


class ValueErrorAfterCreateAdapter(FakeIdempotentAdapter):
    async def create_item_idempotent(self, draft: WorkItemDraft, *, marker: str) -> WorkItem:
        await super().create_item_idempotent(draft, marker=marker)
        raise ValueError("provider returned malformed JSON after committing")


class NulWorkItemAdapter(FakeIdempotentAdapter):
    async def create_item_idempotent(self, draft: WorkItemDraft, *, marker: str) -> WorkItem:
        clean = await super().create_item_idempotent(draft, marker=marker)
        dirty = clean.model_copy(
            update={
                "title": "provider\x00title",
                "description": "provider\x00description",
                "url": "https://tracker.example/item\x00suffix",
            }
        )
        self.created[marker] = dirty
        return dirty


class LeaseStealingCommentAdapter(FakeIdempotentAdapter):
    def __init__(self, repository: Any) -> None:
        super().__init__()
        self.repository = repository

    async def has_comment_idempotency_marker(self, key: str, marker: str) -> bool:
        has_marker = await super().has_comment_idempotency_marker(key, marker)
        with self.repository._lock:
            row = next(iter(self.repository._by_id.values()))
            row.claim_token = "stolen-by-another-replica"
            row.claimed_at = mutation_module.datetime.now(mutation_module.UTC)
        return has_marker


class AsyncSessionFacade:
    """Minimal async facade over SQLite for repository state-machine tests."""

    def __init__(self, engine: Any) -> None:
        self._session = Session(engine, expire_on_commit=False)

    async def __aenter__(self) -> "AsyncSessionFacade":
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
        self._session.commit()

    async def rollback(self) -> None:
        self._session.rollback()


class AsyncSessionFacadeFactory:
    def __init__(self, engine: Any) -> None:
        self._engine = engine

    def __call__(self) -> AsyncSessionFacade:
        return AsyncSessionFacade(self._engine)


def identity(
    consumer_id: str = "consumer-1",
    project_id: str = "project-1",
    app_id: str | None = None,
) -> ConsumerIdentity:
    return ConsumerIdentity(
        consumer_id=consumer_id,
        name=consumer_id,
        consumer_type=ConsumerType.HEADLESS,
        role=Role.OPERATOR,
        scopes=[ScopeRef(project_id=project_id, app_id=app_id)],
    )


def service() -> WorkItemMutationService:
    repository = mutation_module._EphemeralMutationRepository()
    return WorkItemMutationService(repository, ephemeral_repository=repository)


@pytest.fixture(autouse=True)
def no_retry_delay(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(mutation_module, "_RETRY_DELAY", mutation_module.timedelta(0))


async def test_create_reconciles_commit_then_disconnect_without_duplicate() -> None:
    coordinator = service()
    adapter = FakeIdempotentAdapter()
    adapter.fail_create_after_commit = True
    kwargs = {
        "adapter": adapter,
        "draft": WorkItemDraft(title="Ambiguous create"),
        "identity": identity(),
        "project_id": "project-1",
        "connection_id": "connection-1",
        "connection_persisted": False,
        "connection_version": None,
        "idempotency_key": "create-ambiguous-1",
    }

    with pytest.raises(WorkItemMutationOutcomeAmbiguousError, match="outcome is ambiguous"):
        await coordinator.create(**kwargs)
    replay = await coordinator.create(**kwargs)

    assert replay.title == "Ambiguous create"
    assert adapter.create_calls == 1


async def test_create_waits_for_eventually_consistent_marker_without_second_post() -> None:
    coordinator = service()
    adapter = EventuallyConsistentCreateAdapter(invisible_lookups=2)
    adapter.fail_create_after_commit = True
    kwargs = {
        "adapter": adapter,
        "draft": WorkItemDraft(title="Search lag"),
        "identity": identity(),
        "project_id": "project-1",
        "connection_id": "connection-1",
        "connection_persisted": False,
        "connection_version": None,
        "idempotency_key": "eventual-marker",
    }

    with pytest.raises(WorkItemMutationOutcomeAmbiguousError, match="outcome is ambiguous"):
        await coordinator.create(**kwargs)
    with pytest.raises(WorkItemMutationOutcomeAmbiguousError, match="outcome is ambiguous"):
        await coordinator.create(**kwargs)
    with pytest.raises(WorkItemMutationOutcomeAmbiguousError, match="outcome is ambiguous"):
        await coordinator.create(**kwargs)
    replay = await coordinator.create(**kwargs)

    assert replay.title == "Search lag"
    assert adapter.create_calls == 1


async def test_ambiguous_value_error_after_create_remains_reconcilable() -> None:
    coordinator = service()
    adapter = ValueErrorAfterCreateAdapter()
    kwargs = {
        "adapter": adapter,
        "draft": WorkItemDraft(title="Malformed response"),
        "identity": identity(),
        "project_id": "project-1",
        "connection_id": "connection-1",
        "connection_persisted": False,
        "connection_version": None,
        "idempotency_key": "malformed-provider-response",
    }

    with pytest.raises(WorkItemMutationOutcomeAmbiguousError, match="outcome is ambiguous"):
        await coordinator.create(**kwargs)
    replay = await coordinator.create(**kwargs)

    assert replay.title == "Malformed response"
    assert adapter.create_calls == 1


async def test_create_crash_after_fence_surfaces_ambiguous_without_dispatch() -> None:
    repository = CrashAfterDispatchFenceRepository(step="create")
    coordinator = WorkItemMutationService(repository, ephemeral_repository=repository)
    adapter = FakeIdempotentAdapter()
    kwargs = {
        "adapter": adapter,
        "draft": WorkItemDraft(title="Pre-dispatch crash"),
        "identity": identity(),
        "project_id": "project-1",
        "connection_id": "connection-1",
        "connection_persisted": False,
        "connection_version": None,
        "idempotency_key": "crash-after-create-fence",
    }

    with pytest.raises(RuntimeError, match="simulated process death"):
        await coordinator.create(**kwargs)
    with pytest.raises(WorkItemMutationOutcomeAmbiguousError) as excinfo:
        await coordinator.create(**kwargs)

    assert adapter.create_calls == 0
    assert excinfo.value.mutation_id
    assert excinfo.value.provider_marker.startswith("apex-idem-")


async def test_provider_nul_result_is_sanitized_for_completion_and_replay() -> None:
    coordinator = service()
    adapter = NulWorkItemAdapter()
    kwargs = {
        "adapter": adapter,
        "draft": WorkItemDraft(title="Provider normalization"),
        "identity": identity(),
        "project_id": "project-1",
        "connection_id": "connection-1",
        "connection_persisted": False,
        "connection_version": None,
        "idempotency_key": "provider-nul-result",
    }

    completed = await coordinator.create(**kwargs)
    replay = await coordinator.create(**kwargs)

    assert completed == replay
    assert completed.title == "provider\ufffdtitle"
    assert completed.description == "provider\ufffddescription"
    assert completed.url == "https://tracker.example/item\ufffdsuffix"
    assert "\x00" not in str(completed.model_dump(mode="json"))


async def test_persisted_error_text_is_nul_safe_and_bounded() -> None:
    repository = mutation_module._EphemeralMutationRepository()
    row = await repository.reserve(
        scope=MutationScope(
            tenant_scope="a" * 64,
            consumer_id="consumer-1",
            connection_id="connection-1",
            operation="create",
            idempotency_key="safe-error",
        ),
        payload_hash="b" * 64,
        payload={"draft": {"title": "safe"}},
        target_key=None,
        project_id="project-1",
        connection_version=mutation_module.datetime.now(mutation_module.UTC),
        fields_status="skipped",
        comment_status="skipped",
    )
    claimed = await repository.claim(row.id, ignore_backoff=True)

    released = await repository.release(
        row.id,
        claim_token=str(claimed.claim_token),
        error="bad\x00provider" + "x" * 5000,
        retry_delay=mutation_module.timedelta(0),
    )

    assert released.last_error is not None
    assert "\x00" not in released.last_error
    assert "\ufffd" in released.last_error
    assert len(released.last_error) == 4096


def test_durable_mutation_errors_redact_credentials_in_both_persistence_paths() -> None:
    canary = "mutation-error-secret-canary"
    error = (
        f"Authorization: Bearer {canary}; password={canary}; "
        f"url=https://storage.example/blob?X-Amz-Signature={canary}"
    )

    service_error = mutation_module._safe_error_text(error)
    repository_error = repository_mutation_module._safe_error_text(error)

    assert service_error == repository_error
    assert canary not in service_error
    assert "[REDACTED]" in service_error


async def test_completed_result_commit_disconnect_replays_without_provider_mutation() -> None:
    repository = CommitThenDisconnectRepository()
    coordinator = WorkItemMutationService(repository, ephemeral_repository=repository)
    adapter = FakeIdempotentAdapter()
    kwargs = {
        "adapter": adapter,
        "draft": WorkItemDraft(title="Database ambiguity"),
        "identity": identity(),
        "project_id": "project-1",
        "connection_id": "connection-1",
        "connection_persisted": False,
        "connection_version": None,
        "idempotency_key": "database-commit-ambiguity",
    }

    with pytest.raises(RuntimeError, match="database committed"):
        await coordinator.create(**kwargs)
    replay = await coordinator.create(**kwargs)

    assert replay.title == "Database ambiguity"
    assert adapter.create_calls == 1


async def test_same_key_different_payload_conflicts_before_provider_call() -> None:
    coordinator = service()
    adapter = FakeIdempotentAdapter()
    common = {
        "adapter": adapter,
        "identity": identity(),
        "project_id": "project-1",
        "connection_id": "connection-1",
        "connection_persisted": False,
        "connection_version": None,
        "idempotency_key": "same-key",
    }
    await coordinator.create(draft=WorkItemDraft(title="First"), **common)

    with pytest.raises(MutationPayloadConflictError):
        await coordinator.create(draft=WorkItemDraft(title="Different"), **common)

    assert adapter.create_calls == 1


async def test_keys_are_isolated_by_tenant_consumer_connection_and_operation() -> None:
    coordinator = service()
    adapter = FakeIdempotentAdapter()
    draft = WorkItemDraft(title="Scoped")

    await coordinator.create(
        adapter=adapter,
        draft=draft,
        identity=identity("consumer-1", "project-1"),
        project_id="project-1",
        connection_id="connection-1",
        connection_persisted=False,
        connection_version=None,
        idempotency_key="shared",
    )
    await coordinator.create(
        adapter=adapter,
        draft=draft,
        identity=identity("consumer-1", "project-2"),
        project_id="project-2",
        connection_id="connection-1",
        connection_persisted=False,
        connection_version=None,
        idempotency_key="shared",
    )
    await coordinator.create(
        adapter=adapter,
        draft=draft,
        identity=identity("consumer-2", "project-1"),
        project_id="project-1",
        connection_id="connection-1",
        connection_persisted=False,
        connection_version=None,
        idempotency_key="shared",
    )
    await coordinator.create(
        adapter=adapter,
        draft=draft,
        identity=identity("consumer-1", "project-1"),
        project_id="project-1",
        connection_id="connection-2",
        connection_persisted=False,
        connection_version=None,
        idempotency_key="shared",
    )
    await coordinator.enrich(
        adapter=adapter,
        key="PHX-1",
        enrichment=Enrichment(fields={"status": "closed"}),
        identity=identity("consumer-1", "project-1"),
        project_id="project-1",
        connection_id="connection-1",
        connection_persisted=False,
        connection_version=None,
        idempotency_key="shared",
    )

    assert adapter.create_calls == 4
    assert adapter.field_calls == 1


async def test_scope_shape_change_replays_same_target_instead_of_mutating_again() -> None:
    coordinator = service()
    adapter = FakeIdempotentAdapter()
    scoped = identity("consumer-1", "project-1")
    unscoped = ConsumerIdentity(
        consumer_id="consumer-1",
        name="consumer-1",
        consumer_type=ConsumerType.HEADLESS,
        role=Role.OPERATOR,
        scopes=[],
    )
    common = {
        "adapter": adapter,
        "draft": WorkItemDraft(title="Stable tenant target"),
        "project_id": "project-1",
        "connection_id": "connection-1",
        "connection_persisted": False,
        "connection_version": None,
        "idempotency_key": "scope-transition",
    }

    created = await coordinator.create(identity=scoped, **common)
    replay = await coordinator.create(identity=unscoped, **common)

    assert replay == created
    assert adapter.create_calls == 1


async def test_partial_enrichment_resumes_steps_without_duplicate_comment_or_fields() -> None:
    coordinator = service()
    adapter = FakeIdempotentAdapter()
    adapter.fail_comment_after_commit = True
    kwargs = {
        "adapter": adapter,
        "key": "PHX-1",
        "enrichment": Enrichment(fields={"status": "closed"}, comment="triaged"),
        "identity": identity(),
        "project_id": "project-1",
        "connection_id": "connection-1",
        "connection_persisted": False,
        "connection_version": None,
        "idempotency_key": "enrich-partial",
    }

    with pytest.raises(WorkItemMutationOutcomeAmbiguousError, match="outcome is ambiguous"):
        await coordinator.enrich(**kwargs)
    replay = await coordinator.enrich(**kwargs)

    assert replay.status == "closed"
    assert adapter.field_calls == 1
    assert adapter.comment_calls == 1


async def test_comment_waits_for_eventually_consistent_marker_without_second_post() -> None:
    coordinator = service()
    adapter = EventuallyConsistentCommentAdapter(invisible_lookups=2)
    adapter.fail_comment_after_commit = True
    kwargs = {
        "adapter": adapter,
        "key": "PHX-1",
        "enrichment": Enrichment(comment="eventually visible"),
        "identity": identity(),
        "project_id": "project-1",
        "connection_id": "connection-1",
        "connection_persisted": False,
        "connection_version": None,
        "idempotency_key": "eventual-comment-marker",
    }

    with pytest.raises(WorkItemMutationOutcomeAmbiguousError, match="outcome is ambiguous"):
        await coordinator.enrich(**kwargs)
    with pytest.raises(WorkItemMutationOutcomeAmbiguousError, match="outcome is ambiguous"):
        await coordinator.enrich(**kwargs)
    with pytest.raises(WorkItemMutationOutcomeAmbiguousError, match="outcome is ambiguous"):
        await coordinator.enrich(**kwargs)
    replay = await coordinator.enrich(**kwargs)

    assert replay.key == "PHX-1"
    assert adapter.comment_calls == 1


async def test_comment_crash_after_fence_surfaces_ambiguous_without_dispatch() -> None:
    repository = CrashAfterDispatchFenceRepository(step="comment")
    coordinator = WorkItemMutationService(repository, ephemeral_repository=repository)
    adapter = FakeIdempotentAdapter()
    kwargs = {
        "adapter": adapter,
        "key": "PHX-1",
        "enrichment": Enrichment(comment="Pre-dispatch crash"),
        "identity": identity(),
        "project_id": "project-1",
        "connection_id": "connection-1",
        "connection_persisted": False,
        "connection_version": None,
        "idempotency_key": "crash-after-comment-fence",
    }

    with pytest.raises(RuntimeError, match="simulated process death"):
        await coordinator.enrich(**kwargs)
    with pytest.raises(WorkItemMutationOutcomeAmbiguousError) as excinfo:
        await coordinator.enrich(**kwargs)

    assert adapter.comment_calls == 0
    assert excinfo.value.operation == "comment"


async def test_stolen_lease_after_marker_scan_cannot_post_comment() -> None:
    repository = mutation_module._EphemeralMutationRepository()
    coordinator = WorkItemMutationService(repository, ephemeral_repository=repository)
    adapter = LeaseStealingCommentAdapter(repository)

    with pytest.raises(MutationClaimedError, match="lease was lost"):
        await coordinator.enrich(
            adapter=adapter,
            key="PHX-1",
            enrichment=Enrichment(comment="must not duplicate"),
            identity=identity(),
            project_id="project-1",
            connection_id="connection-1",
            connection_persisted=False,
            connection_version=None,
            idempotency_key="stolen-comment-lease",
        )

    assert adapter.comment_calls == 0


async def test_cancellation_during_create_is_observable_without_unsafe_second_post() -> None:
    coordinator = service()
    adapter = FakeIdempotentAdapter()
    adapter.block_create = asyncio.Event()
    kwargs = {
        "adapter": adapter,
        "draft": WorkItemDraft(title="Cancelled"),
        "identity": identity(),
        "project_id": "project-1",
        "connection_id": "connection-1",
        "connection_persisted": False,
        "connection_version": None,
        "idempotency_key": "cancel-provider",
    }
    task = asyncio.create_task(coordinator.create(**kwargs))
    await adapter.create_started.wait()
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task
    adapter.block_create = None
    with pytest.raises(WorkItemMutationOutcomeAmbiguousError, match="outcome is ambiguous"):
        await coordinator.create(**kwargs)

    assert adapter.create_calls == 1


async def test_long_provider_operation_renews_its_lease(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(mutation_module, "_LEASE_HEARTBEAT_S", 0.001)
    repository = RenewCountingRepository()
    coordinator = WorkItemMutationService(repository, ephemeral_repository=repository)
    adapter = FakeIdempotentAdapter()
    adapter.block_create = asyncio.Event()
    task = asyncio.create_task(
        coordinator.create(
            adapter=adapter,
            draft=WorkItemDraft(title="Slow create"),
            identity=identity(),
            project_id="project-1",
            connection_id="connection-1",
            connection_persisted=False,
            connection_version=None,
            idempotency_key="heartbeat",
        )
    )
    await adapter.create_started.wait()
    await repository.renewed.wait()
    adapter.block_create.set()

    result = await task

    assert result.title == "Slow create"
    assert repository.renewals >= 1


async def test_completed_step_does_not_let_a_stale_claim_continue() -> None:
    repository = mutation_module._EphemeralMutationRepository()
    row = await repository.reserve(
        scope=MutationScope(
            tenant_scope="a" * 64,
            consumer_id="consumer-1",
            connection_id="connection-1",
            operation="enrich",
            idempotency_key="stale-token",
        ),
        payload_hash="b" * 64,
        payload={"key": "PHX-1", "enrichment": {"fields": {"status": "closed"}}},
        target_key="PHX-1",
        project_id="project-1",
        connection_version=mutation_module.datetime.now(mutation_module.UTC),
        fields_status="pending",
        comment_status="pending",
    )
    first = await repository.claim(row.id, ignore_backoff=True)
    first_token = str(first.claim_token)
    first.claimed_at = (
        mutation_module.datetime.now(mutation_module.UTC)
        - MUTATION_LEASE
        - mutation_module.timedelta(seconds=1)
    )
    second = await repository.claim(row.id, ignore_backoff=True)
    second_token = str(second.claim_token)
    await repository.mark_step(row.id, claim_token=second_token, step="fields")

    with pytest.raises(MutationClaimedError, match="lease was lost"):
        await repository.mark_step(row.id, claim_token=first_token, step="fields")


async def test_cancellation_while_reserving_never_reaches_provider() -> None:
    adapter = FakeIdempotentAdapter()
    coordinator = WorkItemMutationService(CancelledReserveRepository())

    with pytest.raises(asyncio.CancelledError):
        await coordinator.create(
            adapter=adapter,
            draft=WorkItemDraft(title="Never sent"),
            identity=identity(),
            project_id="project-1",
            connection_id="connection-1",
            connection_persisted=True,
            connection_version=mutation_module.datetime.now(mutation_module.UTC),
            idempotency_key="cancel-reserve",
        )

    assert adapter.create_calls == 0


async def test_terminal_retirement_compacts_payload_releases_fk_and_blocks_reuse() -> None:
    engine = create_engine("sqlite://", poolclass=StaticPool)

    @event.listens_for(engine, "connect")
    def attach_apex(dbapi_connection: Any, _record: Any) -> None:
        dbapi_connection.execute("ATTACH DATABASE ':memory:' AS apex")
        dbapi_connection.execute("PRAGMA foreign_keys=ON")

    maker = AsyncSessionFacadeFactory(engine)
    try:
        with engine.begin() as connection:
            cast(Table, Connection.__table__).create(connection)
            cast(Table, HostMapping.__table__).create(connection)
            cast(Table, WorkItemMutation.__table__).create(connection)
            cast(Table, WorkItemMutationTombstone.__table__).create(connection)
        connection_version = mutation_module.datetime.now(mutation_module.UTC)
        async with maker() as session:
            session.add(
                Connection(
                    id="connection-1",
                    kind="work_tracking",
                    provider="stub",
                    name="retirement-test",
                    enabled=True,
                    updated_at=connection_version,
                    runtime_version=connection_version,
                )
            )
            await session.commit()

        repository = WorkItemMutationsRepository(maker)  # type: ignore[arg-type]
        scope = MutationScope(
            tenant_scope="a" * 64,
            consumer_id="consumer-1",
            connection_id="connection-1",
            operation="create",
            idempotency_key="retire-me",
        )
        payload = {"draft": {"title": "Retire me"}}
        with pytest.raises(MutationConnectionChangedError):
            await repository.reserve(
                scope=scope,
                payload_hash="b" * 64,
                payload=payload,
                target_key=None,
                project_id="project-1",
                connection_version=connection_version - mutation_module.timedelta(seconds=1),
                fields_status="skipped",
                comment_status="skipped",
            )
        row = await repository.reserve(
            scope=scope,
            payload_hash="b" * 64,
            payload=payload,
            target_key=None,
            project_id="project-1",
            connection_version=connection_version,
            fields_status="skipped",
            comment_status="skipped",
        )
        claimed = await repository.claim(row.id, ignore_backoff=True)
        await repository.complete(
            row.id,
            claim_token=str(claimed.claim_token),
            result=WorkItem(key="PHX-1", title="Retire me").model_dump(mode="json"),
        )

        retired = await repository.retire_terminal_before(
            mutation_module.datetime.now(mutation_module.UTC) + mutation_module.timedelta(seconds=1)
        )

        assert retired == 1
        assert await repository.get(row.id) is None
        tombstone = await repository.get_tombstone(scope)
        assert tombstone is not None
        assert tombstone.payload_hash == "b" * 64
        with pytest.raises(MutationRetiredError):
            await repository.reserve(
                scope=scope,
                payload_hash="b" * 64,
                payload=payload,
                target_key=None,
                project_id="project-1",
                connection_version=connection_version,
                fields_status="skipped",
                comment_status="skipped",
            )
        with pytest.raises(MutationPayloadConflictError):
            await repository.reserve(
                scope=scope,
                payload_hash="c" * 64,
                payload={"draft": {"title": "Different"}},
                target_key=None,
                project_id="project-1",
                connection_version=connection_version,
                fields_status="skipped",
                comment_status="skipped",
            )

        async with maker() as session:
            connection = await session.get(Connection, "connection-1")
            assert connection is not None
            await session.delete(connection)
            await session.commit()
            assert await session.get(Connection, "connection-1") is None
    finally:
        engine.dispose()


async def test_resolution_failures_are_deferred_so_later_rows_are_not_starved() -> None:
    version = mutation_module.datetime.now(mutation_module.UTC)
    rows = {
        f"mutation-{index:02d}": SimpleNamespace(
            id=f"mutation-{index:02d}",
            connection_id=f"connection-{index:02d}",
            project_id="project-1",
            connection_version=version,
        )
        for index in range(26)
    }

    class FakeRepository:
        async def get(self, mutation_id: str) -> Any:
            return rows.get(mutation_id)

    class FakeService:
        def __init__(self) -> None:
            self._repository = FakeRepository()
            self.deferred: set[str] = set()
            self.resumed: list[str] = []

        async def ready_ids(self) -> list[str]:
            available = [
                mutation_id
                for mutation_id in rows
                if mutation_id not in self.deferred and mutation_id not in self.resumed
            ]
            return available[:25]

        async def defer_resolution(self, mutation_id: str, _error: str) -> None:
            self.deferred.add(mutation_id)

        async def resume(self, mutation_id: str, _adapter: Any) -> WorkItem:
            self.resumed.append(mutation_id)
            return WorkItem(key="PHX-1", title="reconciled")

        async def retire_terminal(self) -> int:
            return 0

    class FakeResolver:
        async def resolve_with_metadata(
            self, _kind: Any, *, connection_id: str, project_id: str
        ) -> Any:
            del project_id
            if connection_id != "connection-25":
                raise RuntimeError("secret store unavailable")
            return SimpleNamespace(
                persisted=True,
                connection_version=version,
                adapter=FakeIdempotentAdapter(),
            )

    coordinator = FakeService()
    resolver = FakeResolver()

    assert (
        await reconcile_work_item_mutations_once(
            service=coordinator,  # type: ignore[arg-type]
            resolver=resolver,  # type: ignore[arg-type]
        )
        == 0
    )
    assert len(coordinator.deferred) == 25
    assert (
        await reconcile_work_item_mutations_once(
            service=coordinator,  # type: ignore[arg-type]
            resolver=resolver,  # type: ignore[arg-type]
        )
        == 1
    )
    assert coordinator.resumed == ["mutation-25"]

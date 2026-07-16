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
    canonical_mutation_payload_hash,
    mutation_tenant_scope,
)
from apex.ports.work_tracking import WorkTrackingMutationRejectedError
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


class BlockingReleaseRepository(mutation_module._EphemeralMutationRepository):
    def __init__(self) -> None:
        super().__init__()
        self.release_started = asyncio.Event()
        self.allow_release = asyncio.Event()
        self.release_completed = False

    async def release(self, *args: Any, **kwargs: Any) -> WorkItemMutation:
        self.release_started.set()
        await self.allow_release.wait()
        row = await super().release(*args, **kwargs)
        self.release_completed = True
        return row


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


class SlowCancellationCleanupAdapter(FakeIdempotentAdapter):
    def __init__(self) -> None:
        super().__init__()
        self.cleanup_started = asyncio.Event()
        self.allow_cleanup = asyncio.Event()
        self.cleanup_completed = False

    async def create_item_idempotent(self, draft: WorkItemDraft, *, marker: str) -> WorkItem:
        self.create_calls += 1
        self.create_started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            self.cleanup_started.set()
            await self.allow_cleanup.wait()
            self.cleanup_completed = True
            raise
        raise AssertionError("blocking provider unexpectedly resumed")


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
                "url": "https://tracker.example/item",
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


def test_mutation_domain_models_reject_credentials_without_reflection() -> None:
    canary = "mutation-request-secret-canary"

    with pytest.raises(ValueError) as draft_error:
        WorkItemDraft(
            title=f"Authorization: Bearer {canary}",
            fields={"summary": "safe"},
        )
    with pytest.raises(ValueError) as enrichment_error:
        Enrichment(fields={"api_token": canary}, comment="triage")

    assert canary not in str(draft_error.value)
    assert canary not in str(enrichment_error.value)
    assert "credential material" in str(draft_error.value)
    assert "credential material" in str(enrichment_error.value)


async def test_mutation_service_revalidates_constructed_models_before_reserve() -> None:
    canary = "constructed-mutation-secret-canary"
    repository = mutation_module._EphemeralMutationRepository()
    coordinator = WorkItemMutationService(repository, ephemeral_repository=repository)
    adapter = FakeIdempotentAdapter()
    unsafe_draft = WorkItemDraft.model_construct(
        title=f"password={canary}",
        kind="story",
        description="",
        fields={},
    )

    with pytest.raises(ValueError, match="invalid or contains credential material") as raised:
        await coordinator.create(
            adapter=adapter,
            draft=unsafe_draft,
            identity=identity(),
            project_id="project-1",
            connection_id="connection-1",
            connection_persisted=False,
            connection_version=None,
            idempotency_key="constructed-draft",
        )

    assert canary not in str(raised.value)
    assert raised.value.__cause__ is None
    assert raised.value.__context__ is None
    assert repository._by_id == {}
    assert adapter.create_calls == 0


async def test_mutation_service_rejects_credential_idempotency_key_before_reserve() -> None:
    canary = "mutation-idempotency-secret-canary"
    repository = mutation_module._EphemeralMutationRepository()
    coordinator = WorkItemMutationService(repository, ephemeral_repository=repository)
    adapter = FakeIdempotentAdapter()

    with pytest.raises(ValueError, match="must not contain credential material") as raised:
        await coordinator.create(
            adapter=adapter,
            draft=WorkItemDraft(title="Safe draft"),
            identity=identity(),
            project_id="project-1",
            connection_id="connection-1",
            connection_persisted=False,
            connection_version=None,
            idempotency_key=f"Authorization: Bearer {canary}",
        )

    assert canary not in str(raised.value)
    assert repository._by_id == {}
    assert adapter.create_calls == 0


def test_mutation_scope_rejects_noncanonical_and_hostile_identity_components() -> None:
    calls: list[str] = []

    class HostileText(str):
        def strip(self, *_args: object, **_kwargs: object) -> str:
            calls.append("strip")
            raise AssertionError("hostile scalar hook ran")

    for updates in (
        {"idempotency_key": HostileText("mutation-a")},
        {"idempotency_key": " mutation-a"},
        {"idempotency_key": "x" * 256},
        {"connection_id": HostileText("connection-1")},
        {"project_id": HostileText("project-1")},
    ):
        kwargs: dict[str, Any] = {
            "identity": identity(),
            "project_id": "project-1",
            "connection_id": "connection-1",
            "operation": "create",
            "idempotency_key": "mutation-a",
            **updates,
        }
        with pytest.raises(ValueError):
            mutation_module._scope(**kwargs)

    assert calls == []


async def test_mutation_service_rejects_unsafe_target_key_before_reserve() -> None:
    repository = mutation_module._EphemeralMutationRepository()
    coordinator = WorkItemMutationService(repository, ephemeral_repository=repository)

    with pytest.raises(ValueError, match="safe identifier"):
        await coordinator.replay_enrich(
            key="../OTHER-1",
            enrichment=Enrichment(comment="triage"),
            identity=identity(),
            project_id="project-1",
            connection_id="connection-1",
            connection_persisted=False,
            connection_version=None,
            idempotency_key="unsafe-target",
        )

    assert repository._by_id == {}


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


async def test_provider_mutation_failures_do_not_retain_raw_diagnostics() -> None:
    ambiguous_canary = "bare-ambiguous-provider-canary"

    class AmbiguousAdapter(FakeIdempotentAdapter):
        async def create_item_idempotent(
            self,
            draft: WorkItemDraft,
            *,
            marker: str,
        ) -> WorkItem:
            del draft, marker
            raise RuntimeError(ambiguous_canary)

    coordinator = service()
    with pytest.raises(WorkItemMutationOutcomeAmbiguousError) as ambiguous_error:
        await coordinator.create(
            adapter=AmbiguousAdapter(),
            draft=WorkItemDraft(title="Ambiguous provider"),
            identity=identity(),
            project_id="project-1",
            connection_id="connection-1",
            connection_persisted=False,
            connection_version=None,
            idempotency_key="ambiguous-provider-chain",
        )

    assert ambiguous_error.value.__cause__ is None
    assert ambiguous_error.value.__context__ is None
    assert ambiguous_canary not in str(ambiguous_error.value)

    rejection_canary = "bare-rejected-provider-canary"

    class RejectedAdapter(FakeIdempotentAdapter):
        def validate_create_item_idempotent(
            self,
            _draft: WorkItemDraft,
            *,
            marker: str,
        ) -> None:
            del marker
            raise ValueError(rejection_canary)

    coordinator = service()
    with pytest.raises(WorkTrackingMutationRejectedError) as rejected_error:
        await coordinator.create(
            adapter=RejectedAdapter(),
            draft=WorkItemDraft(title="Rejected provider"),
            identity=identity(),
            project_id="project-1",
            connection_id="connection-1",
            connection_persisted=False,
            connection_version=None,
            idempotency_key="rejected-provider-chain",
        )

    assert rejected_error.value.__cause__ is None
    assert rejected_error.value.__context__ is None
    assert rejection_canary not in str(rejected_error.value)


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
    assert completed.url == "https://tracker.example/item"
    assert "\x00" not in str(completed.model_dump(mode="json"))


async def test_provider_credential_text_is_redacted_before_completion_and_replay() -> None:
    secret = "work-item-description-secret-canary"

    class CredentialTextAdapter(FakeIdempotentAdapter):
        async def create_item_idempotent(
            self,
            draft: WorkItemDraft,
            *,
            marker: str,
        ) -> WorkItem:
            clean = await super().create_item_idempotent(draft, marker=marker)
            dirty = clean.model_copy(
                update={
                    "title": f"password={secret}",
                    "description": f"Authorization: Bearer {secret}",
                }
            )
            self.created[marker] = dirty
            return dirty

    repository = mutation_module._EphemeralMutationRepository()
    coordinator = WorkItemMutationService(repository, ephemeral_repository=repository)
    adapter = CredentialTextAdapter()
    draft = WorkItemDraft(title="Provider text boundary")
    common = {
        "draft": draft,
        "identity": identity(),
        "project_id": "project-1",
        "connection_id": "connection-1",
        "connection_persisted": False,
        "connection_version": None,
        "idempotency_key": "credential-provider-text",
    }

    completed = await coordinator.create(adapter=adapter, **common)
    replay = await coordinator.replay_create(**common)

    assert replay == completed
    assert secret not in str(completed.model_dump(mode="json"))
    assert "[REDACTED]" in completed.title
    assert "[REDACTED]" in completed.description
    row = next(iter(repository._by_id.values()))
    assert secret not in str(row.result)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("key", "password=work-item-provider-secret-canary"),
        ("kind", "token=work-item-provider-secret-canary"),
        ("status", "Authorization: Bearer work-item-provider-secret-canary"),
        ("url", "https://tracker.example/item?token=work-item-provider-secret-canary"),
        ("url", " https://tracker.example/item"),
        ("url", "https://tracker.example/item#credential"),
    ],
)
async def test_provider_executable_fields_fail_before_completion_without_reflection(
    field: str,
    value: str,
) -> None:
    class UnsafeItemAdapter(FakeIdempotentAdapter):
        async def create_item_idempotent(
            self,
            draft: WorkItemDraft,
            *,
            marker: str,
        ) -> WorkItem:
            clean = await super().create_item_idempotent(draft, marker=marker)
            dirty = clean.model_copy(update={field: value})
            self.created[marker] = dirty
            return dirty

    repository = mutation_module._EphemeralMutationRepository()
    coordinator = WorkItemMutationService(repository, ephemeral_repository=repository)
    with pytest.raises(RuntimeError, match="invalid work item") as raised:
        await coordinator.create(
            adapter=UnsafeItemAdapter(),
            draft=WorkItemDraft(title="Unsafe provider output"),
            identity=identity(),
            project_id="project-1",
            connection_id="connection-1",
            connection_persisted=False,
            connection_version=None,
            idempotency_key=f"unsafe-provider-{field}-{len(value)}",
        )

    assert raised.value.__cause__ is None
    assert "work-item-provider-secret-canary" not in str(raised.value)
    row = next(iter(repository._by_id.values()))
    assert row.result is None
    assert "work-item-provider-secret-canary" not in str(row.last_error)


async def test_constructed_oversized_provider_item_fails_before_completion() -> None:
    secret = "oversized-work-item-secret-canary"

    class OversizedItemAdapter(FakeIdempotentAdapter):
        async def create_item_idempotent(
            self,
            draft: WorkItemDraft,
            *,
            marker: str,
        ) -> WorkItem:
            del draft, marker
            return WorkItem.model_construct(
                key="PHX-999",
                title="x" * 100_000 + secret,
                kind="story",
                status="open",
                description="",
                url=None,
            )

    repository = mutation_module._EphemeralMutationRepository()
    coordinator = WorkItemMutationService(repository, ephemeral_repository=repository)
    with pytest.raises(RuntimeError, match="invalid work item") as raised:
        await coordinator.create(
            adapter=OversizedItemAdapter(),
            draft=WorkItemDraft(title="Oversized provider output"),
            identity=identity(),
            project_id="project-1",
            connection_id="connection-1",
            connection_persisted=False,
            connection_version=None,
            idempotency_key="oversized-provider-item",
        )

    assert raised.value.__cause__ is None
    assert secret not in str(raised.value)
    assert next(iter(repository._by_id.values())).result is None


async def test_legacy_credential_result_fails_closed_on_replay() -> None:
    secret = "legacy-work-item-result-secret-canary"
    repository = mutation_module._EphemeralMutationRepository()
    coordinator = WorkItemMutationService(repository, ephemeral_repository=repository)
    draft = WorkItemDraft(title="Legacy replay")
    common = {
        "draft": draft,
        "identity": identity(),
        "project_id": "project-1",
        "connection_id": "connection-1",
        "connection_persisted": False,
        "connection_version": None,
        "idempotency_key": "legacy-credential-result",
    }
    await coordinator.create(adapter=FakeIdempotentAdapter(), **common)
    row = next(iter(repository._by_id.values()))
    assert row.result is not None
    row.result["url"] = f"https://tracker.example/item?token={secret}"

    with pytest.raises(RuntimeError, match="invalid durable result") as raised:
        await coordinator.replay_create(**common)

    assert raised.value.__cause__ is None
    assert secret not in str(raised.value)


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
        role=Role.ADMIN,
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


@pytest.mark.parametrize(
    ("role", "scopes", "project_id"),
    [
        (Role.VIEWER, [ScopeRef(project_id="project-1")], "project-1"),
        (
            Role.OPERATOR,
            [ScopeRef(project_id="project-1", app_id="app-1")],
            "project-1",
        ),
        (Role.OPERATOR, [], None),
    ],
)
async def test_service_rejects_non_project_wide_mutation_identity_before_io(
    role: Role,
    scopes: list[ScopeRef],
    project_id: str | None,
) -> None:
    repository = mutation_module._EphemeralMutationRepository()
    coordinator = WorkItemMutationService(repository, ephemeral_repository=repository)
    adapter = FakeIdempotentAdapter()
    who = ConsumerIdentity(
        consumer_id="consumer-1",
        name="consumer-1",
        consumer_type=ConsumerType.HEADLESS,
        role=role,
        scopes=scopes,
    )

    with pytest.raises(PermissionError, match="project-wide operator scope"):
        await coordinator.create(
            adapter=adapter,
            draft=WorkItemDraft(title="Unauthorized direct mutation"),
            identity=who,
            project_id=project_id,
            connection_id="connection-1",
            connection_persisted=False,
            connection_version=None,
            idempotency_key="unauthorized-direct-mutation",
        )

    assert adapter.create_calls == 0
    assert repository._by_id == {}


async def test_replay_rejects_corrupted_project_binding() -> None:
    repository = mutation_module._EphemeralMutationRepository()
    coordinator = WorkItemMutationService(repository, ephemeral_repository=repository)
    adapter = FakeIdempotentAdapter()
    draft = WorkItemDraft(title="Project-bound replay")
    common = {
        "draft": draft,
        "identity": identity(),
        "project_id": "project-1",
        "connection_id": "connection-1",
        "connection_persisted": False,
        "connection_version": None,
        "idempotency_key": "corrupt-project-binding",
    }
    await coordinator.create(adapter=adapter, **common)
    row = next(iter(repository._by_id.values()))
    row.project_id = "project-2"

    with pytest.raises(MutationConnectionChangedError, match="project binding changed"):
        await coordinator.replay_create(**common)

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


async def test_enrichment_rejects_a_provider_result_for_another_key() -> None:
    class MismatchedReadAdapter(FakeIdempotentAdapter):
        async def get_item(self, key: str) -> WorkItem:
            item = await super().get_item(key)
            return item.model_copy(update={"key": "PHX-2"})

    coordinator = service()
    adapter = MismatchedReadAdapter()

    with pytest.raises(RuntimeError, match="mismatched enrichment target"):
        await coordinator.enrich(
            adapter=adapter,
            key="PHX-1",
            enrichment=Enrichment(comment="triaged"),
            identity=identity(),
            project_id="project-1",
            connection_id="connection-1",
            connection_persisted=False,
            connection_version=None,
            idempotency_key="mismatched-enrichment-result",
        )


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


async def test_repeated_cancellation_definitively_settles_lease_release() -> None:
    repository = BlockingReleaseRepository()
    coordinator = WorkItemMutationService(repository, ephemeral_repository=repository)
    adapter = FakeIdempotentAdapter()
    adapter.block_create = asyncio.Event()
    task = asyncio.create_task(
        coordinator.create(
            adapter=adapter,
            draft=WorkItemDraft(title="Repeated cancellation"),
            identity=identity(),
            project_id="project-1",
            connection_id="connection-1",
            connection_persisted=False,
            connection_version=None,
            idempotency_key="repeated-cancel-release",
        )
    )
    await adapter.create_started.wait()
    task.cancel()
    await repository.release_started.wait()
    task.cancel()
    await asyncio.sleep(0)

    assert task.done() is False
    assert repository.release_completed is False
    repository.allow_release.set()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert repository.release_completed is True
    row = next(iter(repository._by_id.values()))
    assert row.status == "pending"
    assert row.claim_token is None


async def test_repeated_cancellation_definitively_settles_provider_cleanup() -> None:
    repository = mutation_module._EphemeralMutationRepository()
    coordinator = WorkItemMutationService(repository, ephemeral_repository=repository)
    adapter = SlowCancellationCleanupAdapter()
    task = asyncio.create_task(
        coordinator.create(
            adapter=adapter,
            draft=WorkItemDraft(title="Provider cleanup"),
            identity=identity(),
            project_id="project-1",
            connection_id="connection-1",
            connection_persisted=False,
            connection_version=None,
            idempotency_key="provider-cleanup-cancel",
        )
    )
    await adapter.create_started.wait()
    task.cancel()
    await adapter.cleanup_started.wait()
    task.cancel()
    await asyncio.sleep(0)

    assert task.done() is False
    assert adapter.cleanup_completed is False
    adapter.allow_cleanup.set()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert adapter.cleanup_completed is True
    row = next(iter(repository._by_id.values()))
    assert row.status == "pending"
    assert row.claim_token is None


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


@pytest.mark.parametrize(
    ("provider", "connection_project", "mutation_project", "error"),
    [
        ("jira", "project-2", "project-1", "outside the mutation project"),
        ("jira", "project-1", None, "outside the mutation project"),
        ("jira", None, "project-1", "not bound to the mutation project"),
        ("jira", "project-1", "project-1", None),
        ("stub", None, "project-1", None),
        ("jira", None, None, None),
    ],
)
async def test_repository_reservation_enforces_connection_project_binding(
    provider: str,
    connection_project: str | None,
    mutation_project: str | None,
    error: str | None,
) -> None:
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
                    provider=provider,
                    name="scope-test",
                    project_id=connection_project,
                    enabled=True,
                    runtime_version=connection_version,
                )
            )
            await session.commit()

        repository = WorkItemMutationsRepository(maker)  # type: ignore[arg-type]
        payload = WorkItemDraft(title="Project bound").model_dump(mode="json")
        durable_payload = {"draft": payload}
        scope = MutationScope(
            tenant_scope=mutation_tenant_scope(mutation_project),
            consumer_id="consumer-1",
            connection_id="connection-1",
            operation="create",
            idempotency_key="project-binding",
        )
        reserve = repository.reserve(
            scope=scope,
            payload_hash=canonical_mutation_payload_hash(durable_payload),
            payload=durable_payload,
            target_key=None,
            project_id=mutation_project,
            connection_version=connection_version,
            fields_status="skipped",
            comment_status="skipped",
        )

        if error is not None:
            with pytest.raises(MutationConnectionChangedError, match=error):
                await reserve
            assert await repository.get_by_scope(scope) is None
        else:
            row = await reserve
            assert row.project_id == mutation_project
    finally:
        engine.dispose()


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
            tenant_scope=mutation_tenant_scope("project-1"),
            consumer_id="consumer-1",
            connection_id="connection-1",
            operation="create",
            idempotency_key="retire-me",
        )
        payload = {"draft": WorkItemDraft(title="Retire me").model_dump(mode="json")}
        payload_hash = canonical_mutation_payload_hash(payload)
        with pytest.raises(MutationConnectionChangedError):
            await repository.reserve(
                scope=scope,
                payload_hash=payload_hash,
                payload=payload,
                target_key=None,
                project_id="project-1",
                connection_version=connection_version - mutation_module.timedelta(seconds=1),
                fields_status="skipped",
                comment_status="skipped",
            )
        row = await repository.reserve(
            scope=scope,
            payload_hash=payload_hash,
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
        assert tombstone.payload_hash == payload_hash
        with pytest.raises(MutationRetiredError):
            await repository.reserve(
                scope=scope,
                payload_hash=payload_hash,
                payload=payload,
                target_key=None,
                project_id="project-1",
                connection_version=connection_version,
                fields_status="skipped",
                comment_status="skipped",
            )
        different_payload = {"draft": WorkItemDraft(title="Different").model_dump(mode="json")}
        with pytest.raises(MutationPayloadConflictError):
            await repository.reserve(
                scope=scope,
                payload_hash=canonical_mutation_payload_hash(different_payload),
                payload=different_payload,
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


async def test_reconciler_isolates_missing_claimed_changed_and_failed_rows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    version = mutation_module.datetime.now(mutation_module.UTC)
    ids = [
        "missing",
        "claimed-resolution",
        "non-persisted",
        "changed-version",
        "defer-claimed",
        "resume-claimed",
        "resume-failed",
        "success",
    ]
    rows = {
        mutation_id: SimpleNamespace(
            id=mutation_id,
            connection_id=f"connection-{mutation_id}",
            project_id="project-1",
            connection_version=version,
        )
        for mutation_id in ids
        if mutation_id != "missing"
    }
    deferred: list[str] = []
    closed: list[str] = []
    heartbeats = 0

    class Repository:
        async def get(self, mutation_id: str) -> Any:
            return rows.get(mutation_id)

    class Service:
        _repository = Repository()

        async def ready_ids(self) -> list[str]:
            return ids

        async def defer_resolution(self, mutation_id: str, _error: str) -> None:
            if mutation_id == "defer-claimed":
                raise MutationClaimedError("lease changed")
            deferred.append(mutation_id)

        async def resume(self, mutation_id: str, _adapter: Any) -> WorkItem:
            if mutation_id == "resume-claimed":
                raise MutationClaimedError("lease changed")
            if mutation_id == "resume-failed":
                raise RuntimeError("provider unavailable")
            return WorkItem(key="PHX-1", title="reconciled")

        async def retire_terminal(self) -> int:
            raise RuntimeError("retirement temporarily unavailable")

    class Resolver:
        async def resolve_with_metadata(
            self, _kind: Any, *, connection_id: str, project_id: str
        ) -> Any:
            del project_id
            mutation_id = connection_id.removeprefix("connection-")
            if mutation_id == "claimed-resolution":
                raise MutationClaimedError("another worker owns it")
            if mutation_id == "defer-claimed":
                raise RuntimeError("secret store unavailable")
            persisted = mutation_id != "non-persisted"
            resolved_version = (
                version + mutation_module.timedelta(seconds=1)
                if mutation_id == "changed-version"
                else version
            )
            return SimpleNamespace(
                persisted=persisted,
                connection_version=resolved_version,
                adapter=SimpleNamespace(provider=mutation_id),
            )

    async def close(adapter: Any) -> None:
        closed.append(adapter.provider)

    def heartbeat() -> None:
        nonlocal heartbeats
        heartbeats += 1

    monkeypatch.setattr(mutation_module, "close_adapter", close)

    reconciled = await reconcile_work_item_mutations_once(
        service=cast(Any, Service()),
        resolver=cast(Any, Resolver()),
        heartbeat=heartbeat,
    )

    assert reconciled == 1
    assert deferred == ["non-persisted", "changed-version"]
    assert closed == [
        "non-persisted",
        "changed-version",
        "resume-claimed",
        "resume-failed",
        "success",
    ]
    assert heartbeats == len(ids) + 1


async def test_work_item_reconciler_loop_logs_failure_then_honors_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stop = asyncio.Event()
    heartbeats = 0

    async def reconcile(*, heartbeat: Any = None) -> int:
        del heartbeat
        raise RuntimeError("temporary database failure")

    async def timeout_once(awaitable: Any, **kwargs: float) -> None:
        assert kwargs["timeout"] == mutation_module.RECONCILE_INTERVAL_S
        awaitable.close()
        stop.set()
        raise TimeoutError

    def heartbeat() -> None:
        nonlocal heartbeats
        heartbeats += 1

    monkeypatch.setattr(mutation_module, "reconcile_work_item_mutations_once", reconcile)
    monkeypatch.setattr(mutation_module.asyncio, "wait_for", timeout_once)

    await mutation_module.run_work_item_mutation_reconciler(stop, heartbeat)

    assert heartbeats == 1

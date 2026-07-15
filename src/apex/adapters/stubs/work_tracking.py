"""Stub work tracking: canned backlog with stable keys (PHX-241 is the demo story)."""

import hashlib

from apex.adapters.registry import AdapterRegistry, ConnectionConfig, PortKind
from apex.domain.integrations import (
    Enrichment,
    Page,
    QueryContext,
    SecretValue,
    TranslatedQuery,
    WorkItem,
    WorkItemDraft,
    WorkItemFilters,
    WorkItemPage,
)
from apex.ports.work_tracking import WorkTrackingMutationTargetNotFoundError

_TRACKER_URL = "https://tracker.stub.local/browse"

_BACKLOG: tuple[WorkItem, ...] = (
    WorkItem(
        key="PHX-241",
        title="Checkout latency p95 regression after payment-svc 2.14 rollout",
        kind="story",
        status="open",
        description=(
            "Checkout latency p95 regression: p95 rose from 220ms to 870ms on the "
            "checkout flow after the payment-svc 2.14 rollout. Reproduce under load "
            "and identify the bottleneck before the next release train. "
            "SLA: p95 <= 250ms at 200 vusers, error rate < 0.5%."
        ),
        url=f"{_TRACKER_URL}/PHX-241",
    ),
    WorkItem(
        key="PHX-187",
        title="Establish baseline load profile for catalog browse",
        kind="task",
        status="done",
        description="500-vuser browse-only baseline for the catalog service (reference run).",
        url=f"{_TRACKER_URL}/PHX-187",
    ),
    WorkItem(
        key="PHX-302",
        title="Soak test cart service for memory growth",
        kind="story",
        status="open",
        description="4h soak at steady 200 vusers; watch RSS growth on cart-svc pods.",
        url=f"{_TRACKER_URL}/PHX-302",
    ),
)


def _matches(item: WorkItem, filters: WorkItemFilters) -> bool:
    if filters.status and item.status != filters.status:
        return False
    if filters.kind and item.kind != filters.kind:
        return False
    if filters.text:
        haystack = f"{item.title}\n{item.description}".lower()
        if filters.text.lower() not in haystack:
            return False
    return True


def _paginate(items: list[WorkItem], page: Page) -> WorkItemPage:
    window = items[page.offset : page.offset + page.limit]
    return WorkItemPage(items=window, total=len(items), page=page)


@AdapterRegistry.register(PortKind.WORK_TRACKING, "stub")
class StubWorkTrackingAdapter:
    provider = "stub"

    def __init__(
        self, conn: ConnectionConfig | None = None, secret: SecretValue | None = None
    ) -> None:
        self._conn = conn
        self._created_by_marker: dict[str, WorkItem] = {}
        self._comment_markers: set[tuple[str, str]] = set()

    async def translate_query(
        self, natural_language: str, *, context: QueryContext
    ) -> TranslatedQuery:
        return TranslatedQuery(
            provider="stub", query=f'text ~ "{natural_language}"', confidence=0.9
        )

    async def execute_query(self, query: TranslatedQuery, *, page: Page) -> WorkItemPage:
        return _paginate([item.model_copy(deep=True) for item in _BACKLOG], page)

    async def get_item(self, key: str) -> WorkItem:
        for item in _BACKLOG:
            if item.key == key:
                return item.model_copy(deep=True)
        raise WorkTrackingMutationTargetNotFoundError(
            f"work item {key!r} not found in stub backlog"
        )

    async def list_items(self, filters: WorkItemFilters, *, page: Page) -> WorkItemPage:
        matched = [item.model_copy(deep=True) for item in _BACKLOG if _matches(item, filters)]
        return _paginate(matched, page)

    async def create_item(self, draft: WorkItemDraft) -> WorkItem:
        # Key derived from the title so re-execution after crash recovery is idempotent.
        suffix = int(hashlib.sha256(draft.title.encode()).hexdigest()[:4], 16) % 1000
        key = f"PHX-{9000 + suffix}"
        return WorkItem(
            key=key,
            title=draft.title,
            kind=draft.kind,
            status="open",
            description=draft.description,
            url=f"{_TRACKER_URL}/{key}",
        )

    async def find_item_by_idempotency_marker(self, marker: str) -> WorkItem | None:
        item = self._created_by_marker.get(marker)
        return item.model_copy(deep=True) if item is not None else None

    async def create_item_idempotent(self, draft: WorkItemDraft, *, marker: str) -> WorkItem:
        existing = await self.find_item_by_idempotency_marker(marker)
        if existing is not None:
            return existing
        item = await self.create_item(draft)
        self._created_by_marker[marker] = item.model_copy(deep=True)
        return item

    async def update_item_fields_idempotent(self, key: str, fields: dict[str, object]) -> None:
        await self.enrich_item(key, Enrichment(fields=fields))

    async def has_comment_idempotency_marker(self, key: str, marker: str) -> bool:
        await self.get_item(key)
        return (key, marker) in self._comment_markers

    async def add_item_comment_idempotent(self, key: str, comment: str, *, marker: str) -> None:
        await self.enrich_item(key, Enrichment(comment=comment))
        self._comment_markers.add((key, marker))

    async def enrich_item(self, key: str, enrichment: Enrichment) -> WorkItem:
        item = await self.get_item(key)
        update: dict[str, str] = {}
        if enrichment.comment:
            update["description"] = (
                f"{item.description}\n\n[enrichment] {enrichment.comment}".strip()
            )
        status = enrichment.fields.get("status")
        if isinstance(status, str):
            update["status"] = status
        return item.model_copy(update=update)

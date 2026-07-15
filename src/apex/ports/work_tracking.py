"""Work-tracking port (Jira / Azure DevOps / stub). Provider differences —
JQL vs WIQL, field naming — live entirely inside adapters."""

from typing import Protocol, runtime_checkable

from apex.domain.integrations import (
    Enrichment,
    Page,
    QueryContext,
    TranslatedQuery,
    WorkItem,
    WorkItemDraft,
    WorkItemFilters,
    WorkItemPage,
)


class WorkTrackingMutationRejectedError(ValueError):
    """Provider definitively rejected a mutation before applying it."""


class WorkTrackingMutationTargetNotFoundError(KeyError):
    """Provider definitively rejected a mutation because its target is absent."""


@runtime_checkable
class WorkTrackingPort(Protocol):
    async def translate_query(
        self, natural_language: str, *, context: QueryContext
    ) -> TranslatedQuery: ...

    async def execute_query(self, query: TranslatedQuery, *, page: Page) -> WorkItemPage: ...

    async def get_item(self, key: str) -> WorkItem: ...

    async def list_items(self, filters: WorkItemFilters, *, page: Page) -> WorkItemPage: ...

    async def create_item(self, draft: WorkItemDraft) -> WorkItem: ...

    async def enrich_item(self, key: str, enrichment: Enrichment) -> WorkItem: ...


@runtime_checkable
class IdempotentWorkTrackingMutationPort(Protocol):
    """Provider hooks used by the durable mutation coordinator.

    Create and comment operations are not naturally idempotent, so adapters
    must persist and reconcile the supplied provider marker. Field updates are
    exact set/upsert operations and can therefore be retried independently.
    """

    async def get_item(self, key: str) -> WorkItem: ...

    async def find_item_by_idempotency_marker(self, marker: str) -> WorkItem | None: ...

    async def create_item_idempotent(self, draft: WorkItemDraft, *, marker: str) -> WorkItem: ...

    async def update_item_fields_idempotent(self, key: str, fields: dict[str, object]) -> None: ...

    async def has_comment_idempotency_marker(self, key: str, marker: str) -> bool: ...

    async def add_item_comment_idempotent(self, key: str, comment: str, *, marker: str) -> None: ...

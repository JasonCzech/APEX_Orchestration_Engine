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

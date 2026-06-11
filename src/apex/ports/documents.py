"""Document retrieval port (uploaded docs / knowledge sources / stub)."""

from typing import Protocol, runtime_checkable

from apex.domain.integrations import DocHit, DocRef, DocScope, DocumentContent


@runtime_checkable
class DocumentRetrievalPort(Protocol):
    async def search(self, query: str, *, scope: DocScope, k: int = 10) -> list[DocHit]: ...

    async def fetch(self, ref: DocRef) -> DocumentContent: ...

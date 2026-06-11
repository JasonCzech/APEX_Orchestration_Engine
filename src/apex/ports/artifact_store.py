"""Artifact store port. Bytes never enter graph state — only refs/URIs do."""

from typing import Protocol, runtime_checkable

from pydantic import BaseModel


class StoredArtifact(BaseModel):
    key: str
    uri: str
    size: int


@runtime_checkable
class ArtifactStorePort(Protocol):
    async def put(self, key: str, data: bytes, *, content_type: str) -> StoredArtifact: ...

    async def get(self, key: str) -> bytes: ...

    async def get_url(self, key: str, *, ttl_s: int = 3600) -> str: ...

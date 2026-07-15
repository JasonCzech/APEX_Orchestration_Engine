"""Artifact store port. Bytes never enter graph state — only refs/URIs do."""

import re
from collections.abc import AsyncIterable, AsyncIterator
from hashlib import sha256
from pathlib import PurePath
from typing import Protocol, runtime_checkable

from pydantic import BaseModel


class StoredArtifact(BaseModel):
    key: str
    uri: str
    size: int


class ArtifactStoreBusyError(RuntimeError):
    """Artifact bytes cannot start streaming without exceeding provider capacity."""


def engine_artifact_namespace(idempotency_key: str) -> str:
    """Opaque, globally stable namespace for one internal engine attempt."""

    digest = sha256(idempotency_key.encode("utf-8")).hexdigest()
    return f"engine-runs/{digest}"


def engine_artifact_key(idempotency_key: str, name: str) -> str:
    """Return a traversal-safe key beneath the attempt's private namespace."""

    # Preserve uniqueness prefixes added by adapters even when a provider name
    # contains path separators. Flatten the path instead of taking only basename.
    filename = "_".join(
        part for part in name.replace("\\", "/").split("/") if part not in {"", ".", ".."}
    ).strip()
    if filename in {"", ".", ".."}:
        filename = "artifact.bin"
    filename = filename[:512]
    return f"{engine_artifact_namespace(idempotency_key)}/{filename}"


def transcript_artifact_key(thread_id: str, phase: str, attempt: int) -> str:
    """Traversal-safe, replay-stable key for one phase-attempt transcript."""

    normalized_thread_id = thread_id.strip()
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,254}", normalized_thread_id):
        raise ValueError("thread_id is not safe for a transcript artifact key")
    safe_phase = PurePath(phase.replace("\\", "/")).name.strip() or "phase"
    return f"transcripts/{normalized_thread_id}/{safe_phase}/attempt-{attempt}.txt"


@runtime_checkable
class ArtifactStorePort(Protocol):
    async def put(self, key: str, data: bytes, *, content_type: str) -> StoredArtifact: ...

    async def put_stream(
        self,
        key: str,
        data: AsyncIterable[bytes],
        *,
        content_type: str,
        max_bytes: int,
    ) -> StoredArtifact: ...

    async def get(self, key: str) -> bytes: ...

    def iter_bytes(self, key: str, *, chunk_size: int = 64 * 1024) -> AsyncIterator[bytes]: ...

    async def get_url(self, key: str, *, ttl_s: int = 3600) -> str: ...


class ArtifactDeletePort(Protocol):
    async def delete(self, key: str) -> None: ...

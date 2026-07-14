"""In-memory artifact store (provider "stub").

The bucket is a class-level dict so every instance in the process shares one
store — mirroring a shared object store so graph nodes and routers see the same
artifacts. Process-local by design; the MinIO/S3 adapter lands in M3.
"""

from collections.abc import AsyncIterable, AsyncIterator
from typing import ClassVar

from apex.adapters.registry import AdapterRegistry, ConnectionConfig, PortKind
from apex.domain.integrations import SecretValue
from apex.ports.artifact_store import StoredArtifact


@AdapterRegistry.register(PortKind.ARTIFACT_STORE, "stub")
class MemoryArtifactStore:
    _objects: ClassVar[dict[str, tuple[bytes, str]]] = {}

    def __init__(
        self, conn: ConnectionConfig | None = None, secret: SecretValue | None = None
    ) -> None:
        self._conn = conn

    async def put(self, key: str, data: bytes, *, content_type: str) -> StoredArtifact:
        self._objects[key] = (bytes(data), content_type)
        return StoredArtifact(key=key, uri=f"memory://{key}", size=len(data))

    async def put_stream(
        self,
        key: str,
        data: AsyncIterable[bytes],
        *,
        content_type: str,
        max_bytes: int,
    ) -> StoredArtifact:
        payload = bytearray()
        async for chunk in data:
            if len(payload) + len(chunk) > max_bytes:
                raise ValueError(f"artifact exceeds maximum size of {max_bytes} bytes")
            payload.extend(chunk)
        return await self.put(key, bytes(payload), content_type=content_type)

    async def get(self, key: str) -> bytes:
        try:
            return self._objects[key][0]
        except KeyError:
            raise KeyError(f"artifact {key!r} not found in memory store") from None

    async def delete(self, key: str) -> None:
        self._objects.pop(key, None)

    async def iter_bytes(self, key: str, *, chunk_size: int = 64 * 1024) -> AsyncIterator[bytes]:
        if chunk_size < 1:
            raise ValueError("chunk_size must be >= 1")
        data = await self.get(key)
        for offset in range(0, len(data), chunk_size):
            yield data[offset : offset + chunk_size]

    async def get_url(self, key: str, *, ttl_s: int = 3600) -> str:
        if key not in self._objects:
            raise KeyError(f"artifact {key!r} not found in memory store")
        return f"memory://{key}"

    @classmethod
    def clear(cls) -> None:
        """Test helper: empty the shared bucket."""
        cls._objects.clear()

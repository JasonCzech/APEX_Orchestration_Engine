"""In-memory artifact store (provider "stub").

The bucket is a class-level dict so every instance in the process shares one
store — mirroring a shared object store so graph nodes and routers see the same
artifacts. Process-local by design; the MinIO/S3 adapter lands in M3.
"""

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

    async def get(self, key: str) -> bytes:
        try:
            return self._objects[key][0]
        except KeyError:
            raise KeyError(f"artifact {key!r} not found in memory store") from None

    async def get_url(self, key: str, *, ttl_s: int = 3600) -> str:
        if key not in self._objects:
            raise KeyError(f"artifact {key!r} not found in memory store")
        return f"memory://{key}"

    @classmethod
    def clear(cls) -> None:
        """Test helper: empty the shared bucket."""
        cls._objects.clear()

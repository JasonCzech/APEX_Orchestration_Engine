"""S3/MinIO artifact store (provider "s3", PortKind.ARTIFACT_STORE).

Backed by the synchronous `minio` SDK — every network call runs inside
asyncio.to_thread so the async port surface never blocks the event loop.

Connection options: {"endpoint": "localhost:9000", "bucket": "apex-artifacts",
"secure": false, "access_key": "apex"}; the secret key arrives as the
SecretValue that AdapterRegistry.build resolves from the connection's
secret_ref (e.g. "env:APEX_MINIO_SECRET_KEY" via the env secrets adapter).
"""

import asyncio
import threading
from datetime import timedelta
from io import BytesIO
from typing import Any

from minio import Minio
from minio.error import S3Error

from apex.adapters.registry import AdapterRegistry, ConnectionConfig, PortKind
from apex.domain.integrations import SecretValue
from apex.ports.artifact_store import StoredArtifact

DEFAULT_ENDPOINT = "localhost:9000"
DEFAULT_BUCKET = "apex-artifacts"

# make_bucket races between concurrent writers are benign — first writer wins.
_BUCKET_RACE_CODES = frozenset({"BucketAlreadyOwnedByYou", "BucketAlreadyExists"})


@AdapterRegistry.register(PortKind.ARTIFACT_STORE, "s3")
class S3ArtifactStore:
    """ArtifactStorePort against any S3-compatible object store (MinIO in dev).

    The bucket is ensured (created if missing, race-tolerant) at most once per
    adapter instance, lazily on first use — constructing the adapter never
    touches the network, so registry builds stay cheap. `client` exists for
    tests: inject a fake instead of constructing a real Minio client.
    """

    def __init__(
        self,
        conn: ConnectionConfig | None = None,
        secret: SecretValue | None = None,
        *,
        client: Any | None = None,
    ) -> None:
        options: dict[str, Any] = dict(conn.options) if conn is not None else {}
        self._bucket = str(options.get("bucket", DEFAULT_BUCKET))
        if client is None:
            if secret is None:
                conn_id = conn.id if conn is not None else "<none>"
                raise ValueError(
                    f"s3 artifact store connection {conn_id!r} requires a secret key; "
                    'set secret_ref on the connection (e.g. "env:APEX_MINIO_SECRET_KEY")'
                )
            client = Minio(
                str(options.get("endpoint", DEFAULT_ENDPOINT)),
                access_key=str(options.get("access_key", "")),
                secret_key=secret.value,
                secure=bool(options.get("secure", False)),
            )
        self._client = client
        self._bucket_ensured = False
        # threading.Lock (not asyncio.Lock): instances are cached process-wide by
        # the ConnectionResolver and may be used from more than one event loop.
        self._ensure_lock = threading.Lock()

    # ── port surface ──────────────────────────────────────────────────────────

    async def put(self, key: str, data: bytes, *, content_type: str) -> StoredArtifact:
        await self._ensure_bucket()
        payload = bytes(data)

        def _put() -> None:
            self._client.put_object(
                self._bucket,
                key,
                BytesIO(payload),
                length=len(payload),
                content_type=content_type,
            )

        await asyncio.to_thread(_put)
        return StoredArtifact(key=key, uri=f"s3://{self._bucket}/{key}", size=len(payload))

    async def get(self, key: str) -> bytes:
        await self._ensure_bucket()

        def _get() -> bytes:
            response = self._client.get_object(self._bucket, key)
            try:
                return response.read()
            finally:  # minio response hygiene: close + return the conn to the pool
                response.close()
                response.release_conn()

        try:
            return await asyncio.to_thread(_get)
        except S3Error as exc:
            if exc.code == "NoSuchKey":
                raise KeyError(f"artifact {key!r} not found in bucket {self._bucket!r}") from None
            raise

    async def get_url(self, key: str, *, ttl_s: int = 3600) -> str:
        """Presigned GET URL. Signing is local — no existence check, no network."""
        await self._ensure_bucket()
        return await asyncio.to_thread(
            self._client.presigned_get_object,
            self._bucket,
            key,
            expires=timedelta(seconds=ttl_s),
        )

    async def aclose(self) -> None:
        http_pool = getattr(self._client, "_http", None)
        clear = getattr(http_pool, "clear", None)
        if callable(clear):
            await asyncio.to_thread(clear)

    # ── bucket bootstrap ──────────────────────────────────────────────────────

    async def _ensure_bucket(self) -> None:
        if self._bucket_ensured:  # fast path: no thread hop after first success
            return
        await asyncio.to_thread(self._ensure_bucket_sync)

    def _ensure_bucket_sync(self) -> None:
        with self._ensure_lock:
            if self._bucket_ensured:
                return
            if not self._client.bucket_exists(self._bucket):
                try:
                    self._client.make_bucket(self._bucket)
                except S3Error as exc:
                    if exc.code not in _BUCKET_RACE_CODES:
                        raise
            self._bucket_ensured = True

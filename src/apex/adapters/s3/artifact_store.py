"""S3/MinIO artifact store (provider "s3", PortKind.ARTIFACT_STORE).

Backed by the synchronous `minio` SDK — every network call runs inside
asyncio.to_thread so the async port surface never blocks the event loop.

Connection options: {"endpoint": "localhost:9000", "bucket": "apex-artifacts",
"secure": false, "access_key": "apex"}; the secret key arrives as the
SecretValue that AdapterRegistry.build resolves from the connection's
secret_ref (e.g. "env:APEX_INTEGRATION_MINIO_SECRET_KEY" via the env secrets adapter).
"""

import asyncio
import os
import threading
from collections.abc import AsyncIterable, AsyncIterator
from datetime import timedelta
from io import BytesIO
from tempfile import SpooledTemporaryFile
from typing import Any

import certifi
from minio import Minio
from minio.error import S3Error
from urllib3 import Retry, Timeout

from apex.adapters.network_safety import SafePoolManager, private_hosts_allowed
from apex.adapters.registry import AdapterRegistry, ConnectionConfig, PortKind
from apex.domain.integrations import SecretValue
from apex.ports.artifact_store import StoredArtifact

DEFAULT_ENDPOINT = "localhost:9000"
DEFAULT_BUCKET = "apex-artifacts"
STREAM_SPOOL_BYTES = 8 * 1024 * 1024

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
                    "set secret_ref on the connection "
                    '(e.g. "env:APEX_INTEGRATION_MINIO_SECRET_KEY")'
                )
            timeout = 5 * 60
            http_client = SafePoolManager(
                allow_private_hosts=private_hosts_allowed(options),
                timeout=Timeout(connect=timeout, read=timeout),
                maxsize=10,
                cert_reqs="CERT_REQUIRED",
                ca_certs=os.environ.get("SSL_CERT_FILE") or certifi.where(),
                retries=Retry(
                    total=5,
                    backoff_factor=0.2,
                    status_forcelist=[500, 502, 503, 504],
                ),
            )
            client = Minio(
                str(options.get("endpoint", DEFAULT_ENDPOINT)),
                access_key=str(options.get("access_key", "")),
                secret_key=secret.value,
                secure=bool(options.get("secure", False)),
                http_client=http_client,
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

    async def put_stream(
        self,
        key: str,
        data: AsyncIterable[bytes],
        *,
        content_type: str,
        max_bytes: int,
    ) -> StoredArtifact:
        """Bounded streaming upload, spilling larger payloads to a temporary file."""
        await self._ensure_bucket()
        size = 0
        with SpooledTemporaryFile(max_size=STREAM_SPOOL_BYTES, mode="w+b") as payload:
            async for chunk in data:
                size += len(chunk)
                if size > max_bytes:
                    raise ValueError(f"artifact exceeds maximum size of {max_bytes} bytes")
                payload.write(chunk)
            payload.seek(0)

            def _put() -> None:
                self._client.put_object(
                    self._bucket,
                    key,
                    payload,
                    length=size,
                    content_type=content_type,
                )

            await asyncio.to_thread(_put)
        return StoredArtifact(key=key, uri=f"s3://{self._bucket}/{key}", size=size)

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

    async def iter_bytes(self, key: str, *, chunk_size: int = 64 * 1024) -> AsyncIterator[bytes]:
        if chunk_size < 1:
            raise ValueError("chunk_size must be >= 1")
        await self._ensure_bucket()
        try:
            response = await asyncio.to_thread(self._client.get_object, self._bucket, key)
        except S3Error as exc:
            if exc.code == "NoSuchKey":
                raise KeyError(f"artifact {key!r} not found in bucket {self._bucket!r}") from None
            raise
        try:
            while chunk := await asyncio.to_thread(response.read, chunk_size):
                yield chunk
        finally:
            response.close()
            response.release_conn()

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

"""S3/MinIO artifact store (provider "s3", PortKind.ARTIFACT_STORE).

Backed by the synchronous `minio` SDK — blocking calls run in isolated,
bounded executors so the async port surface never blocks the event loop or
starves unrelated default-executor work.

Connection options: {"endpoint": "localhost:9000", "bucket": "apex-artifacts",
"secure": false, "access_key": "apex"}; the secret key arrives as the
SecretValue that AdapterRegistry.build resolves from the connection's
secret_ref (e.g. "env:APEX_INTEGRATION_MINIO_SECRET_KEY" via the env secrets adapter).
"""

import asyncio
import os
import re
import threading
from collections.abc import AsyncIterable, AsyncIterator, Callable
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from datetime import timedelta
from functools import partial
from io import BytesIO
from tempfile import SpooledTemporaryFile
from typing import Any, cast
from urllib.parse import quote

import certifi
from minio import Minio
from minio.error import S3Error
from urllib3 import Retry, Timeout

from apex.adapters.network_safety import SafePoolManager, private_hosts_allowed
from apex.adapters.options import (
    coerce_bool,
    normalize_host_port_endpoint,
    require_bounded_credential,
)
from apex.adapters.registry import AdapterRegistry, ConnectionConfig, PortKind
from apex.domain.integrations import SecretValue
from apex.ports.artifact_store import ArtifactStoreBusyError, StoredArtifact

DEFAULT_ENDPOINT = "localhost:9000"
DEFAULT_BUCKET = "apex-artifacts"
STREAM_SPOOL_BYTES = 8 * 1024 * 1024
STREAM_WORKER_LIMIT = 8
SDK_WORKER_LIMIT = 8
MAX_PROCESS_SPOOL_BYTES = 1024 * 1024 * 1024
MAX_DIRECT_GET_BYTES = 64 * 1024 * 1024
_BUCKET_NAME = re.compile(r"[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]")

# make_bucket races between concurrent writers are benign — first writer wins.
_BUCKET_RACE_CODES = frozenset({"BucketAlreadyOwnedByYou", "BucketAlreadyExists"})

_STREAM_EXECUTOR_LOCK = threading.Lock()
_STREAM_EXECUTOR: ThreadPoolExecutor | None = None
_STREAM_EXECUTOR_PID: int | None = None
_SDK_EXECUTOR_LOCK = threading.Lock()
_SDK_EXECUTOR: ThreadPoolExecutor | None = None
_SDK_EXECUTOR_PID: int | None = None


class _ProcessWorkerAdmission:
    """Cross-event-loop admission that prevents executor queue overcommit."""

    def __init__(self, limit: int) -> None:
        self.limit = limit
        self._available = limit
        self._pid = os.getpid()
        self._lock = threading.Lock()
        self._waiters: set[tuple[asyncio.AbstractEventLoop, asyncio.Event]] = set()

    def _reset_after_fork(self) -> None:
        pid = os.getpid()
        if pid != self._pid:
            self._pid = pid
            self._available = self.limit
            self._waiters.clear()

    async def acquire(self) -> None:
        loop = asyncio.get_running_loop()
        event = asyncio.Event()
        waiter = (loop, event)
        try:
            while True:
                with self._lock:
                    self._reset_after_fork()
                    if self._available:
                        self._available -= 1
                        self._waiters.discard(waiter)
                        return
                    self._waiters.add(waiter)
                await event.wait()
                event.clear()
        except BaseException:
            with self._lock:
                self._waiters.discard(waiter)
            raise

    def try_acquire(self) -> bool:
        """Acquire immediately or fail without creating an unbounded waiter."""

        with self._lock:
            self._reset_after_fork()
            if self._available < 1:
                return False
            self._available -= 1
            return True

    def release(self) -> None:
        with self._lock:
            self._reset_after_fork()
            self._available += 1
            if self._available > self.limit:
                self._available -= 1
                raise RuntimeError("S3 worker admission released without ownership")
            waiters = tuple(self._waiters)
        for loop, event in waiters:
            try:
                loop.call_soon_threadsafe(event.set)
            except RuntimeError:
                with self._lock:
                    self._waiters.discard((loop, event))


_STREAM_ADMISSION = _ProcessWorkerAdmission(STREAM_WORKER_LIMIT)
_SDK_ADMISSION = _ProcessWorkerAdmission(SDK_WORKER_LIMIT)


class _ProcessSpoolBudget:
    """Cross-event-loop byte reservation for temporary upload spools."""

    def __init__(self, limit: int) -> None:
        self.limit = limit
        self._used = 0
        self._lock = threading.Lock()
        self._waiters: set[tuple[asyncio.AbstractEventLoop, asyncio.Event]] = set()

    async def acquire(self, amount: int) -> None:
        loop = asyncio.get_running_loop()
        event = asyncio.Event()
        waiter = (loop, event)
        try:
            while True:
                with self._lock:
                    if amount <= self.limit - self._used:
                        self._used += amount
                        self._waiters.discard(waiter)
                        return
                    self._waiters.add(waiter)
                await event.wait()
                event.clear()
        except BaseException:
            with self._lock:
                self._waiters.discard(waiter)
            raise

    def release(self, amount: int) -> None:
        with self._lock:
            self._used -= amount
            if self._used < 0:
                raise RuntimeError("S3 spool budget released more bytes than reserved")
            waiters = tuple(self._waiters)
        for loop, event in waiters:
            try:
                loop.call_soon_threadsafe(event.set)
            except RuntimeError:
                # A cancelled worker loop can close before another loop releases.
                with self._lock:
                    self._waiters.discard((loop, event))


_PROCESS_SPOOL_BUDGET = _ProcessSpoolBudget(MAX_PROCESS_SPOOL_BYTES)


def _stream_executor() -> ThreadPoolExecutor:
    """One isolated bounded pool per process; never consume default-executor workers."""

    global _STREAM_EXECUTOR, _STREAM_EXECUTOR_PID
    pid = os.getpid()
    with _STREAM_EXECUTOR_LOCK:
        if _STREAM_EXECUTOR is None or _STREAM_EXECUTOR_PID != pid:
            _STREAM_EXECUTOR = ThreadPoolExecutor(
                max_workers=STREAM_WORKER_LIMIT,
                thread_name_prefix="apex-s3-stream",
            )
            _STREAM_EXECUTOR_PID = pid
        return _STREAM_EXECUTOR


def _sdk_executor() -> ThreadPoolExecutor:
    """Isolate non-streaming SDK calls from streams and asyncio's default pool."""

    global _SDK_EXECUTOR, _SDK_EXECUTOR_PID
    pid = os.getpid()
    with _SDK_EXECUTOR_LOCK:
        if _SDK_EXECUTOR is None or _SDK_EXECUTOR_PID != pid:
            _SDK_EXECUTOR = ThreadPoolExecutor(
                max_workers=SDK_WORKER_LIMIT,
                thread_name_prefix="apex-s3-sdk",
            )
            _SDK_EXECUTOR_PID = pid
        return _SDK_EXECUTOR


@asynccontextmanager
async def _reserve_spool(max_bytes: int) -> AsyncIterator[None]:
    """Reserve worst-case temporary bytes before consuming an upload stream."""

    if max_bytes < 0:
        raise ValueError("max_bytes must be non-negative")
    if max_bytes > _PROCESS_SPOOL_BUDGET.limit:
        raise ValueError(
            f"artifact stream limit exceeds process spool budget of "
            f"{_PROCESS_SPOOL_BUDGET.limit} bytes"
        )
    # The cross-loop event waiter consumes no executor thread while queued and
    # remains immediately cancellable.
    await _PROCESS_SPOOL_BUDGET.acquire(max_bytes)
    try:
        yield
    finally:
        _PROCESS_SPOOL_BUDGET.release(max_bytes)


async def _await_worker_definitively[T](
    future: asyncio.Future[T],
) -> tuple[T | None, BaseException | None, bool]:
    """Wait through every caller cancellation and always retrieve worker outcome."""

    cancelled = False
    while not future.done():
        try:
            await asyncio.shield(future)
        except asyncio.CancelledError:
            cancelled = True
        except BaseException:
            # Executor exceptions are retrieved below, preventing an unobserved
            # future while preserving cancellation precedence.
            break
    try:
        return future.result(), None, cancelled
    except BaseException as exc:  # includes an executor-shutdown cancellation
        return None, exc, cancelled


async def _run_stream_worker[T](
    func: Callable[..., T],
    /,
    *args: Any,
    cleanup_cancelled_result: Callable[[T], Any] | None = None,
) -> T:
    """Run one blocking SDK operation without abandoning it on cancellation.

    When opening an object is cancelled, ``cleanup_cancelled_result`` closes the
    response returned by the now-finished worker before cancellation propagates.
    """

    loop = asyncio.get_running_loop()
    future = loop.run_in_executor(_stream_executor(), partial(func, *args))
    result, error, cancelled = await _await_worker_definitively(future)
    if error is not None:
        if cancelled:
            raise asyncio.CancelledError from None
        raise error
    typed_result = cast(T, result)
    if cancelled and cleanup_cancelled_result is not None:
        cleanup_future = loop.run_in_executor(
            _stream_executor(), partial(cleanup_cancelled_result, typed_result)
        )
        # Cancellation still wins, but the cleanup itself is also definitive and
        # its outcome is retrieved before returning control to the caller.
        await _await_worker_definitively(cleanup_future)
    if cancelled:
        raise asyncio.CancelledError
    return typed_result


async def _run_sdk_worker[T](func: Callable[..., T], /, *args: Any, **kwargs: Any) -> T:
    """Run one non-streaming SDK call under bounded definitive ownership."""

    await _SDK_ADMISSION.acquire()
    try:
        loop = asyncio.get_running_loop()
        future = loop.run_in_executor(_sdk_executor(), partial(func, *args, **kwargs))
        result, error, cancelled = await _await_worker_definitively(future)
        if error is not None:
            if cancelled:
                raise asyncio.CancelledError from None
            raise error
        if cancelled:
            raise asyncio.CancelledError
        return cast(T, result)
    finally:
        _SDK_ADMISSION.release()


def _close_object_response(response: Any) -> None:
    try:
        response.close()
    finally:
        response.release_conn()


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
        raw_bucket = options.get("bucket", DEFAULT_BUCKET)
        if not isinstance(raw_bucket, str) or _BUCKET_NAME.fullmatch(raw_bucket) is None:
            raise ValueError("s3 bucket must be a valid 3-63 character DNS-style name")
        self._bucket = raw_bucket
        if client is None:
            if secret is None:
                conn_id = conn.id if conn is not None else "<none>"
                raise ValueError(
                    f"s3 artifact store connection {conn_id!r} requires a secret key; "
                    "set secret_ref on the connection "
                    '(e.g. "env:APEX_INTEGRATION_MINIO_SECRET_KEY")'
                )
            raw_endpoint = options.get("endpoint", DEFAULT_ENDPOINT)
            endpoint, secure = normalize_host_port_endpoint(
                raw_endpoint,
                secure=coerce_bool(options.get("secure"), default=False),
            )
            access_key = require_bounded_credential(
                options.get("access_key", ""),
                label="s3 access key",
                max_bytes=1_024,
            )
            secret_key = require_bounded_credential(
                secret.value,
                label="s3 secret key",
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
                endpoint,
                access_key=access_key,
                secret_key=secret_key,
                secure=secure,
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

        provider_failed = False
        try:
            await _run_sdk_worker(_put)
        except S3Error:
            provider_failed = True
        if provider_failed:
            raise RuntimeError("S3 artifact upload failed")
        encoded_key = quote(key, safe="/-._~")
        return StoredArtifact(
            key=key,
            uri=f"s3://{self._bucket}/{encoded_key}",
            size=len(payload),
        )

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
        async with _reserve_spool(max_bytes):
            size = 0
            with SpooledTemporaryFile(max_size=STREAM_SPOOL_BYTES, mode="w+b") as payload:
                async for chunk in data:
                    if len(chunk) > max_bytes - size:
                        raise ValueError(f"artifact exceeds maximum size of {max_bytes} bytes")
                    size += len(chunk)
                    # SpooledTemporaryFile rolls over to disk after the in-memory
                    # threshold. Keep both that rollover and subsequent filesystem
                    # writes off the event loop under the same bounded SDK pool.
                    await _run_sdk_worker(payload.write, chunk)
                await _run_sdk_worker(payload.seek, 0)

                def _put() -> None:
                    self._client.put_object(
                        self._bucket,
                        key,
                        payload,
                        length=size,
                        content_type=content_type,
                    )

                # Cancellation waits for the worker before this context closes
                # the spool or releases its process-wide byte reservation.
                provider_failed = False
                try:
                    await _run_sdk_worker(_put)
                except S3Error:
                    provider_failed = True
                if provider_failed:
                    raise RuntimeError("S3 artifact upload failed")
        encoded_key = quote(key, safe="/-._~")
        return StoredArtifact(
            key=key,
            uri=f"s3://{self._bucket}/{encoded_key}",
            size=size,
        )

    async def get(self, key: str) -> bytes:
        await self._ensure_bucket()

        def _get() -> bytes:
            response = self._client.get_object(self._bucket, key)
            try:
                payload = bytearray()
                while True:
                    remaining = MAX_DIRECT_GET_BYTES + 1 - len(payload)
                    if remaining <= 0:
                        raise ValueError(
                            "artifact exceeds the maximum direct-read size; use iter_bytes instead"
                        )
                    chunk = response.read(min(1024 * 1024, remaining))
                    if not chunk:
                        return bytes(payload)
                    if not isinstance(chunk, bytes) or len(chunk) > remaining:
                        raise RuntimeError("S3 get_object returned an invalid response chunk")
                    payload.extend(chunk)
            finally:  # minio response hygiene: close + return the conn to the pool
                response.close()
                response.release_conn()

        payload: bytes | None = None
        missing = False
        provider_failed = False
        try:
            payload = await _run_sdk_worker(_get)
        except S3Error as exc:
            if exc.code == "NoSuchKey":
                missing = True
            else:
                provider_failed = True
        if missing:
            raise KeyError(f"artifact {key!r} not found in bucket {self._bucket!r}")
        if provider_failed:
            raise RuntimeError("S3 artifact download failed")
        if payload is None:  # pragma: no cover - SDK contract invariant
            raise RuntimeError("S3 get_object returned no payload")
        return payload

    async def delete(self, key: str) -> None:
        await self._ensure_bucket()
        provider_failed = False
        try:
            await _run_sdk_worker(self._client.remove_object, self._bucket, key)
        except S3Error:
            provider_failed = True
        if provider_failed:
            raise RuntimeError("S3 artifact deletion failed")

    async def iter_bytes(self, key: str, *, chunk_size: int = 64 * 1024) -> AsyncIterator[bytes]:
        if chunk_size < 1:
            raise ValueError("chunk_size must be >= 1")
        if not _STREAM_ADMISSION.try_acquire():
            raise ArtifactStoreBusyError("artifact streaming capacity is busy")
        response: Any | None = None
        try:
            if not self._bucket_ensured:
                await _run_stream_worker(self._ensure_bucket_sync)
            missing = False
            provider_failed = False
            try:
                response = await _run_stream_worker(
                    self._client.get_object,
                    self._bucket,
                    key,
                    cleanup_cancelled_result=_close_object_response,
                )
                if response is None:
                    raise RuntimeError("S3 get_object returned no response")
            except S3Error as exc:
                if exc.code == "NoSuchKey":
                    missing = True
                else:
                    provider_failed = True
            if missing:
                raise KeyError(f"artifact {key!r} not found in bucket {self._bucket!r}")
            if provider_failed:
                raise RuntimeError("S3 artifact download failed")
            if response is None:  # pragma: no cover - SDK contract invariant
                raise RuntimeError("S3 get_object returned no response")
            while True:
                chunk = await _run_stream_worker(response.read, chunk_size)
                if not chunk:
                    break
                yield chunk
        finally:
            try:
                if response is not None:
                    # Every read above reaches a definitive worker outcome before
                    # control arrives here, so close/release can never race read().
                    await _run_stream_worker(_close_object_response, response)
            finally:
                _STREAM_ADMISSION.release()

    async def get_url(self, key: str, *, ttl_s: int = 3600) -> str:
        """Presigned GET URL. Signing is local — no existence check, no network."""
        await self._ensure_bucket()
        url: str | None = None
        provider_failed = False
        try:
            url = await _run_sdk_worker(
                self._client.presigned_get_object,
                self._bucket,
                key,
                expires=timedelta(seconds=ttl_s),
            )
        except S3Error:
            provider_failed = True
        if provider_failed:
            raise RuntimeError("S3 artifact URL signing failed")
        if url is None:  # pragma: no cover - SDK contract invariant
            raise RuntimeError("S3 artifact URL signing returned no URL")
        return url

    async def aclose(self) -> None:
        http_pool = getattr(self._client, "_http", None)
        clear = getattr(http_pool, "clear", None)
        if callable(clear):
            await _run_sdk_worker(clear)

    # ── bucket bootstrap ──────────────────────────────────────────────────────

    async def _ensure_bucket(self) -> None:
        if self._bucket_ensured:  # fast path: no thread hop after first success
            return
        await _run_sdk_worker(self._ensure_bucket_sync)

    def _ensure_bucket_sync(self) -> None:
        with self._ensure_lock:
            if self._bucket_ensured:
                return
            exists: bool | None = None
            provider_failed = False
            try:
                exists = self._client.bucket_exists(self._bucket)
            except S3Error:
                provider_failed = True
            if provider_failed:
                raise RuntimeError("S3 artifact bucket check failed")
            if exists is None:  # pragma: no cover - SDK contract invariant
                raise RuntimeError("S3 artifact bucket check returned no result")
            if not exists:
                bucket_race = False
                try:
                    self._client.make_bucket(self._bucket)
                except S3Error as exc:
                    bucket_race = exc.code in _BUCKET_RACE_CODES
                    provider_failed = not bucket_race
                if provider_failed:
                    raise RuntimeError("S3 artifact bucket creation failed")
            self._bucket_ensured = True

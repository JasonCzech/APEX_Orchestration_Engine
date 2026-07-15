"""S3/MinIO artifact store: fake-client unit coverage + env-gated MinIO round-trip.

Unit tests inject a FakeMinio through the adapter's `client` constructor param,
so no object store (and no network) is required. The integration tests at the
bottom run only with APEX_TEST_MINIO=1 against the dev MinIO at localhost:9000.
"""

import asyncio
import os
import threading
import uuid
from collections.abc import Buffer
from datetime import timedelta
from io import BytesIO
from typing import Any, cast

import pytest
from minio import Minio
from minio.error import S3Error

import apex.adapters.s3.artifact_store as s3_module
from apex.adapters.network_safety import SafePoolManager
from apex.adapters.registry import AdapterRegistry, ConnectionConfig, PortKind
from apex.adapters.s3 import S3ArtifactStore
from apex.adapters.stubs import EnvSecretsAdapter
from apex.domain.integrations import SecretValue
from apex.ports.artifact_store import ArtifactStoreBusyError, ArtifactStorePort

# --- fixtures / fakes ---------------------------------------------------------


def _conn(**option_overrides: Any) -> ConnectionConfig:
    options: dict[str, Any] = {
        "endpoint": "localhost:9000",
        "bucket": "apex-artifacts",
        "secure": False,
        "access_key": "apex",
    }
    options.update(option_overrides)
    return ConnectionConfig(
        id="conn-s3-test",
        kind=PortKind.ARTIFACT_STORE,
        provider="s3",
        name="minio-artifacts",
        options=options,
        secret_ref="env:APEX_INTEGRATION_MINIO_SECRET_KEY",
    )


def _s3_error(code: str, bucket: str, key: str) -> S3Error:
    return S3Error(
        cast(Any, None),  # response: unused by the adapter's error handling
        code=code,
        message=f"fake {code}",
        resource=f"/{bucket}/{key}",
        request_id="req-1",
        host_id="host-1",
        bucket_name=bucket,
        object_name=key or None,
    )


class FakeObjectResponse:
    """Mimics the urllib3 response returned by Minio.get_object."""

    def __init__(self, payload: bytes) -> None:
        self._payload = payload
        self.closed = False
        self.released = False

    def read(self, size: int = -1) -> bytes:
        if size < 0:
            payload, self._payload = self._payload, b""
            return payload
        payload, self._payload = self._payload[:size], self._payload[size:]
        return payload

    def close(self) -> None:
        self.closed = True

    def release_conn(self) -> None:
        self.released = True


class FakeMinio:
    """In-memory stand-in for minio.Minio covering the calls the adapter makes."""

    def __init__(self, *, existing_buckets: set[str] | None = None) -> None:
        self.buckets: set[str] = set(existing_buckets or set())
        self.objects: dict[tuple[str, str], tuple[bytes, str]] = {}
        self.bucket_exists_calls = 0
        self.make_bucket_calls = 0
        self.responses: list[FakeObjectResponse] = []

    def bucket_exists(self, bucket_name: str) -> bool:
        self.bucket_exists_calls += 1
        return bucket_name in self.buckets

    def make_bucket(self, bucket_name: str) -> None:
        self.make_bucket_calls += 1
        if bucket_name in self.buckets:
            raise _s3_error("BucketAlreadyOwnedByYou", bucket_name, "")
        self.buckets.add(bucket_name)

    def put_object(
        self,
        bucket_name: str,
        object_name: str,
        data: BytesIO,
        length: int,
        content_type: str = "application/octet-stream",
    ) -> None:
        assert bucket_name in self.buckets, "put_object before bucket ensure"
        self.objects[(bucket_name, object_name)] = (data.read(length), content_type)

    def get_object(self, bucket_name: str, object_name: str) -> FakeObjectResponse:
        try:
            payload, _ = self.objects[(bucket_name, object_name)]
        except KeyError:
            raise _s3_error("NoSuchKey", bucket_name, object_name) from None
        response = FakeObjectResponse(payload)
        self.responses.append(response)
        return response

    def remove_object(self, bucket_name: str, object_name: str) -> None:
        self.objects.pop((bucket_name, object_name), None)

    def presigned_get_object(
        self,
        bucket_name: str,
        object_name: str,
        expires: timedelta = timedelta(days=7),
    ) -> str:
        ttl = int(expires.total_seconds())
        return f"http://localhost:9000/{bucket_name}/{object_name}?X-Amz-Expires={ttl}"


class RacingFakeMinio(FakeMinio):
    """Simulates losing the create race: exists says no, create says taken."""

    def bucket_exists(self, bucket_name: str) -> bool:
        self.bucket_exists_calls += 1
        return False

    def make_bucket(self, bucket_name: str) -> None:
        self.make_bucket_calls += 1
        self.buckets.add(bucket_name)  # the other writer created it
        raise _s3_error("BucketAlreadyOwnedByYou", bucket_name, "")


# --- unit: round-trip + uri shape ----------------------------------------------


async def test_put_get_url_roundtrip_with_fake_client() -> None:
    fake = FakeMinio()
    store = S3ArtifactStore(_conn(), client=fake)

    stored = await store.put(
        "runs/r1/results.json", b'{"ok": true}', content_type="application/json"
    )
    assert stored.key == "runs/r1/results.json"
    assert stored.uri == "s3://apex-artifacts/runs/r1/results.json"
    assert stored.size == len(b'{"ok": true}')
    assert fake.objects[("apex-artifacts", "runs/r1/results.json")] == (
        b'{"ok": true}',
        "application/json",
    )

    assert await store.get("runs/r1/results.json") == b'{"ok": true}'

    url = await store.get_url("runs/r1/results.json", ttl_s=600)
    assert url == "http://localhost:9000/apex-artifacts/runs/r1/results.json?X-Amz-Expires=600"


async def test_put_stream_uploads_chunks_and_enforces_hard_cap() -> None:
    fake = FakeMinio()
    store = S3ArtifactStore(_conn(), client=fake)

    async def chunks():  # type: ignore[no-untyped-def]
        yield b"abc"
        yield b"def"

    stored = await store.put_stream(
        "runs/r1/stream.bin",
        chunks(),
        content_type="application/octet-stream",
        max_bytes=6,
    )
    assert stored.size == 6
    assert fake.objects[("apex-artifacts", "runs/r1/stream.bin")][0] == b"abcdef"

    with pytest.raises(ValueError, match="maximum size"):
        await store.put_stream(
            "runs/r1/too-big.bin",
            chunks(),
            content_type="application/octet-stream",
            max_bytes=5,
        )
    assert ("apex-artifacts", "runs/r1/too-big.bin") not in fake.objects


async def test_put_stream_spool_writes_do_not_run_on_event_loop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    loop_thread = threading.get_ident()
    write_threads: list[int] = []
    seek_threads: list[int] = []

    class TrackingSpool(BytesIO):
        def __enter__(self) -> "TrackingSpool":
            return self

        def __exit__(self, *_args: object) -> None:
            self.close()

        def write(self, data: Buffer) -> int:
            write_threads.append(threading.get_ident())
            return super().write(data)

        def seek(self, offset: int, whence: int = 0) -> int:
            seek_threads.append(threading.get_ident())
            return super().seek(offset, whence)

    monkeypatch.setattr(s3_module, "SpooledTemporaryFile", lambda **_kwargs: TrackingSpool())

    async def chunks():  # type: ignore[no-untyped-def]
        yield b"abc"
        yield b"def"

    fake = FakeMinio(existing_buckets={"apex-artifacts"})
    stored = await S3ArtifactStore(_conn(), client=fake).put_stream(
        "runs/r1/nonblocking.bin",
        chunks(),
        content_type="application/octet-stream",
        max_bytes=6,
    )

    assert stored.size == 6
    assert write_threads and all(thread != loop_thread for thread in write_threads)
    assert seek_threads and all(thread != loop_thread for thread in seek_threads)


async def test_get_closes_and_releases_response() -> None:
    fake = FakeMinio()
    store = S3ArtifactStore(_conn(), client=fake)
    await store.put("k", b"v", content_type="text/plain")
    await store.get("k")
    (response,) = fake.responses
    assert response.closed and response.released


async def test_iter_bytes_streams_and_releases_response() -> None:
    fake = FakeMinio()
    store = S3ArtifactStore(_conn(), client=fake)
    await store.put("k", b"abcdef", content_type="text/plain")

    assert [chunk async for chunk in store.iter_bytes("k", chunk_size=2)] == [
        b"ab",
        b"cd",
        b"ef",
    ]
    (response,) = fake.responses
    assert response.closed and response.released


async def test_cancelled_streams_are_worker_bounded_and_do_not_starve_to_thread() -> None:
    release_reads = threading.Event()
    state_lock = threading.Lock()
    state = {"active": 0, "max_active": 0, "close_raced": False}

    class BlockingResponse(FakeObjectResponse):
        def __init__(self) -> None:
            super().__init__(b"")
            self.reading = False

        def read(self, size: int = -1) -> bytes:
            del size
            with state_lock:
                self.reading = True
                state["active"] += 1
                state["max_active"] = max(state["max_active"], state["active"])
            try:
                if not release_reads.wait(timeout=5):
                    raise TimeoutError("test did not release blocking S3 read")
                return b""
            finally:
                with state_lock:
                    self.reading = False
                    state["active"] -= 1

        def close(self) -> None:
            with state_lock:
                if self.reading:
                    state["close_raced"] = True
            super().close()

    class BlockingMinio(FakeMinio):
        def get_object(self, bucket_name: str, object_name: str) -> BlockingResponse:
            del object_name
            assert bucket_name in self.buckets
            response = BlockingResponse()
            self.responses.append(response)
            return response

    fake = BlockingMinio(existing_buckets={"apex-artifacts"})
    store = S3ArtifactStore(_conn(), client=fake)

    async def consume(index: int) -> None:
        async for _chunk in store.iter_bytes(f"object-{index}"):
            pass

    tasks = [
        asyncio.create_task(consume(index)) for index in range(s3_module.STREAM_WORKER_LIMIT * 3)
    ]
    results: list[Any] = []
    try:
        for _ in range(200):
            with state_lock:
                saturated = state["active"] == s3_module.STREAM_WORKER_LIMIT
            if saturated:
                break
            await asyncio.sleep(0.01)
        else:
            raise AssertionError("S3 stream worker pool did not saturate")

        for task in tasks:
            task.cancel()
        await asyncio.sleep(0)

        # Blocking reads use the dedicated bounded pool. Even while every stream
        # worker is occupied and cancelled consumers definitively await them, an
        # unrelated default-executor operation must still start immediately.
        probe = await asyncio.wait_for(asyncio.to_thread(lambda: "available"), timeout=0.5)
        assert probe == "available"
        with state_lock:
            assert state["max_active"] <= s3_module.STREAM_WORKER_LIMIT
    finally:
        for task in tasks:
            task.cancel()
        release_reads.set()
        results = list(await asyncio.gather(*tasks, return_exceptions=True))

    assert all(
        isinstance(result, asyncio.CancelledError | ArtifactStoreBusyError) for result in results
    )
    assert sum(isinstance(result, ArtifactStoreBusyError) for result in results) == (
        s3_module.STREAM_WORKER_LIMIT * 2
    )
    assert len(fake.responses) == s3_module.STREAM_WORKER_LIMIT
    assert all(response.closed and response.released for response in fake.responses)
    assert state["close_raced"] is False


async def test_cancelled_deletes_are_bounded_and_do_not_starve_to_thread() -> None:
    release_deletes = threading.Event()
    state_lock = threading.Lock()
    state = {"active": 0, "max_active": 0, "calls": 0}

    class BlockingDeleteMinio(FakeMinio):
        def remove_object(self, bucket_name: str, object_name: str) -> None:
            del bucket_name, object_name
            with state_lock:
                state["active"] += 1
                state["calls"] += 1
                state["max_active"] = max(state["max_active"], state["active"])
            try:
                if not release_deletes.wait(timeout=5):
                    raise TimeoutError("test did not release blocking S3 delete")
            finally:
                with state_lock:
                    state["active"] -= 1

    store = S3ArtifactStore(
        _conn(), client=BlockingDeleteMinio(existing_buckets={"apex-artifacts"})
    )
    tasks = [
        asyncio.create_task(store.delete(f"object-{index}"))
        for index in range(s3_module.SDK_WORKER_LIMIT * 3)
    ]
    results: list[Any] = []
    try:
        for _ in range(200):
            with state_lock:
                saturated = state["active"] == s3_module.SDK_WORKER_LIMIT
            if saturated:
                break
            await asyncio.sleep(0.01)
        else:
            raise AssertionError("S3 SDK worker pool did not saturate")

        for task in tasks:
            task.cancel()
            task.cancel()
        await asyncio.sleep(0)

        assert await asyncio.wait_for(asyncio.to_thread(lambda: "available"), timeout=0.5) == (
            "available"
        )
        with state_lock:
            assert state["max_active"] <= s3_module.SDK_WORKER_LIMIT
    finally:
        for task in tasks:
            task.cancel()
        release_deletes.set()
        results = list(await asyncio.gather(*tasks, return_exceptions=True))

    assert all(isinstance(result, asyncio.CancelledError) for result in results)
    assert state["calls"] == s3_module.SDK_WORKER_LIMIT


def test_worker_admission_is_shared_across_event_loops() -> None:
    admission = s3_module._ProcessWorkerAdmission(1)
    start = threading.Barrier(3)
    state_lock = threading.Lock()
    active = 0
    maximum = 0

    async def work() -> None:
        nonlocal active, maximum
        await admission.acquire()
        try:
            with state_lock:
                active += 1
                maximum = max(maximum, active)
            await asyncio.sleep(0.03)
            with state_lock:
                active -= 1
        finally:
            admission.release()

    def worker() -> None:
        start.wait()
        asyncio.run(work())

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for thread in threads:
        thread.start()
    start.wait()
    for thread in threads:
        thread.join(timeout=2)

    assert all(not thread.is_alive() for thread in threads)
    assert maximum == 1


async def test_artifact_stream_admission_fails_fast_without_queuing_waiters(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    admission = s3_module._ProcessWorkerAdmission(1)
    assert admission.try_acquire() is True
    monkeypatch.setattr(s3_module, "_STREAM_ADMISSION", admission)
    store = S3ArtifactStore(_conn(), client=FakeMinio(existing_buckets={"apex-artifacts"}))

    iterator = store.iter_bytes("busy.bin")
    with pytest.raises(ArtifactStoreBusyError, match="capacity is busy"):
        await asyncio.wait_for(anext(iterator), timeout=0.1)

    assert admission._waiters == set()  # noqa: SLF001 - admission invariant
    admission.release()


async def test_cancelled_put_stream_keeps_spool_open_until_worker_finishes() -> None:
    started = threading.Event()
    release = threading.Event()
    state: dict[str, Any] = {"payload": None, "closed_early": False}

    class BlockingPutMinio(FakeMinio):
        def put_object(
            self,
            bucket_name: str,
            object_name: str,
            data: Any,
            length: int,
            content_type: str = "application/octet-stream",
        ) -> None:
            del bucket_name, object_name, content_type
            started.set()
            if not release.wait(timeout=5):
                raise TimeoutError("test did not release blocking S3 put")
            try:
                state["payload"] = data.read(length)
            except ValueError:
                state["closed_early"] = True
                raise

    async def chunks():  # type: ignore[no-untyped-def]
        yield b"payload"

    store = S3ArtifactStore(_conn(), client=BlockingPutMinio(existing_buckets={"apex-artifacts"}))
    task = asyncio.create_task(
        store.put_stream(
            "object",
            chunks(),
            content_type="application/octet-stream",
            max_bytes=1024,
        )
    )
    assert await asyncio.to_thread(started.wait, 1)
    task.cancel()
    task.cancel()
    await asyncio.sleep(0)
    assert not task.done()

    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert state == {"payload": b"payload", "closed_early": False}


async def test_put_stream_reserves_process_spool_budget_before_consuming() -> None:
    started = 0
    budget_full = asyncio.Event()
    release = asyncio.Event()
    per_upload = s3_module.MAX_PROCESS_SPOOL_BYTES // s3_module.SDK_WORKER_LIMIT

    async def blocked_chunks():  # type: ignore[no-untyped-def]
        nonlocal started
        started += 1
        if started == s3_module.SDK_WORKER_LIMIT:
            budget_full.set()
        yield b"x"
        await release.wait()

    store = S3ArtifactStore(_conn(), client=FakeMinio(existing_buckets={"apex-artifacts"}))
    tasks = [
        asyncio.create_task(
            store.put_stream(
                f"object-{index}",
                blocked_chunks(),
                content_type="application/octet-stream",
                max_bytes=per_upload,
            )
        )
        for index in range(s3_module.SDK_WORKER_LIMIT + 1)
    ]
    try:
        await asyncio.wait_for(budget_full.wait(), timeout=1)
        await asyncio.sleep(0.05)
        assert started == s3_module.SDK_WORKER_LIMIT
    finally:
        for task in tasks:
            task.cancel()
        release.set()
        await asyncio.gather(*tasks, return_exceptions=True)


async def test_bucket_option_drives_uri_and_default_bucket() -> None:
    fake = FakeMinio()
    stored = await S3ArtifactStore(_conn(bucket="other-bucket"), client=fake).put(
        "k", b"v", content_type="text/plain"
    )
    assert stored.uri == "s3://other-bucket/k"

    # No conn at all -> default bucket.
    fake2 = FakeMinio()
    stored2 = await S3ArtifactStore(client=fake2).put("k", b"v", content_type="text/plain")
    assert stored2.uri == "s3://apex-artifacts/k"


async def test_get_missing_key_raises_keyerror() -> None:
    store = S3ArtifactStore(_conn(), client=FakeMinio())
    with pytest.raises(KeyError, match="missing/key"):
        await store.get("missing/key")


# --- unit: bucket ensure -------------------------------------------------------


async def test_bucket_ensured_once_per_instance() -> None:
    fake = FakeMinio()
    store = S3ArtifactStore(_conn(), client=fake)
    await store.put("a", b"1", content_type="text/plain")
    await store.put("b", b"2", content_type="text/plain")
    await store.get("a")
    await store.get_url("b")
    assert fake.bucket_exists_calls == 1
    assert fake.make_bucket_calls == 1


async def test_existing_bucket_is_not_recreated() -> None:
    fake = FakeMinio(existing_buckets={"apex-artifacts"})
    store = S3ArtifactStore(_conn(), client=fake)
    await store.put("a", b"1", content_type="text/plain")
    assert fake.bucket_exists_calls == 1
    assert fake.make_bucket_calls == 0


async def test_make_bucket_race_is_tolerated() -> None:
    fake = RacingFakeMinio()
    store = S3ArtifactStore(_conn(), client=fake)
    stored = await store.put("a", b"1", content_type="text/plain")
    assert stored.uri == "s3://apex-artifacts/a"
    assert fake.make_bucket_calls == 1  # raced once, then remembered as ensured


# --- unit: construction + registry ---------------------------------------------


async def test_satisfies_artifact_store_port() -> None:
    assert isinstance(S3ArtifactStore(_conn(), client=FakeMinio()), ArtifactStorePort)


async def test_missing_secret_raises_value_error() -> None:
    with pytest.raises(ValueError, match="secret"):
        S3ArtifactStore(_conn(), None)


@pytest.mark.parametrize("bucket", ["ab", "UPPERCASE", "bucket/name", "b" * 64])
def test_constructor_rejects_invalid_bucket_before_sdk_use(bucket: str) -> None:
    with pytest.raises(ValueError, match="bucket"):
        S3ArtifactStore(_conn(bucket=bucket), client=FakeMinio())


@pytest.mark.parametrize(
    ("option", "value", "match"),
    [
        ("endpoint", True, "endpoint"),
        ("access_key", True, "access key"),
        ("access_key", "unsafe\r\nkey", "control"),
        ("access_key", "a" * 1_025, "1024"),
    ],
)
def test_constructor_rejects_unsafe_sdk_identity_options(
    option: str,
    value: object,
    match: str,
) -> None:
    with pytest.raises(ValueError, match=match):
        S3ArtifactStore(_conn(**{option: value}), SecretValue(value="secret"))


@pytest.mark.parametrize("secret", ["unsafe\r\nsecret", "s" * 16_385])
def test_constructor_rejects_unsafe_secret_without_reflection(secret: str) -> None:
    with pytest.raises(ValueError) as error:
        S3ArtifactStore(_conn(), SecretValue(value=secret))

    assert secret not in str(error.value)


def test_string_false_does_not_enable_tls(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}

    def fake_minio(*args: Any, **kwargs: Any) -> FakeMinio:
        captured.update(kwargs)
        return FakeMinio()

    monkeypatch.setattr(s3_module, "Minio", fake_minio)
    S3ArtifactStore(_conn(secure="false"), SecretValue(value="secret"))

    assert captured["secure"] is False


@pytest.mark.parametrize(
    ("endpoint", "configured_secure", "expected_endpoint", "expected_secure"),
    [
        ("minio.example.test:9000", True, "minio.example.test:9000", True),
        ("https://minio.example.test:9000/", False, "minio.example.test:9000", True),
        ("http://minio.example.test:9000", True, "minio.example.test:9000", False),
    ],
)
def test_endpoint_url_is_normalized_to_minio_host_port_contract(
    monkeypatch: pytest.MonkeyPatch,
    endpoint: str,
    configured_secure: bool,
    expected_endpoint: str,
    expected_secure: bool,
) -> None:
    captured: dict[str, Any] = {}

    def fake_minio(raw_endpoint: str, **kwargs: Any) -> FakeMinio:
        captured["endpoint"] = raw_endpoint
        captured.update(kwargs)
        return FakeMinio()

    monkeypatch.setattr(s3_module, "Minio", fake_minio)
    S3ArtifactStore(
        _conn(endpoint=endpoint, secure=configured_secure),
        SecretValue(value="secret"),
    )

    assert captured["endpoint"] == expected_endpoint
    assert captured["secure"] is expected_secure


@pytest.mark.parametrize(
    "endpoint",
    [
        "https://user:password@minio.example.test:9000",
        "https://minio.example.test:9000/bucket-prefix",
        "https://minio.example.test:9000?signature=secret",
        "minio.example.test:9000/bucket-prefix",
    ],
)
def test_endpoint_rejects_non_host_port_components_before_sdk_use(endpoint: str) -> None:
    with pytest.raises(ValueError, match="endpoint") as error:
        S3ArtifactStore(_conn(endpoint=endpoint), SecretValue(value="secret"))

    assert "password" not in str(error.value)
    assert "signature" not in str(error.value)


async def test_registry_builds_s3_provider_with_env_secret(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("APEX_INTEGRATION_MINIO_SECRET_KEY", "apex-minio")
    adapter = await AdapterRegistry.build(_conn(), EnvSecretsAdapter())
    assert isinstance(adapter, S3ArtifactStore)
    # Real client constructed (lazily — no network at build time).
    assert isinstance(adapter._client, Minio)
    assert isinstance(adapter._client._http, SafePoolManager)


# --- integration: real MinIO (env-gated) ----------------------------------------

requires_minio = pytest.mark.skipif(
    not os.environ.get("APEX_TEST_MINIO"),
    reason="needs MinIO at localhost:9000 (set APEX_TEST_MINIO=1)",
)


def _real_store() -> S3ArtifactStore:
    secret = SecretValue(value=os.environ.get("APEX_INTEGRATION_MINIO_SECRET_KEY", "apex-minio"))
    return S3ArtifactStore(_conn(bucket="apex-artifacts-test"), secret)


@requires_minio
async def test_minio_roundtrip_small_and_large_with_presigned_url() -> None:
    import httpx

    store = _real_store()
    prefix = f"tests/{uuid.uuid4().hex}"

    stored = await store.put(
        f"{prefix}/results.json", b'{"ok": true}', content_type="application/json"
    )
    assert stored.uri == f"s3://apex-artifacts-test/{prefix}/results.json"
    assert await store.get(f"{prefix}/results.json") == b'{"ok": true}'

    # Exceed S3's 5 MiB minimum part size so CI proves the gateway's narrow POST
    # allowlist still permits create/complete multipart upload operations.
    big = os.urandom(6 * 1024 * 1024)
    stored_big = await store.put(
        f"{prefix}/transcript.bin", big, content_type="application/octet-stream"
    )
    assert stored_big.size == len(big)
    assert await store.get(f"{prefix}/transcript.bin") == big

    url = await store.get_url(f"{prefix}/transcript.bin", ttl_s=300)
    async with httpx.AsyncClient() as http:
        response = await http.get(url)
    assert response.status_code == 200
    assert response.content == big


@requires_minio
async def test_minio_missing_key_raises_keyerror() -> None:
    store = _real_store()
    with pytest.raises(KeyError, match="never-stored"):
        await store.get(f"tests/{uuid.uuid4().hex}/never-stored")

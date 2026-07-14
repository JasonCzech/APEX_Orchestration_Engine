"""S3/MinIO artifact store: fake-client unit coverage + env-gated MinIO round-trip.

Unit tests inject a FakeMinio through the adapter's `client` constructor param,
so no object store (and no network) is required. The integration tests at the
bottom run only with APEX_TEST_MINIO=1 against the dev MinIO at localhost:9000.
"""

import os
import uuid
from datetime import timedelta
from io import BytesIO
from typing import Any, cast

import pytest
from minio import Minio
from minio.error import S3Error

from apex.adapters.network_safety import SafePoolManager
from apex.adapters.registry import AdapterRegistry, ConnectionConfig, PortKind
from apex.adapters.s3 import S3ArtifactStore
from apex.adapters.stubs import EnvSecretsAdapter
from apex.domain.integrations import SecretValue
from apex.ports.artifact_store import ArtifactStorePort

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

    big = os.urandom(1_500_000)  # > 1 MB
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

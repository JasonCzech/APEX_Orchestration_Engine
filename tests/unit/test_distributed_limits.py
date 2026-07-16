import asyncio
from collections import deque
from typing import Any

import pytest

from apex.app.distributed_limits import (
    LimitBackendUnavailable,
    RedisDistributedLimitBackend,
)


class FakeRedis:
    def __init__(self, *results: int | Exception) -> None:
        self.results = deque(results)
        self.calls: list[tuple[str, int, tuple[Any, ...]]] = []
        self.closed = False

    async def eval(self, script: str, numkeys: int, *keys_and_args: Any) -> int:
        self.calls.append((script, numkeys, keys_and_args))
        result = self.results.popleft() if self.results else 1
        if isinstance(result, Exception):
            raise result
        return result

    async def ping(self) -> bool:
        result = self.results.popleft() if self.results else 1
        if isinstance(result, Exception):
            raise result
        return bool(result)

    async def aclose(self) -> None:
        self.closed = True


async def test_fixed_window_uses_hashed_cluster_colocated_keys() -> None:
    redis = FakeRedis(0)
    backend = RedisDistributedLimitBackend(client=redis)

    retry = await backend.check_window(
        "request",
        (("ip:203.0.113.10", 10), ("key:super-secret-credential", 3)),
        window_s=60,
    )

    assert retry is None
    _script, numkeys, values = redis.calls[0]
    keys = values[:numkeys]
    assert numkeys == 2
    assert all("{limits}" in key for key in keys)
    assert "super-secret-credential" not in repr(keys)
    assert values[-2:] == (10, 3)


async def test_backend_returns_retry_and_wraps_redis_failure() -> None:
    redis = FakeRedis(7, ConnectionError("redis credentials leaked here"))
    backend = RedisDistributedLimitBackend(client=redis)

    assert await backend.check_window("run-create", (("ip:one", 1),), window_s=60) == 7
    with pytest.raises(LimitBackendUnavailable) as error:
        await backend.auth_retry_after(("ip:one",))

    assert "credentials" not in str(error.value)
    assert error.value.__cause__ is None
    assert error.value.__context__ is None


async def test_stream_permit_is_a_renewable_expiring_lease() -> None:
    redis = FakeRedis(1, 1, 1)
    backend = RedisDistributedLimitBackend(client=redis)

    lease = await backend.acquire_stream(
        ("ip:one", "key:one"),
        global_limit=2,
        source_limit=1,
        credential_limit=1,
        lease_ttl_s=30,
    )

    assert lease is not None
    assert len(lease.redis_keys) == 3
    assert await backend.renew_stream(lease, lease_ttl_s=30) is True
    await backend.release_stream(lease)
    assert [call[1] for call in redis.calls] == [3, 3, 3]


async def test_cancelled_stream_acquire_definitively_releases_ambiguous_lease() -> None:
    class AmbiguousAcquireRedis(FakeRedis):
        def __init__(self) -> None:
            super().__init__()
            self.acquire_started = asyncio.Event()
            self.allow_acquire = asyncio.Event()
            self.acquire_finished = asyncio.Event()
            self.release_started = asyncio.Event()
            self.allow_release = asyncio.Event()

        async def eval(self, script: str, numkeys: int, *keys_and_args: Any) -> int:
            self.calls.append((script, numkeys, keys_and_args))
            if len(self.calls) == 1:
                self.acquire_started.set()
                await self.allow_acquire.wait()
                self.acquire_finished.set()
                return 1
            assert self.acquire_finished.is_set()
            self.release_started.set()
            await self.allow_release.wait()
            return 1

    redis = AmbiguousAcquireRedis()
    backend = RedisDistributedLimitBackend(client=redis)
    acquire = asyncio.create_task(
        backend.acquire_stream(
            ("ip:one", "key:one"),
            global_limit=2,
            source_limit=1,
            credential_limit=1,
            lease_ttl_s=30,
        )
    )
    await redis.acquire_started.wait()

    acquire.cancel()
    await asyncio.sleep(0)
    assert redis.release_started.is_set() is False
    assert acquire.done() is False

    redis.allow_acquire.set()
    await redis.acquire_finished.wait()
    await redis.release_started.wait()
    acquire.cancel()
    await asyncio.sleep(0)
    acquire.cancel()
    await asyncio.sleep(0)

    assert acquire.done() is False
    redis.allow_release.set()
    with pytest.raises(asyncio.CancelledError):
        await acquire

    assert len(redis.calls) == 2
    acquire_call, release_call = redis.calls
    assert acquire_call[1] == release_call[1] == 3
    assert acquire_call[2][:4] == release_call[2]


async def test_failed_stream_acquire_compensates_for_a_lost_success_reply() -> None:
    redis = FakeRedis(ConnectionError("reply lost after commit"), 1)
    backend = RedisDistributedLimitBackend(client=redis)

    with pytest.raises(LimitBackendUnavailable):
        await backend.acquire_stream(
            ("ip:one", "key:one"),
            global_limit=2,
            source_limit=1,
            credential_limit=1,
            lease_ttl_s=30,
        )

    assert len(redis.calls) == 2
    acquire_call, release_call = redis.calls
    assert acquire_call[1] == release_call[1] == 3
    assert acquire_call[2][:4] == release_call[2]


async def test_auth_failure_and_success_touch_both_source_and_credential_state() -> None:
    redis = FakeRedis(1, 1)
    backend = RedisDistributedLimitBackend(client=redis)
    subjects = ("ip:one", "key:one")

    await backend.record_auth_failure(subjects, limit=2, window_s=60, lockout_s=30)
    await backend.clear_auth(subjects[1:])

    assert redis.calls[0][1] == 4  # failure + lockout key for both subjects
    assert redis.calls[1][1] == 2  # successful auth clears only the credential pair


async def test_backend_close_releases_redis_pool() -> None:
    redis = FakeRedis()
    backend = RedisDistributedLimitBackend(client=redis)

    await backend.close()

    assert redis.closed is True


async def test_backend_readiness_fails_closed() -> None:
    backend = RedisDistributedLimitBackend(client=FakeRedis(ConnectionError("offline")))

    with pytest.raises(LimitBackendUnavailable, match="readiness"):
        await backend.check_ready()

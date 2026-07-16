"""Redis-backed limits shared by every API replica.

All multi-key operations are atomic Lua scripts and every key uses the same
Redis Cluster hash tag. Redis server time, rather than pod wall clocks, keeps
windows and leases consistent across replicas. Backend failures are surfaced so
the HTTP middleware can fail closed instead of silently reverting to local state.
"""

from __future__ import annotations

import asyncio
import hashlib
import secrets
from dataclasses import dataclass
from typing import Any, Protocol, cast

from redis.asyncio import Redis

_PREFIX = "apex:{limits}:v1"

_FIXED_WINDOW_SCRIPT = """
local stamp = redis.call('TIME')
local now = (tonumber(stamp[1]) * 1000) + math.floor(tonumber(stamp[2]) / 1000)
local window = tonumber(ARGV[1])
local member = ARGV[2]
local retry = 0

for index, key in ipairs(KEYS) do
  local limit = tonumber(ARGV[index + 2])
  redis.call('ZREMRANGEBYSCORE', key, '-inf', now - window)
  if redis.call('ZCARD', key) >= limit then
    local oldest = redis.call('ZRANGE', key, 0, 0, 'WITHSCORES')
    local wait = math.max(math.ceil((tonumber(oldest[2]) + window - now) / 1000), 1)
    retry = math.max(retry, wait)
  end
end

if retry > 0 then
  return retry
end
for _, key in ipairs(KEYS) do
  redis.call('ZADD', key, now, member)
  redis.call('PEXPIRE', key, window + 1000)
end
return 0
"""

_AUTH_RETRY_SCRIPT = """
local retry = 0
for _, key in ipairs(KEYS) do
  local ttl = redis.call('PTTL', key)
  if ttl == -1 then
    retry = math.max(retry, 1)
  elseif ttl > 0 then
    retry = math.max(retry, math.max(math.ceil(ttl / 1000), 1))
  end
end
return retry
"""

_AUTH_FAILURE_SCRIPT = """
local stamp = redis.call('TIME')
local now = (tonumber(stamp[1]) * 1000) + math.floor(tonumber(stamp[2]) / 1000)
local limit = tonumber(ARGV[1])
local window = tonumber(ARGV[2])
local lockout = tonumber(ARGV[3])
local member = ARGV[4]
local retention = math.max(window, lockout) + 1000

for index = 1, #KEYS, 2 do
  local failures = KEYS[index]
  local lock = KEYS[index + 1]
  redis.call('ZREMRANGEBYSCORE', failures, '-inf', now - window)
  redis.call('ZADD', failures, now, member)
  redis.call('PEXPIRE', failures, retention)
  if redis.call('ZCARD', failures) >= limit then
    redis.call('SET', lock, '1', 'PX', lockout)
  end
end
return 1
"""

_DELETE_SCRIPT = """
if #KEYS == 0 then
  return 0
end
return redis.call('DEL', unpack(KEYS))
"""

_STREAM_ACQUIRE_SCRIPT = """
local stamp = redis.call('TIME')
local now = (tonumber(stamp[1]) * 1000) + math.floor(tonumber(stamp[2]) / 1000)
local lease = ARGV[1]
local ttl = tonumber(ARGV[2])

for index, key in ipairs(KEYS) do
  local limit = tonumber(ARGV[index + 2])
  redis.call('ZREMRANGEBYSCORE', key, '-inf', now)
  if redis.call('ZCARD', key) >= limit then
    return 0
  end
end
for _, key in ipairs(KEYS) do
  redis.call('ZADD', key, now + ttl, lease)
  redis.call('PEXPIRE', key, ttl + 5000)
end
return 1
"""

_STREAM_RENEW_SCRIPT = """
local stamp = redis.call('TIME')
local now = (tonumber(stamp[1]) * 1000) + math.floor(tonumber(stamp[2]) / 1000)
local lease = ARGV[1]
local ttl = tonumber(ARGV[2])

for _, key in ipairs(KEYS) do
  redis.call('ZREMRANGEBYSCORE', key, '-inf', now)
  if not redis.call('ZSCORE', key, lease) then
    return 0
  end
end
for _, key in ipairs(KEYS) do
  redis.call('ZADD', key, now + ttl, lease)
  redis.call('PEXPIRE', key, ttl + 5000)
end
return 1
"""

_STREAM_RELEASE_SCRIPT = """
local lease = ARGV[1]
for _, key in ipairs(KEYS) do
  redis.call('ZREM', key, lease)
end
return 1
"""


class LimitBackendUnavailable(RuntimeError):
    """The shared limiter cannot make an authoritative admission decision."""


@dataclass(frozen=True)
class StreamLease:
    id: str
    redis_keys: tuple[str, ...]


class DistributedLimitBackend(Protocol):
    async def check_ready(self) -> None: ...

    async def check_window(
        self,
        namespace: str,
        keyed_limits: tuple[tuple[str, int], ...],
        *,
        window_s: int,
    ) -> int | None: ...

    async def auth_retry_after(self, keys: tuple[str, ...]) -> int | None: ...

    async def record_auth_failure(
        self,
        keys: tuple[str, ...],
        *,
        limit: int,
        window_s: int,
        lockout_s: int,
    ) -> None: ...

    async def clear_auth(self, keys: tuple[str, ...]) -> None: ...

    async def acquire_stream(
        self,
        keys: tuple[str, ...],
        *,
        global_limit: int,
        source_limit: int,
        credential_limit: int,
        lease_ttl_s: int,
    ) -> StreamLease | None: ...

    async def renew_stream(self, lease: StreamLease, *, lease_ttl_s: int) -> bool: ...

    async def release_stream(self, lease: StreamLease) -> None: ...

    async def close(self) -> None: ...


class _RedisClient(Protocol):
    async def ping(self) -> Any: ...

    async def eval(self, script: str, numkeys: int, *keys_and_args: Any) -> Any: ...

    async def aclose(self) -> None: ...


def _subject_token(subject: str) -> str:
    return hashlib.sha256(subject.encode("utf-8")).hexdigest()


class RedisDistributedLimitBackend:
    """Atomic shared request, lockout, and leased-concurrency limits."""

    def __init__(self, redis_uri: str | None = None, *, client: _RedisClient | None = None) -> None:
        if client is None:
            if not redis_uri:
                raise ValueError("Redis distributed limits require REDIS_URI")
            client = cast(
                _RedisClient,
                Redis.from_url(
                    redis_uri,
                    decode_responses=False,
                    health_check_interval=30,
                    max_connections=32,
                    retry_on_timeout=False,
                    socket_connect_timeout=2.0,
                    socket_timeout=2.0,
                ),
            )
        assert client is not None
        self._redis = client

    async def _eval(self, script: str, keys: tuple[str, ...], *args: Any) -> int:
        backend_unavailable = False
        try:
            result = await self._redis.eval(script, len(keys), *keys, *args)
            parsed_result = int(result)
        except asyncio.CancelledError:
            raise
        except Exception:
            backend_unavailable = True
            parsed_result = 0
        if backend_unavailable:
            raise LimitBackendUnavailable("distributed limit backend is unavailable")
        return parsed_result

    async def check_ready(self) -> None:
        readiness_failed = False
        try:
            if not await self._redis.ping():
                raise LimitBackendUnavailable("distributed limit backend readiness check failed")
        except asyncio.CancelledError:
            raise
        except LimitBackendUnavailable:
            raise
        except Exception:
            readiness_failed = True
        if readiness_failed:
            raise LimitBackendUnavailable("distributed limit backend readiness check failed")

    @staticmethod
    def _key(namespace: str, subject: str) -> str:
        return f"{_PREFIX}:{namespace}:{_subject_token(subject)}"

    @classmethod
    def _auth_keys(cls, subjects: tuple[str, ...]) -> tuple[str, ...]:
        keys: list[str] = []
        for subject in subjects:
            token = _subject_token(subject)
            keys.extend(
                (
                    f"{_PREFIX}:auth-failures:{token}",
                    f"{_PREFIX}:auth-lockout:{token}",
                )
            )
        return tuple(keys)

    async def check_window(
        self,
        namespace: str,
        keyed_limits: tuple[tuple[str, int], ...],
        *,
        window_s: int,
    ) -> int | None:
        if namespace not in {"request", "run-create"}:
            raise ValueError("unsupported distributed limit namespace")
        if not keyed_limits:
            return None
        keys = tuple(self._key(namespace, subject) for subject, _limit in keyed_limits)
        limits = tuple(max(int(limit), 1) for _subject, limit in keyed_limits)
        retry = await self._eval(
            _FIXED_WINDOW_SCRIPT,
            keys,
            max(int(window_s), 1) * 1000,
            secrets.token_hex(16),
            *limits,
        )
        return retry or None

    async def auth_retry_after(self, keys: tuple[str, ...]) -> int | None:
        lock_keys = self._auth_keys(keys)[1::2]
        if not lock_keys:
            return None
        retry = await self._eval(_AUTH_RETRY_SCRIPT, lock_keys)
        return retry or None

    async def record_auth_failure(
        self,
        keys: tuple[str, ...],
        *,
        limit: int,
        window_s: int,
        lockout_s: int,
    ) -> None:
        auth_keys = self._auth_keys(keys)
        if not auth_keys:
            return
        await self._eval(
            _AUTH_FAILURE_SCRIPT,
            auth_keys,
            max(int(limit), 1),
            max(int(window_s), 1) * 1000,
            max(int(lockout_s), 1) * 1000,
            secrets.token_hex(16),
        )

    async def clear_auth(self, keys: tuple[str, ...]) -> None:
        auth_keys = self._auth_keys(keys)
        if auth_keys:
            await self._eval(_DELETE_SCRIPT, auth_keys)

    async def acquire_stream(
        self,
        keys: tuple[str, ...],
        *,
        global_limit: int,
        source_limit: int,
        credential_limit: int,
        lease_ttl_s: int,
    ) -> StreamLease | None:
        if not keys:
            raise ValueError("stream admission requires a source key")
        redis_keys = [f"{_PREFIX}:sse:global", self._key("sse-source", keys[0])]
        limits = [max(int(global_limit), 1), max(int(source_limit), 1)]
        if len(keys) > 1:
            redis_keys.append(self._key("sse-credential", keys[1]))
            limits.append(max(int(credential_limit), 1))
        lease = StreamLease(id=secrets.token_hex(16), redis_keys=tuple(redis_keys))
        acquire_task = asyncio.create_task(
            self._eval(
                _STREAM_ACQUIRE_SCRIPT,
                lease.redis_keys,
                lease.id,
                max(int(lease_ttl_s), 1) * 1000,
                *limits,
            ),
            name="apex-stream-lease-acquire",
        )

        async def compensate_ambiguous_acquire() -> None:
            async def release_exact_lease() -> None:
                try:
                    await self.release_stream(lease)
                except LimitBackendUnavailable:
                    # The expiring lease remains the final outage fallback.
                    pass

            release_task = asyncio.create_task(
                release_exact_lease(),
                name="apex-ambiguous-stream-lease-release",
            )
            interrupted = False
            while not release_task.done():
                try:
                    await asyncio.shield(release_task)
                except asyncio.CancelledError:
                    interrupted = True
            release_task.result()
            if interrupted:
                raise asyncio.CancelledError from None

        try:
            acquired = await asyncio.shield(acquire_task)
        except asyncio.CancelledError:
            # Do not let caller cancellation cancel the Redis command itself.
            # A release sent before an in-flight acquire settles could execute
            # first and still leave the later lease orphaned.  Establish command
            # ordering by owning and definitively settling the acquire task,
            # despite repeated cancellation, before exact-id compensation.
            while not acquire_task.done():
                try:
                    await asyncio.shield(acquire_task)
                except asyncio.CancelledError:
                    continue
                except BaseException:
                    break
            try:
                acquire_task.result()
            except BaseException:
                # Caller cancellation remains authoritative.  Exact release is
                # safe whether the script denied, succeeded, or lost its reply.
                pass
            await compensate_ambiguous_acquire()
            raise
        except LimitBackendUnavailable:
            # A transport failure can arrive after Redis committed the Lua
            # script but before its reply reached this process.  The caller
            # receives the original backend error only after exact-id cleanup.
            await compensate_ambiguous_acquire()
            raise
        return lease if acquired == 1 else None

    async def renew_stream(self, lease: StreamLease, *, lease_ttl_s: int) -> bool:
        renewed = await self._eval(
            _STREAM_RENEW_SCRIPT,
            lease.redis_keys,
            lease.id,
            max(int(lease_ttl_s), 1) * 1000,
        )
        return renewed == 1

    async def release_stream(self, lease: StreamLease) -> None:
        await self._eval(_STREAM_RELEASE_SCRIPT, lease.redis_keys, lease.id)

    async def close(self) -> None:
        close_failed = False
        try:
            await self._redis.aclose()
        except asyncio.CancelledError:
            raise
        except Exception:
            close_failed = True
        if close_failed:
            raise LimitBackendUnavailable("distributed limit backend close failed")

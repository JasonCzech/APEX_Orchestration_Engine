"""Connect-time destination validation for outbound adapter HTTP clients.

Resolving a hostname when a connection is saved is not sufficient protection
against DNS rebinding: an attacker can return a public address during validation
and a private address when the HTTP client opens its socket.  The transports in
this module resolve immediately before connecting, reject the entire answer set
when any address is non-global, and connect to a validated numeric address.  TLS
still receives the original hostname from httpcore/urllib3, preserving SNI and
certificate hostname verification.
"""

# The async backend must exactly implement httpcore's public ``connect_tcp``
# signature, whose timeout argument triggers this generic lint rule.
# ruff: noqa: ASYNC109

from __future__ import annotations

import asyncio
import json
import math
import socket
import ssl
import subprocess
import sys
import threading
import time
from collections.abc import Iterable, Mapping
from ipaddress import IPv4Address, IPv6Address, ip_address
from typing import Any, cast

import httpcore
import httpx
import urllib3
from httpcore._backends.auto import AutoBackend
from httpcore._backends.base import SOCKET_OPTION
from httpcore._backends.sync import SyncBackend
from urllib3 import HTTPConnectionPool, HTTPSConnectionPool
from urllib3.connection import HTTPConnection, HTTPSConnection
from urllib3.exceptions import ConnectTimeoutError, NewConnectionError

TRUSTED_PRIVATE_HOST_OPTION = "_apex_trusted_private_host"

DENIED_HOSTS = frozenset(
    {
        "localhost",
        "localhost.localdomain",
        "metadata",
        "metadata.google.internal",
    }
)
DENIED_HOST_SUFFIXES = (".metadata.google.internal",)
DNS_WORKER_LIMIT = 8
DNS_RESOLUTION_TIMEOUT_S = 5.0
MAX_CONNECT_CANDIDATES = 8
_DNS_ADMISSION = threading.BoundedSemaphore(DNS_WORKER_LIMIT)
_DNS_WORKER_ENV = {
    "LANG": "C",
    "LC_ALL": "C",
}
_DNS_CHILD_CODE = """
import json
import socket
import sys

try:
    infos = socket.getaddrinfo(sys.argv[1], int(sys.argv[2]), type=socket.SOCK_STREAM)
    addresses = sorted({str(info[4][0]) for info in infos if info and info[4]})[:256]
    sys.stdout.write(json.dumps(addresses, separators=(",", ":")))
except BaseException:
    raise SystemExit(2)
"""


class UnsafeDestinationError(ValueError):
    """A destination cannot be connected to under the outbound network policy."""


def _dns_command(host: str, port: int) -> tuple[str, ...]:
    return (sys.executable, "-I", "-c", _DNS_CHILD_CODE, host, str(port))


def _parse_dns_output(output: bytes) -> list[str]:
    try:
        decoded = json.loads(output.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise UnsafeDestinationError("adapter host could not be resolved") from exc
    if not isinstance(decoded, list) or any(not isinstance(item, str) for item in decoded):
        raise UnsafeDestinationError("adapter host could not be resolved")
    return decoded[:256]


def _resolve_hostname_sync(
    host: str,
    port: int,
    *,
    timeout_s: float = DNS_RESOLUTION_TIMEOUT_S,
) -> list[str]:
    """Resolve in a killable child process under one absolute wall deadline."""

    started = time.monotonic()
    budget = min(DNS_RESOLUTION_TIMEOUT_S, max(timeout_s, 0.0))
    if not _DNS_ADMISSION.acquire(timeout=budget):
        raise UnsafeDestinationError("adapter host DNS resolution timed out")
    try:
        remaining = budget - (time.monotonic() - started)
        if remaining <= 0:
            raise UnsafeDestinationError("adapter host DNS resolution timed out")
        try:
            completed = subprocess.run(  # noqa: S603 — fixed interpreter/script, argv only
                _dns_command(host, port),
                check=False,
                capture_output=True,
                timeout=remaining,
                env=dict(_DNS_WORKER_ENV),
            )
        except subprocess.TimeoutExpired as exc:
            raise UnsafeDestinationError("adapter host DNS resolution timed out") from exc
        if completed.returncode != 0:
            raise UnsafeDestinationError("adapter host could not be resolved")
        return _parse_dns_output(completed.stdout)
    finally:
        _DNS_ADMISSION.release()


async def _acquire_dns_admission() -> None:
    delay = 0.001
    while not _DNS_ADMISSION.acquire(blocking=False):
        await asyncio.sleep(delay)
        delay = min(delay * 2, 0.05)


async def _terminate_dns_process(process: asyncio.subprocess.Process) -> None:
    """Kill/reap a resolver even under repeated caller cancellation."""

    try:
        process.kill()
    except ProcessLookupError:
        pass
    wait_task = asyncio.create_task(process.wait())
    cancelled = False
    while not wait_task.done():
        try:
            await asyncio.shield(wait_task)
        except asyncio.CancelledError:
            cancelled = True
    wait_task.result()
    if cancelled:
        raise asyncio.CancelledError


async def _resolve_hostname_async(
    host: str,
    port: int,
    *,
    timeout_s: float = DNS_RESOLUTION_TIMEOUT_S,
) -> list[str]:
    """Async killable DNS resolution; cancellation terminates the child."""

    process: asyncio.subprocess.Process | None = None
    acquired = False
    try:
        async with asyncio.timeout(min(DNS_RESOLUTION_TIMEOUT_S, max(timeout_s, 0.0))):
            await _acquire_dns_admission()
            acquired = True
            process = await asyncio.create_subprocess_exec(
                *_dns_command(host, port),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
                env=dict(_DNS_WORKER_ENV),
            )
            stdout, _stderr = await process.communicate()
            if process.returncode != 0:
                raise UnsafeDestinationError("adapter host could not be resolved")
            return _parse_dns_output(stdout)
    except TimeoutError as exc:
        raise UnsafeDestinationError("adapter host DNS resolution timed out") from exc
    finally:
        try:
            if process is not None and process.returncode is None:
                await _terminate_dns_process(process)
        finally:
            if acquired:
                _DNS_ADMISSION.release()


async def _resolve_destination_async(
    host: str,
    port: int,
    *,
    allow_private_hosts: bool,
    timeout_s: float | None = None,
) -> tuple[IPv4Address | IPv6Address, ...]:
    """Resolve without blocking the event loop and validate the complete answer."""

    normalized, literal = _normalize_destination(host, allow_private_hosts=allow_private_hosts)
    if literal is not None:
        raw_addresses = [str(literal)]
    elif timeout_s is None:
        raw_addresses = await _resolve_hostname_async(normalized, port)
    else:
        raw_addresses = await _resolve_hostname_async(normalized, port, timeout_s=timeout_s)
    return _validate_addresses(raw_addresses, allow_private_hosts=allow_private_hosts)


def private_hosts_allowed(options: Mapping[str, Any]) -> bool:
    """Return the effective private-network policy for a persisted connection."""

    if options.get(TRUSTED_PRIVATE_HOST_OPTION) is True:
        return True
    # Imported lazily to keep adapter registration independent from settings
    # module initialization.
    from apex.settings import get_settings

    return get_settings().allow_private_adapter_hosts


def resolve_destination(
    host: str,
    port: int,
    *,
    allow_private_hosts: bool = False,
    timeout_s: float | None = None,
) -> tuple[IPv4Address | IPv6Address, ...]:
    """Resolve ``host`` once and return a validated, deterministic address set.

    Every returned address must be acceptable.  Rejecting mixed public/private
    answers prevents a resolver from steering individual connection attempts to
    a private address while leaving a public address present as camouflage.
    """

    normalized, literal = _normalize_destination(host, allow_private_hosts=allow_private_hosts)
    if literal is not None:
        raw_addresses = [str(literal)]
    elif timeout_s is None:
        raw_addresses = _resolve_hostname_sync(normalized, port)
    else:
        raw_addresses = _resolve_hostname_sync(normalized, port, timeout_s=timeout_s)
    return _validate_addresses(raw_addresses, allow_private_hosts=allow_private_hosts)


def _normalize_destination(
    host: str, *, allow_private_hosts: bool
) -> tuple[str, IPv4Address | IPv6Address | None]:
    normalized = host.rstrip(".").lower()
    if not normalized:
        raise UnsafeDestinationError("destination host is empty")
    if not allow_private_hosts and (
        normalized in DENIED_HOSTS or normalized.endswith(DENIED_HOST_SUFFIXES)
    ):
        raise UnsafeDestinationError("private adapter hosts are disabled")

    try:
        return normalized, ip_address(normalized)
    except ValueError:
        return normalized, None


def _validate_addresses(
    raw_addresses: Iterable[str], *, allow_private_hosts: bool
) -> tuple[IPv4Address | IPv6Address, ...]:
    addresses: list[IPv4Address | IPv6Address] = []
    seen: set[IPv4Address | IPv6Address] = set()
    for raw_address in raw_addresses:
        try:
            address = ip_address(raw_address)
        except ValueError as exc:
            raise UnsafeDestinationError("adapter host resolved to an invalid address") from exc
        if address in seen:
            continue
        seen.add(address)
        addresses.append(address)

    if not addresses:
        raise UnsafeDestinationError("adapter host could not be resolved")
    if not allow_private_hosts and any(not address.is_global for address in addresses):
        raise UnsafeDestinationError("private adapter hosts are disabled")
    # Validate every bounded DNS answer above, but only attempt a small prefix.
    # A hostile record must not multiply one connect budget hundreds of times.
    return tuple(addresses[:MAX_CONNECT_CANDIDATES])


def _timeout_deadline(timeout: float | None) -> float | None:
    return time.monotonic() + max(timeout, 0.0) if timeout is not None else None


def _remaining_timeout(timeout: float | None, deadline: float | None) -> float | None:
    if deadline is None:
        return timeout
    remaining = max(0.0, deadline - time.monotonic())
    return remaining if timeout is None else min(timeout, remaining)


class _DeadlineSyncStream(httpcore.NetworkStream):
    """Apply one absolute request deadline to TCP, TLS, writes, and reads."""

    def __init__(self, source: httpcore.NetworkStream, *, deadline: float) -> None:
        self._source = source
        self._deadline = deadline

    def _timeout(
        self,
        requested: float | None,
        error_type: type[httpcore.TimeoutException],
    ) -> float:
        remaining = _remaining_timeout(requested, self._deadline)
        if remaining is None or remaining <= 0:
            raise error_type("total request timeout exceeded")
        return remaining

    def read(self, max_bytes: int, timeout: float | None = None) -> bytes:
        return self._source.read(max_bytes, self._timeout(timeout, httpcore.ReadTimeout))

    def write(self, buffer: bytes, timeout: float | None = None) -> None:
        self._source.write(buffer, self._timeout(timeout, httpcore.WriteTimeout))

    def close(self) -> None:
        self._source.close()

    def start_tls(
        self,
        ssl_context: ssl.SSLContext,
        server_hostname: str | None = None,
        timeout: float | None = None,
    ) -> httpcore.NetworkStream:
        stream = self._source.start_tls(
            ssl_context,
            server_hostname=server_hostname,
            timeout=self._timeout(timeout, httpcore.ConnectTimeout),
        )
        return _DeadlineSyncStream(stream, deadline=self._deadline)

    def get_extra_info(self, info: str) -> Any:
        return self._source.get_extra_info(info)


class _PinnedSyncBackend(SyncBackend):
    def __init__(
        self,
        *,
        allow_private_hosts: bool,
        total_timeout_s: float | None = None,
    ) -> None:
        super().__init__()
        self._allow_private_hosts = allow_private_hosts
        self._total_timeout_s = total_timeout_s

    def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options: Iterable[SOCKET_OPTION] | None = None,
    ) -> httpcore.NetworkStream:
        started = time.monotonic()
        connect_deadline = started + max(timeout, 0.0) if timeout is not None else None
        total_deadline = (
            started + self._total_timeout_s if self._total_timeout_s is not None else None
        )
        deadlines = [
            deadline for deadline in (connect_deadline, total_deadline) if deadline is not None
        ]
        effective_deadline = min(deadlines) if deadlines else None
        resolution_timeout = _remaining_timeout(timeout, effective_deadline)
        if resolution_timeout is not None and resolution_timeout <= 0:
            raise httpcore.ConnectTimeout("total connect timeout exceeded")
        try:
            addresses = resolve_destination(
                host,
                port,
                allow_private_hosts=self._allow_private_hosts,
                timeout_s=resolution_timeout,
            )
        except UnsafeDestinationError as exc:
            raise httpcore.ConnectError(str(exc)) from exc

        last_error: httpcore.ConnectError | httpcore.ConnectTimeout | None = None
        for address in addresses:
            remaining = _remaining_timeout(timeout, effective_deadline)
            if remaining is not None and remaining <= 0:
                raise httpcore.ConnectTimeout("total connect timeout exceeded")
            try:
                stream = super().connect_tcp(
                    str(address),
                    port,
                    timeout=remaining,
                    local_address=local_address,
                    socket_options=socket_options,
                )
                return (
                    _DeadlineSyncStream(stream, deadline=total_deadline)
                    if total_deadline is not None
                    else stream
                )
            except (httpcore.ConnectError, httpcore.ConnectTimeout) as exc:
                last_error = exc
        assert last_error is not None
        raise last_error


class _PinnedAsyncBackend(AutoBackend):
    def __init__(self, *, allow_private_hosts: bool) -> None:
        self._allow_private_hosts = allow_private_hosts

    async def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options: Iterable[SOCKET_OPTION] | None = None,
    ) -> httpcore.AsyncNetworkStream:
        deadline = _timeout_deadline(timeout)
        resolution_timeout = _remaining_timeout(timeout, deadline)
        if resolution_timeout is not None and resolution_timeout <= 0:
            raise httpcore.ConnectTimeout("total connect timeout exceeded")
        try:
            addresses = await _resolve_destination_async(
                host,
                port,
                allow_private_hosts=self._allow_private_hosts,
                timeout_s=resolution_timeout,
            )
        except UnsafeDestinationError as exc:
            raise httpcore.ConnectError(str(exc)) from exc

        last_error: httpcore.ConnectError | httpcore.ConnectTimeout | None = None
        for address in addresses:
            remaining = _remaining_timeout(timeout, deadline)
            if remaining is not None and remaining <= 0:
                raise httpcore.ConnectTimeout("total connect timeout exceeded")
            try:
                return await super().connect_tcp(
                    str(address),
                    port,
                    timeout=remaining,
                    local_address=local_address,
                    socket_options=socket_options,
                )
            except (httpcore.ConnectError, httpcore.ConnectTimeout) as exc:
                last_error = exc
        assert last_error is not None
        raise last_error


class SafeHTTPTransport(httpx.HTTPTransport):
    """HTTPX sync transport whose TCP backend validates and pins DNS answers."""

    def __init__(
        self,
        *,
        allow_private_hosts: bool = False,
        verify: ssl.SSLContext | str | bool = True,
        total_timeout_s: float | None = None,
    ) -> None:
        if total_timeout_s is not None and (
            not math.isfinite(total_timeout_s) or total_timeout_s <= 0
        ):
            raise ValueError("total_timeout_s must be finite and greater than zero")
        super().__init__(verify=verify, trust_env=False)
        self._pool._network_backend = _PinnedSyncBackend(  # pyright: ignore[reportPrivateUsage]
            allow_private_hosts=allow_private_hosts,
            total_timeout_s=total_timeout_s,
        )


class SafeAsyncHTTPTransport(httpx.AsyncHTTPTransport):
    """HTTPX async transport whose TCP backend validates and pins DNS answers."""

    def __init__(
        self,
        *,
        allow_private_hosts: bool = False,
        verify: ssl.SSLContext | str | bool = True,
    ) -> None:
        super().__init__(verify=verify, trust_env=False)
        self._pool._network_backend = _PinnedAsyncBackend(  # pyright: ignore[reportPrivateUsage]
            allow_private_hosts=allow_private_hosts
        )


def safe_http_client(
    *,
    allow_private_hosts: bool = False,
    verify: ssl.SSLContext | str | bool = True,
    total_timeout_s: float | None = None,
    **kwargs: Any,
) -> httpx.Client:
    """Build a sync HTTPX client with DNS pinning and no environment proxy."""

    return httpx.Client(
        transport=SafeHTTPTransport(
            allow_private_hosts=allow_private_hosts,
            verify=verify,
            total_timeout_s=total_timeout_s,
        ),
        trust_env=False,
        **kwargs,
    )


def safe_async_http_client(
    *,
    allow_private_hosts: bool = False,
    verify: ssl.SSLContext | str | bool = True,
    **kwargs: Any,
) -> httpx.AsyncClient:
    """Build an async HTTPX client with DNS pinning and no environment proxy."""

    return httpx.AsyncClient(
        transport=SafeAsyncHTTPTransport(
            allow_private_hosts=allow_private_hosts,
            verify=verify,
        ),
        trust_env=False,
        **kwargs,
    )


class _PinnedUrllib3ConnectionMixin:
    _allow_private_hosts = False

    def _new_conn(self) -> socket.socket:
        original_timeout = self.timeout  # pyright: ignore[reportAttributeAccessIssue]
        numeric_timeout = (
            float(original_timeout) if isinstance(original_timeout, int | float) else None
        )
        deadline = _timeout_deadline(numeric_timeout)
        try:
            addresses = resolve_destination(
                self._dns_host,
                self.port,  # pyright: ignore[reportAttributeAccessIssue]
                allow_private_hosts=self._allow_private_hosts,
                timeout_s=numeric_timeout,
            )
        except UnsafeDestinationError as exc:
            raise NewConnectionError(cast(HTTPConnection, self), str(exc)) from exc

        original_host = self._dns_host
        last_error: NewConnectionError | ConnectTimeoutError | None = None
        try:
            for address in addresses:
                remaining = _remaining_timeout(numeric_timeout, deadline)
                if remaining is not None and remaining <= 0:
                    raise ConnectTimeoutError(
                        cast(HTTPConnection, self),
                        "total connect timeout exceeded across resolved addresses",
                    )
                self._dns_host = str(address)
                self.timeout = remaining  # pyright: ignore[reportAttributeAccessIssue]
                try:
                    # HTTPSConnection.connect() uses ``self.host`` (the original
                    # hostname) for SNI after this numeric-address socket opens.
                    return super()._new_conn()  # pyright: ignore[reportAttributeAccessIssue]
                except (NewConnectionError, ConnectTimeoutError) as exc:
                    last_error = exc
        finally:
            self._dns_host = original_host
            self.timeout = original_timeout  # pyright: ignore[reportAttributeAccessIssue]
        assert last_error is not None
        raise last_error


class _PinnedHTTPConnection(_PinnedUrllib3ConnectionMixin, HTTPConnection):
    pass


class _PinnedHTTPSConnection(_PinnedUrllib3ConnectionMixin, HTTPSConnection):
    pass


class _PrivatePinnedHTTPConnection(_PinnedHTTPConnection):
    _allow_private_hosts = True


class _PrivatePinnedHTTPSConnection(_PinnedHTTPSConnection):
    _allow_private_hosts = True


class _PinnedHTTPConnectionPool(HTTPConnectionPool):
    ConnectionCls = _PinnedHTTPConnection  # pyright: ignore[reportAssignmentType]


class _PinnedHTTPSConnectionPool(HTTPSConnectionPool):
    ConnectionCls = _PinnedHTTPSConnection  # pyright: ignore[reportAssignmentType]


class _PrivatePinnedHTTPConnectionPool(HTTPConnectionPool):
    ConnectionCls = _PrivatePinnedHTTPConnection  # pyright: ignore[reportAssignmentType]


class _PrivatePinnedHTTPSConnectionPool(HTTPSConnectionPool):
    ConnectionCls = _PrivatePinnedHTTPSConnection  # pyright: ignore[reportAssignmentType]


class SafePoolManager(urllib3.PoolManager):
    """urllib3 pool manager with the same connect-time DNS policy."""

    def __init__(self, *, allow_private_hosts: bool = False, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        if allow_private_hosts:
            self.pool_classes_by_scheme = {
                "http": _PrivatePinnedHTTPConnectionPool,
                "https": _PrivatePinnedHTTPSConnectionPool,
            }
        else:
            self.pool_classes_by_scheme = {
                "http": _PinnedHTTPConnectionPool,
                "https": _PinnedHTTPSConnectionPool,
            }

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
from ipaddress import IPv4Address, IPv6Address, ip_address, ip_network
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
        "api.metadata.cloud.ibm.com",
        "instance-data.ec2.internal",
        "metadata.tencentyun.com",
    }
)
DENIED_HOST_SUFFIXES = (".metadata.google.internal",)
UNCONDITIONALLY_DENIED_HOSTS = frozenset(
    {
        "metadata",
        "metadata.google.internal",
        "api.metadata.cloud.ibm.com",
        "instance-data.ec2.internal",
        "metadata.tencentyun.com",
    }
)
UNCONDITIONALLY_DENIED_HOST_SUFFIXES = (".metadata.google.internal",)
_APPROVED_PRIVATE_NETWORKS = (
    ip_network("10.0.0.0/8"),
    ip_network("172.16.0.0/12"),
    ip_network("192.168.0.0/16"),
    ip_network("fc00::/7"),
)
_UNCONDITIONALLY_DENIED_ADDRESSES = frozenset(
    {
        # Cloud instance metadata/control-plane endpoints remain forbidden even
        # when an operator approves private application targets.
        ip_address("169.254.169.254"),
        ip_address("fd00:ec2::254"),
        ip_address("fd20:ce::254"),
        ip_address("168.63.129.16"),
        ip_address("100.100.100.200"),
        ip_address("192.0.0.192"),
    }
)
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
_DESTINATION_ERROR_DETAILS = frozenset(
    {
        "adapter host could not be resolved",
        "adapter host DNS resolution timed out",
        "adapter host is unconditionally forbidden",
        "private adapter hosts are disabled",
        "adapter host resolved to an invalid address",
        "adapter host resolved to a forbidden address",
        "destination host is empty",
        "destination host is invalid",
        "destination port is invalid",
    }
)


class UnsafeDestinationError(ValueError):
    """A destination cannot be connected to under the outbound network policy."""


def _safe_destination_error_detail(exc: UnsafeDestinationError) -> str:
    """Project only fixed module-owned policy errors across transport layers."""

    if type(exc) is UnsafeDestinationError:
        try:
            arguments = BaseException.__dict__["args"].__get__(exc, UnsafeDestinationError)
        except Exception:
            arguments = ()
        if (
            type(arguments) is tuple
            and len(arguments) == 1
            and type(arguments[0]) is str
            and arguments[0] in _DESTINATION_ERROR_DETAILS
        ):
            return arguments[0]
    return "adapter host could not be resolved"


def _dns_command(host: str, port: int) -> tuple[str, ...]:
    return (sys.executable, "-I", "-c", _DNS_CHILD_CODE, host, str(port))


def _parse_dns_output(output: bytes) -> list[str]:
    decoded: Any = None
    malformed = False
    try:
        decoded = json.loads(output.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        malformed = True
    if malformed:
        raise UnsafeDestinationError("adapter host could not be resolved")
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
        completed: subprocess.CompletedProcess[bytes] | None = None
        timed_out = False
        try:
            completed = subprocess.run(  # noqa: S603 — fixed interpreter/script, argv only
                _dns_command(host, port),
                check=False,
                capture_output=True,
                timeout=remaining,
                env=dict(_DNS_WORKER_ENV),
            )
        except subprocess.TimeoutExpired:
            timed_out = True
        if timed_out:
            raise UnsafeDestinationError("adapter host DNS resolution timed out")
        if completed is None:  # pragma: no cover - subprocess contract invariant
            raise UnsafeDestinationError("adapter host could not be resolved")
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


async def _spawn_dns_process(host: str, port: int) -> asyncio.subprocess.Process:
    """Transfer child ownership atomically despite cancellation during spawn."""

    spawn_task = asyncio.create_task(
        asyncio.create_subprocess_exec(
            *_dns_command(host, port),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            env=dict(_DNS_WORKER_ENV),
        )
    )
    interrupted = False
    while not spawn_task.done():
        try:
            await asyncio.shield(spawn_task)
        except asyncio.CancelledError:
            interrupted = True
        except BaseException:
            break
    process: asyncio.subprocess.Process | None = None
    error: BaseException | None = None
    try:
        process = spawn_task.result()
    except BaseException as exc:
        error = exc
    if interrupted:
        if process is not None and process.returncode is None:
            await _terminate_dns_process(process)
        raise asyncio.CancelledError from None
    if error is not None:
        raise error
    if process is None:  # pragma: no cover - task result invariant
        raise RuntimeError("DNS resolver process did not start")
    return process


async def _resolve_hostname_async(
    host: str,
    port: int,
    *,
    timeout_s: float = DNS_RESOLUTION_TIMEOUT_S,
) -> list[str]:
    """Async killable DNS resolution; cancellation terminates the child."""

    process: asyncio.subprocess.Process | None = None
    acquired = False
    addresses: list[str] | None = None
    timed_out = False
    try:
        async with asyncio.timeout(min(DNS_RESOLUTION_TIMEOUT_S, max(timeout_s, 0.0))):
            await _acquire_dns_admission()
            acquired = True
            process = await _spawn_dns_process(host, port)
            stdout, _stderr = await process.communicate()
            if process.returncode != 0:
                raise UnsafeDestinationError("adapter host could not be resolved")
            addresses = _parse_dns_output(stdout)
    except TimeoutError:
        timed_out = True
    finally:
        try:
            if process is not None and process.returncode is None:
                await _terminate_dns_process(process)
        finally:
            if acquired:
                _DNS_ADMISSION.release()
    if timed_out:
        raise UnsafeDestinationError("adapter host DNS resolution timed out")
    if addresses is None:  # pragma: no cover - resolver process contract invariant
        raise UnsafeDestinationError("adapter host could not be resolved")
    return addresses


async def _resolve_destination_async(
    host: str,
    port: int,
    *,
    allow_private_hosts: bool,
    timeout_s: float | None = None,
) -> tuple[IPv4Address | IPv6Address, ...]:
    """Resolve without blocking the event loop and validate the complete answer."""

    _validate_destination_port(port)
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

    _validate_destination_port(port)
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
    if (
        type(host) is not str
        or not 1 <= len(host) <= 253
        or host != host.strip()
        or any(ord(character) <= 0x20 or ord(character) == 0x7F for character in host)
    ):
        raise UnsafeDestinationError("destination host is invalid")
    normalized = host.rstrip(".").lower()
    if not normalized:
        raise UnsafeDestinationError("destination host is empty")
    if is_unconditionally_denied_host(normalized):
        # Private-host approval is for an operator-selected application target,
        # never a cloud control-plane credential endpoint.
        raise UnsafeDestinationError("adapter host is unconditionally forbidden")
    if not allow_private_hosts and (
        normalized in DENIED_HOSTS or normalized.endswith(DENIED_HOST_SUFFIXES)
    ):
        raise UnsafeDestinationError("private adapter hosts are disabled")

    try:
        return normalized, ip_address(normalized)
    except ValueError:
        return normalized, None


def _validate_destination_port(port: Any) -> None:
    if type(port) is not int or not 1 <= port <= 65_535:
        raise UnsafeDestinationError("destination port is invalid")


def _validate_addresses(
    raw_addresses: Iterable[str], *, allow_private_hosts: bool
) -> tuple[IPv4Address | IPv6Address, ...]:
    addresses: list[IPv4Address | IPv6Address] = []
    seen: set[IPv4Address | IPv6Address] = set()
    for raw_address in raw_addresses:
        address: IPv4Address | IPv6Address | None = None
        invalid_address = False
        try:
            address = ip_address(raw_address)
        except ValueError:
            invalid_address = True
        if invalid_address:
            raise UnsafeDestinationError("adapter host resolved to an invalid address")
        if address is None:  # pragma: no cover - ipaddress contract invariant
            raise UnsafeDestinationError("adapter host resolved to an invalid address")
        if address in seen:
            continue
        seen.add(address)
        addresses.append(address)

    if not addresses:
        raise UnsafeDestinationError("adapter host could not be resolved")
    if any(
        not destination_address_allowed(
            address,
            allow_private_hosts=allow_private_hosts,
        )
        for address in addresses
    ):
        detail = (
            "adapter host resolved to a forbidden address"
            if allow_private_hosts
            else "private adapter hosts are disabled"
        )
        raise UnsafeDestinationError(detail)
    # Validate every bounded DNS answer above, but only attempt a small prefix.
    # A hostile record must not multiply one connect budget hundreds of times.
    return tuple(addresses[:MAX_CONNECT_CANDIDATES])


def is_unconditionally_denied_host(host: str) -> bool:
    """Return whether a hostname denotes a platform metadata endpoint."""

    if type(host) is not str or not 1 <= len(host) <= 253:
        return True
    normalized = host.rstrip(".").lower()
    return normalized in UNCONDITIONALLY_DENIED_HOSTS or normalized.endswith(
        UNCONDITIONALLY_DENIED_HOST_SUFFIXES
    )


def destination_address_allowed(
    address: IPv4Address | IPv6Address,
    *,
    allow_private_hosts: bool,
) -> bool:
    """Apply an explicit global/RFC1918/loopback/ULA destination policy.

    ``ipaddress.is_private`` includes metadata, documentation, benchmark,
    unspecified, and reserved ranges. Operator approval must not accidentally
    grant all of those. IPv4-mapped IPv6 addresses are assessed as IPv4 so they
    cannot bypass the same range policy.
    """

    if isinstance(address, IPv6Address) and address.scope_id is not None:
        return False
    effective: IPv4Address | IPv6Address = (
        address.ipv4_mapped
        if isinstance(address, IPv6Address) and address.ipv4_mapped is not None
        else address
    )
    if effective in _UNCONDITIONALLY_DENIED_ADDRESSES:
        return False
    if (
        effective.is_unspecified
        or effective.is_link_local
        or effective.is_multicast
        or getattr(effective, "is_site_local", False)
    ):
        return False
    if effective.is_loopback:
        return allow_private_hosts
    if effective.is_reserved:
        return False
    if isinstance(effective, IPv6Address) and (
        effective.sixtofour is not None or effective.teredo is not None
    ):
        return False
    if effective.is_global:
        return True
    return allow_private_hosts and any(
        effective.version == network.version and effective in network
        for network in _APPROVED_PRIVATE_NETWORKS
    )


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
        addresses: tuple[IPv4Address | IPv6Address, ...] | None = None
        resolution_error: str | None = None
        try:
            addresses = resolve_destination(
                host,
                port,
                allow_private_hosts=self._allow_private_hosts,
                timeout_s=resolution_timeout,
            )
        except UnsafeDestinationError as exc:
            resolution_error = _safe_destination_error_detail(exc)
        if resolution_error is not None:
            raise httpcore.ConnectError(resolution_error)
        if addresses is None:  # pragma: no cover - resolver contract invariant
            raise httpcore.ConnectError("adapter host could not be resolved")

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
        addresses: tuple[IPv4Address | IPv6Address, ...] | None = None
        resolution_error: str | None = None
        try:
            addresses = await _resolve_destination_async(
                host,
                port,
                allow_private_hosts=self._allow_private_hosts,
                timeout_s=resolution_timeout,
            )
        except UnsafeDestinationError as exc:
            resolution_error = _safe_destination_error_detail(exc)
        if resolution_error is not None:
            raise httpcore.ConnectError(resolution_error)
        if addresses is None:  # pragma: no cover - resolver contract invariant
            raise httpcore.ConnectError("adapter host could not be resolved")

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
        super().__init__(verify=_normalized_httpx_verify(verify), trust_env=False)
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
        super().__init__(verify=_normalized_httpx_verify(verify), trust_env=False)
        self._pool._network_backend = _PinnedAsyncBackend(  # pyright: ignore[reportPrivateUsage]
            allow_private_hosts=allow_private_hosts
        )


def _normalized_httpx_verify(
    verify: ssl.SSLContext | str | bool,
) -> ssl.SSLContext | bool:
    """Convert a CA-file path to HTTPX's durable SSL-context interface."""

    if isinstance(verify, str):
        return ssl.create_default_context(cafile=verify)
    return verify


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
        addresses: tuple[IPv4Address | IPv6Address, ...] | None = None
        resolution_error: str | None = None
        try:
            addresses = resolve_destination(
                self._dns_host,
                self.port,  # pyright: ignore[reportAttributeAccessIssue]
                allow_private_hosts=self._allow_private_hosts,
                timeout_s=numeric_timeout,
            )
        except UnsafeDestinationError as exc:
            resolution_error = _safe_destination_error_detail(exc)
        if resolution_error is not None:
            raise NewConnectionError(cast(HTTPConnection, self), resolution_error)
        if addresses is None:  # pragma: no cover - resolver contract invariant
            raise NewConnectionError(
                cast(HTTPConnection, self), "adapter host could not be resolved"
            )

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

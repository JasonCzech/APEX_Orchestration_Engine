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
import socket
import ssl
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


class UnsafeDestinationError(ValueError):
    """A destination cannot be connected to under the outbound network policy."""


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
) -> tuple[IPv4Address | IPv6Address, ...]:
    """Resolve ``host`` once and return a validated, deterministic address set.

    Every returned address must be acceptable.  Rejecting mixed public/private
    answers prevents a resolver from steering individual connection attempts to
    a private address while leaving a public address present as camouflage.
    """

    normalized = host.rstrip(".").lower()
    if not normalized:
        raise UnsafeDestinationError("destination host is empty")
    if not allow_private_hosts and (
        normalized in DENIED_HOSTS or normalized.endswith(DENIED_HOST_SUFFIXES)
    ):
        raise UnsafeDestinationError("private adapter hosts are disabled")

    try:
        raw_addresses = [str(ip_address(normalized))]
    except ValueError:
        try:
            infos = socket.getaddrinfo(normalized, port, type=socket.SOCK_STREAM)
        except OSError as exc:
            raise UnsafeDestinationError("adapter host could not be resolved") from exc
        raw_addresses = [str(info[4][0]) for info in infos if info and info[4]]

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
    return tuple(addresses)


class _PinnedSyncBackend(SyncBackend):
    def __init__(self, *, allow_private_hosts: bool) -> None:
        super().__init__()
        self._allow_private_hosts = allow_private_hosts

    def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options: Iterable[SOCKET_OPTION] | None = None,
    ) -> httpcore.NetworkStream:
        try:
            addresses = resolve_destination(
                host, port, allow_private_hosts=self._allow_private_hosts
            )
        except UnsafeDestinationError as exc:
            raise httpcore.ConnectError(str(exc)) from exc

        last_error: httpcore.ConnectError | httpcore.ConnectTimeout | None = None
        for address in addresses:
            try:
                return super().connect_tcp(
                    str(address),
                    port,
                    timeout=timeout,
                    local_address=local_address,
                    socket_options=socket_options,
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
        try:
            addresses = await asyncio.to_thread(
                resolve_destination,
                host,
                port,
                allow_private_hosts=self._allow_private_hosts,
            )
        except UnsafeDestinationError as exc:
            raise httpcore.ConnectError(str(exc)) from exc

        last_error: httpcore.ConnectError | httpcore.ConnectTimeout | None = None
        for address in addresses:
            try:
                return await super().connect_tcp(
                    str(address),
                    port,
                    timeout=timeout,
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
    ) -> None:
        super().__init__(verify=verify, trust_env=False)
        self._pool._network_backend = _PinnedSyncBackend(  # pyright: ignore[reportPrivateUsage]
            allow_private_hosts=allow_private_hosts
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
    **kwargs: Any,
) -> httpx.Client:
    """Build a sync HTTPX client with DNS pinning and no environment proxy."""

    return httpx.Client(
        transport=SafeHTTPTransport(
            allow_private_hosts=allow_private_hosts,
            verify=verify,
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
        try:
            addresses = resolve_destination(
                self._dns_host,
                self.port,  # pyright: ignore[reportAttributeAccessIssue]
                allow_private_hosts=self._allow_private_hosts,
            )
        except UnsafeDestinationError as exc:
            raise NewConnectionError(cast(HTTPConnection, self), str(exc)) from exc

        original_host = self._dns_host
        last_error: NewConnectionError | ConnectTimeoutError | None = None
        try:
            for address in addresses:
                self._dns_host = str(address)
                try:
                    # HTTPSConnection.connect() uses ``self.host`` (the original
                    # hostname) for SNI after this numeric-address socket opens.
                    return super()._new_conn()  # pyright: ignore[reportAttributeAccessIssue]
                except (NewConnectionError, ConnectTimeoutError) as exc:
                    last_error = exc
        finally:
            self._dns_host = original_host
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

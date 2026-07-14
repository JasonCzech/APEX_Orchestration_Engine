"""SSRF-guarded HTTP fetch backing the analysis agent's ``fetch_results`` tool.

Deny-by-default: a URL is fetched only when its host is in the configured
allow-list (``LLMSettings.fetch_allowed_hosts``) and does not resolve to a
private/loopback/link-local address (unless explicitly permitted). With no
allow-list configured the guard rejects every URL, so the tool is inert by
default — callers opt in per deployment.
"""

import ipaddress
import socket
from collections.abc import Iterable
from urllib.parse import urlsplit

import httpx

from apex.adapters.network_safety import safe_http_client

_ALLOWED_SCHEMES = ("https", "http")


class FetchError(Exception):
    """A results URL was rejected by the SSRF guard or failed to fetch."""


def _host_resolves_private(host: str) -> bool:
    """True if `host` is, or resolves to, a private/loopback/link-local/reserved IP."""
    try:
        candidates: list[str] = [str(ipaddress.ip_address(host))]
    except ValueError:
        try:
            candidates = [str(info[4][0]) for info in socket.getaddrinfo(host, None)]
        except OSError as exc:
            raise FetchError(f"could not resolve host {host!r}") from exc
    for addr in candidates:
        try:
            ip = ipaddress.ip_address(addr)
        except ValueError:
            continue
        if (
            ip.is_private
            or ip.is_loopback
            or ip.is_link_local
            or ip.is_reserved
            or ip.is_multicast
            or ip.is_unspecified
        ):
            return True
    return False


def validate_fetch_url(
    url: str, *, allowed_hosts: Iterable[str], allow_private: bool = False
) -> str:
    """Return the normalized host if `url` passes the SSRF guard, else raise FetchError."""
    parts = urlsplit(url)
    if parts.scheme not in _ALLOWED_SCHEMES:
        raise FetchError(f"unsupported URL scheme {parts.scheme or '(none)'!r}")
    host = (parts.hostname or "").lower()
    if not host:
        raise FetchError("URL has no host")
    allowed = {entry.strip().lower() for entry in allowed_hosts if entry.strip()}
    if not allowed:
        raise FetchError("results fetch is disabled (no allow-listed hosts configured)")
    if host not in allowed:
        raise FetchError(f"host {host!r} is not in the results-fetch allow-list")
    if not allow_private and _host_resolves_private(host):
        raise FetchError(f"host {host!r} resolves to a private/loopback address")
    return host


def fetch_results_text(
    url: str,
    *,
    allowed_hosts: Iterable[str],
    allow_private: bool = False,
    max_bytes: int = 1_000_000,
    timeout_s: float = 20.0,
) -> str:
    """Fetch a results URL after SSRF validation; return up to `max_bytes` of text.

    Redirects are not followed (a 3xx is rejected) so a redirect cannot bounce the
    request to a host outside the allow-list.
    """
    validate_fetch_url(url, allowed_hosts=allowed_hosts, allow_private=allow_private)
    if max_bytes < 1:
        raise FetchError("max_bytes must be >= 1")
    try:
        with safe_http_client(
            timeout=timeout_s,
            follow_redirects=False,
            allow_private_hosts=allow_private,
        ) as client:
            content = bytearray()
            with client.stream("GET", url) as response:
                if response.is_redirect:
                    raise FetchError(f"refusing to follow redirect from {url!r}")
                response.raise_for_status()
                for chunk in response.iter_bytes():
                    remaining = max_bytes - len(content)
                    if remaining <= 0:
                        break
                    content.extend(chunk[:remaining])
                    if len(chunk) > remaining:
                        break
    except httpx.HTTPError as exc:
        raise FetchError(f"failed to fetch {url!r}: {exc}") from exc
    return bytes(content).decode("utf-8", errors="replace")

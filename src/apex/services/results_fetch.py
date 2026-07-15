"""SSRF-guarded HTTP fetch backing the analysis agent's ``fetch_results`` tool.

Deny-by-default: a URL is fetched only when its host is in the configured
allow-list (``LLMSettings.fetch_allowed_hosts``) and does not resolve to a
private/loopback/link-local address (unless explicitly permitted). With no
allow-list configured the guard rejects every URL, so the tool is inert by
default — callers opt in per deployment.
"""

import ipaddress
import math
import time
from collections.abc import Iterable
from urllib.parse import urlsplit, urlunsplit

import httpx

from apex.adapters.network_safety import safe_http_client
from apex.settings import (
    MAX_FETCH_ALLOWED_HOSTS,
    normalize_fetch_allowed_host,
)

_ALLOWED_SCHEMES = ("https", "http")


class FetchError(Exception):
    """A results URL was rejected by the SSRF guard or failed to fetch."""


def redact_fetch_url(url: str) -> str:
    """Return only a URL's scheme and host, never credentials/query/path tokens."""

    try:
        parts = urlsplit(url)
        host = parts.hostname or ""
        port = parts.port
    except ValueError:
        return "<invalid-url>"
    if not host:
        return "<invalid-url>"
    display_host = f"[{host}]" if ":" in host and not host.startswith("[") else host
    netloc = f"{display_host}:{port}" if port is not None else display_host
    return urlunsplit((parts.scheme, netloc, "", "", ""))


def _host_resolves_private(host: str) -> bool:
    """Reject unsafe numeric literals; hostnames are checked at socket connect."""
    try:
        candidates: list[str] = [str(ipaddress.ip_address(host))]
    except ValueError:
        return False
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
    url: str,
    *,
    allowed_hosts: Iterable[str],
    allow_private: bool = False,
    require_https: bool = False,
) -> str:
    """Return the normalized host if `url` passes the SSRF guard, else raise FetchError."""
    if (
        not isinstance(url, str)
        or not url
        or url != url.strip()
        or len(url) > 4_096
        or "\\" in url
        or any(ord(char) <= 0x20 or ord(char) == 0x7F for char in url)
    ):
        raise FetchError("malformed results URL")
    try:
        parts = urlsplit(url)
        # Accessing `.port` validates malformed/out-of-range ports eagerly.
        _ = parts.port
    except ValueError as exc:
        raise FetchError("malformed results URL") from exc
    if parts.scheme not in _ALLOWED_SCHEMES:
        raise FetchError(f"unsupported URL scheme {parts.scheme or '(none)'!r}")
    if require_https and parts.scheme != "https":
        raise FetchError("results URLs must use https in locked environments")
    if parts.username is not None or parts.password is not None:
        raise FetchError("results URLs must not contain embedded credentials")
    try:
        host = normalize_fetch_allowed_host(parts.hostname or "")
    except ValueError as exc:
        raise FetchError("URL has an invalid host") from exc
    allowed: set[str] = set()
    for index, entry in enumerate(allowed_hosts):
        if index >= MAX_FETCH_ALLOWED_HOSTS:
            raise FetchError(
                f"results-fetch allow-list exceeds the {MAX_FETCH_ALLOWED_HOSTS}-host limit"
            )
        try:
            allowed.add(normalize_fetch_allowed_host(entry))
        except ValueError as exc:
            raise FetchError("results-fetch allow-list contains an invalid host") from exc
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
    require_https: bool = False,
    max_bytes: int = 1_000_000,
    timeout_s: float = 20.0,
) -> str:
    """Fetch a results URL after SSRF validation; return up to `max_bytes` of text.

    Redirects are not followed (a 3xx is rejected) so a redirect cannot bounce the
    request to a host outside the allow-list.
    """
    validate_fetch_url(
        url,
        allowed_hosts=allowed_hosts,
        allow_private=allow_private,
        require_https=require_https,
    )
    safe_url = redact_fetch_url(url)
    if max_bytes < 1:
        raise FetchError("max_bytes must be >= 1")
    if not math.isfinite(timeout_s) or timeout_s <= 0:
        raise FetchError("timeout_s must be a finite value greater than zero")
    deadline = time.monotonic() + timeout_s
    try:
        with safe_http_client(
            timeout=timeout_s,
            total_timeout_s=timeout_s,
            follow_redirects=False,
            allow_private_hosts=allow_private,
        ) as client:
            content = bytearray()
            with client.stream("GET", url, headers={"Accept-Encoding": "identity"}) as response:
                if time.monotonic() >= deadline:
                    raise httpx.ReadTimeout(
                        "total results-fetch timeout exceeded",
                        request=response.request,
                    )
                if response.is_redirect:
                    raise FetchError(f"refusing to follow redirect from {safe_url!r}")
                response.raise_for_status()
                content_encoding = response.headers.get("content-encoding", "identity")
                if content_encoding.strip().casefold() not in {"", "identity"}:
                    raise FetchError("results server ignored the identity encoding requirement")
                content_length = response.headers.get("content-length")
                if content_length is not None:
                    try:
                        declared_length = int(content_length)
                    except ValueError:
                        declared_length = -1
                    if declared_length > max_bytes:
                        raise FetchError(f"results response exceeds the {max_bytes}-byte limit")
                for chunk in response.iter_bytes():
                    if time.monotonic() >= deadline:
                        raise httpx.ReadTimeout(
                            "total results-fetch timeout exceeded",
                            request=response.request,
                        )
                    remaining = max_bytes - len(content)
                    if remaining <= 0:
                        break
                    content.extend(chunk[:remaining])
                    if len(content) >= max_bytes:
                        break
                if len(content) < max_bytes and time.monotonic() >= deadline:
                    raise httpx.ReadTimeout(
                        "total results-fetch timeout exceeded",
                        request=response.request,
                    )
    except httpx.HTTPError as exc:
        safe_detail = (
            str(exc)
            if str(exc)
            in {
                "private adapter hosts are disabled",
                "adapter host could not be resolved",
            }
            else exc.__class__.__name__
        )
        raise FetchError(f"failed to fetch {safe_url!r} ({safe_detail})") from exc
    return bytes(content).decode("utf-8", errors="replace")

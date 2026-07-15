"""Small option-parsing helpers shared by adapters."""

from __future__ import annotations

from typing import Any
from urllib.parse import urlsplit

_TRUE_STRINGS = {"true", "1", "yes", "on", "y"}
_FALSE_STRINGS = {"false", "0", "no", "off", "n", ""}


def require_bounded_credential(
    value: Any,
    *,
    label: str,
    max_bytes: int = 16_384,
    header_token: bool = False,
) -> str:
    """Validate provider credentials before an SDK/header boundary.

    ``SecretValue`` intentionally has a generous generic ceiling. Individual
    adapters need a much smaller transport limit so a corrupted environment
    value cannot allocate an enormous Authorization header or defer CR/LF and
    non-ASCII failures until provider I/O. Error messages never include input.
    """

    if not isinstance(value, str) or not value:
        raise ValueError(f"{label} must be a non-empty string")
    try:
        encoded = value.encode("utf-8")
    except UnicodeError as exc:
        raise ValueError(f"{label} must contain valid UTF-8 text") from exc
    if len(encoded) > max_bytes:
        raise ValueError(f"{label} must not exceed {max_bytes} UTF-8 bytes")
    if any(ord(character) < 0x20 or ord(character) == 0x7F for character in value):
        raise ValueError(f"{label} contains invalid control characters")
    if header_token and any(not 0x21 <= ord(character) <= 0x7E for character in value):
        raise ValueError(f"{label} must contain visible ASCII header characters")
    return value


def normalize_host_port_endpoint(value: Any, *, secure: bool) -> tuple[str, bool]:
    """Normalize an S3/MinIO endpoint to the SDK's ``host[:port]`` contract.

    Operators commonly supply either ``minio.example:9000`` or an http(s) URL.
    A URL scheme is accepted only when it carries no path, query, fragment, or
    userinfo, then stripped while its scheme becomes the effective TLS setting.
    """

    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or len(value) > 4_096
        or "\\" in value
        or any(ord(character) < 0x21 or ord(character) == 0x7F for character in value)
    ):
        raise ValueError("s3 endpoint must be a bounded host[:port] or http(s) URL")
    has_scheme = "://" in value
    candidate = value if has_scheme else f"{'https' if secure else 'http'}://{value}"
    try:
        parsed = urlsplit(candidate)
        port = parsed.port
    except (TypeError, ValueError) as exc:
        raise ValueError("s3 endpoint must contain a valid host and optional port") from exc
    if (
        parsed.scheme not in {"http", "https"}
        or parsed.hostname is None
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or parsed.path not in {"", "/"}
        or parsed.netloc.endswith(":")
        or (port is not None and not 1 <= port <= 65_535)
    ):
        raise ValueError(
            "s3 endpoint must not contain credentials, a path, query, or fragment"
        )
    hostname = parsed.hostname
    if any(character in hostname for character in "/\\?#@"):
        raise ValueError("s3 endpoint contains an invalid host")
    host = f"[{hostname}]" if ":" in hostname else hostname
    endpoint = f"{host}:{port}" if port is not None else host
    return endpoint, parsed.scheme == "https" if has_scheme else secure


def coerce_bool(value: Any, *, default: bool) -> bool:
    """Interpret a connection-option value as a bool.

    JSON booleans and numbers pass through; strings are parsed case-insensitively so
    a ``"false"`` / ``"0"`` / ``"no"`` option is honored as ``False`` instead of
    ``bool("false")`` silently evaluating to ``True``. Unrecognized values fall back
    to ``default``.
    """
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        text = value.strip().lower()
        if text in _TRUE_STRINGS:
            return True
        if text in _FALSE_STRINGS:
            return False
    return default

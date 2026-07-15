"""Shared validation for connection credential references.

Both the HTTP administration API and declarative bootstrap write the same
``connections`` table.  Keeping their credential validation here prevents a
bootstrap ConfigMap from bypassing the API's secret-free contract.
"""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import urlsplit

from apex.adapters.options import normalize_host_port_endpoint
from apex.domain.input_limits import validate_json_object

# The only registered SecretsPort provider is EnvSecretsAdapter. Persisting a
# vault:/file: locator currently creates a connection that passes validation but
# can never be resolved. Expand this grammar only when a provider with that
# scheme is shipped (and advertise its capability at this boundary).
_SECRET_REF_RE = re.compile(r"env:[A-Za-z_][A-Za-z0-9_]{0,254}")
_SECRET_NAME_MARKERS = {
    "auth",
    "basicauth",
    "httpauth",
    "password",
    "token",
    "secret",
    "secretkey",
    "apikey",
    "clientsecret",
    "bearertoken",
    "privatekey",
    "credential",
    "pat",
    "bearer",
    "jwt",
    "psk",
    "sharedkey",
    "accountkey",
    "storagekey",
    "subscriptionkey",
    "sessionkey",
    "sessionid",
    "clientcertificate",
    "clientcert",
    "privatepem",
    "pfx",
    "pkcs12",
    "keystore",
    "signature",
    "cookie",
    "authheader",
}
_SECRET_NAME_SUBSTRINGS = (
    "password",
    "token",
    "secret",
    "apikey",
    "credential",
    "authorization",
    "bearer",
    "sharedkey",
    "accountkey",
    "storagekey",
    "subscriptionkey",
    "sessionkey",
    "sessionid",
    "clientcertificate",
    "clientcert",
    "privatepem",
    "signature",
    "cookie",
    "authheader",
)
_SECRET_NAME_SUFFIXES = ("pat", "jwt", "psk", "pfx", "pkcs12", "keystore")
_REDACTED = "[REDACTED]"
_REPAIR_REQUIRED_OPTIONS = {"_apex_repair_required": True}


def _normalized_option_key(key: str) -> str:
    return "".join(character for character in key.casefold() if character.isalnum())


def _is_secret_option_key(key: str) -> bool:
    normalized = _normalized_option_key(key)
    return (
        normalized in _SECRET_NAME_MARKERS
        or any(marker in normalized for marker in _SECRET_NAME_SUBSTRINGS)
        or any(normalized.endswith(suffix) for suffix in _SECRET_NAME_SUFFIXES)
    )


def connection_url_requires_repair(value: Any, *, allow_bare_host: bool = False) -> bool:
    """Return whether a persisted transport URL is unsafe to serialize or delegate.

    Legacy rows predate the current request validators and can contain credentials
    in URI userinfo, query, or fragment components. Invalid values are also treated
    as repair-only so an arbitrary string in a URL column cannot be reflected as
    innocuous connection metadata.
    """

    if value is None:
        return False
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or len(value) > 4_096
        or "\\" in value
        or any(ord(character) < 0x20 or ord(character) == 0x7F for character in value)
    ):
        return True
    candidate = value
    if allow_bare_host and "://" not in candidate:
        candidate = f"https://{candidate}"
    try:
        parsed = urlsplit(candidate)
        port = parsed.port
    except (TypeError, ValueError):
        return True
    return bool(
        parsed.scheme not in {"http", "https"}
        or parsed.hostname is None
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or (port is not None and not 1 <= port <= 65_535)
    )


def connection_options_require_repair(options: Any) -> bool:
    """Detect malformed or credential-bearing options on legacy rows."""

    if not isinstance(options, dict):
        return True
    try:
        validate_json_object(options, label="connection options")
    except ValueError:
        return True

    stack: list[Any] = [options]
    while stack:
        value = stack.pop()
        if isinstance(value, dict):
            for key, nested in value.items():
                # This marker is emitted only by output sanitizers.  Treating a
                # caller-supplied copy as ordinary configuration would let the
                # write path and the output/runtime quarantine predicates
                # disagree about the same persisted target.
                if key == "_apex_repair_required":
                    return True
                if _is_secret_option_key(key):
                    return True
                normalized = _normalized_option_key(key)
                if normalized == "baseurl" and connection_url_requires_repair(nested):
                    return True
                if normalized == "endpoint":
                    try:
                        normalize_host_port_endpoint(nested, secure=False)
                    except ValueError:
                        return True
                if (
                    isinstance(nested, str)
                    and nested.casefold().startswith(("http://", "https://"))
                    and connection_url_requires_repair(nested)
                ):
                    return True
                stack.append(nested)
        elif isinstance(value, list):
            stack.extend(value)
    return False


def environment_target_requires_repair(base_url: Any, options: Any) -> bool:
    """Atomically classify legacy environment execution-target metadata.

    Environments and connections share the same outbound URL/credential threat
    boundary, but old environment rows stored the URL and options separately.
    Callers must treat the pair as one unit: a safe-looking URL must not make a
    credential-bearing option object executable (or serializable), and vice
    versa.
    """

    return connection_url_requires_repair(base_url) or connection_options_require_repair(options)


def sanitize_connection_url_for_output(value: Any) -> str | None:
    """Project a legacy URL without ever reflecting credential-bearing input."""

    if value is None:
        return None
    if connection_url_requires_repair(value):
        return _REDACTED
    return value


def sanitize_connection_options_for_output(options: Any) -> dict[str, Any]:
    """Project safe options, replacing a repair-required object atomically."""

    if connection_options_require_repair(options):
        # Redact the complete object rather than trying to preserve siblings: a
        # malformed nested structure must not create a sanitizer bypass.
        return dict(_REPAIR_REQUIRED_OPTIONS)
    return dict(options)


def sanitize_secret_ref_for_output(value: Any) -> str | None:
    """Return supported references while hiding raw/unsupported legacy values."""

    if value is None:
        return None
    if not isinstance(value, str):
        return _REDACTED
    try:
        return validate_secret_ref(value)
    except ValueError:
        return _REDACTED


def validate_secret_ref(value: str | None) -> str | None:
    """Accept only bounded references handled by a registered secret adapter."""

    if value is None:
        return None
    if _SECRET_REF_RE.fullmatch(value) is None:
        raise ValueError("secret_ref must use the supported env:NAME reference format")
    return value


def reject_raw_secret_options(
    options: dict[str, Any],
    *,
    label: str = "connection options",
    reference: str = "secret_ref",
) -> dict[str, Any]:
    """Reject secret-looking keys at any depth and return validated options."""

    validate_json_object(options, label=label)
    stack: list[Any] = [options]
    while stack:
        value = stack.pop()
        if isinstance(value, dict):
            for key, nested in value.items():
                if _is_secret_option_key(key):
                    raise ValueError(f"{label} secrets must be supplied through {reference}")
                stack.append(nested)
        elif isinstance(value, list):
            stack.extend(value)
    return options

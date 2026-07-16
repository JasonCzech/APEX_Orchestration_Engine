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
from apex.domain.diagnostics import bounded_diagnostic, is_credential_field
from apex.domain.input_limits import validate_json_object

# The only registered SecretsPort provider is EnvSecretsAdapter. Persisting a
# vault:/file: locator currently creates a connection that passes validation but
# can never be resolved. Expand this grammar only when a provider with that
# scheme is shipped (and advertise its capability at this boundary).
_SECRET_REF_RE = re.compile(r"env:[A-Za-z_][A-Za-z0-9_]{0,254}")
_NON_SECRET_OPTION_NAMES = frozenset({"accesskey"})
_NON_SECRET_OPTION_SUFFIXES = ("accesskeyid", "accesskeyidentifier")
_TRANSPORT_OPTION_NAMES = frozenset({"baseurl", "endpoint"})
_REDACTED = "[REDACTED]"
_REPAIR_REQUIRED_OPTIONS = {"_apex_repair_required": True}


def _normalized_option_key(key: str) -> str:
    return "".join(character for character in key.casefold() if character.isalnum())


def _is_secret_option_key(key: str) -> bool:
    normalized = _normalized_option_key(key)
    # Access-key IDs are identifiers, not the paired secret-access-key. The S3
    # adapter historically calls that identifier simply ``access_key``; retain
    # both spellings while rejecting every secret-bearing alias recognized by
    # the shared diagnostic boundary.
    if normalized in _NON_SECRET_OPTION_NAMES or any(
        normalized.endswith(suffix) for suffix in _NON_SECRET_OPTION_SUFFIXES
    ):
        return False
    # Keep connection/bootstrap classification identical to the shared
    # diagnostic boundary. In particular, control metadata such as
    # tokenCount/signatureAlgorithm/authenticationMode is not a credential,
    # while terminal *Token/*Signature fields remain credential-bearing.
    return is_credential_field(key)


def _contains_credential_text(value: str) -> bool:
    """Apply the shared diagnostic redactor as a bounded credential detector."""

    # Callers validate the complete object against MAX_JSON_BYTES before this
    # comparison. Matching output therefore means redaction, not truncation.
    return bounded_diagnostic(value, max_chars=max(1, len(value))) != value


def reject_credential_text(value: str | None, *, label: str) -> str | None:
    """Reject credential material hidden in an otherwise ordinary text field."""

    if value is None:
        return None
    if type(value) is not str:
        raise ValueError(f"{label} must be a string or null")
    if _contains_credential_text(value):
        raise ValueError(f"{label} must not contain credential material")
    return value


def sanitize_credential_text_for_output(value: Any) -> str | None:
    """Project a scalar label without reflecting a credential-bearing legacy row."""

    if value is None:
        return None
    if type(value) is not str or _contains_credential_text(value):
        return _REDACTED
    return value


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
        if isinstance(value, str):
            if _contains_credential_text(value):
                return True
        elif isinstance(value, dict):
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
    stack: list[tuple[Any, bool]] = [(options, True)]
    while stack:
        value, inspect_text = stack.pop()
        if isinstance(value, str):
            if inspect_text and _contains_credential_text(value):
                raise ValueError(f"{label} secrets must be supplied through {reference}")
        elif isinstance(value, dict):
            for key, nested in value.items():
                if _is_secret_option_key(key):
                    raise ValueError(f"{label} secrets must be supplied through {reference}")
                # Transport fields have dedicated URL/endpoint validators that
                # reject userinfo and return a non-reflective semantic error.
                # Let them preserve that safer response path; every other
                # scalar is inspected here before model construction.
                inspect_nested_text = _normalized_option_key(key) not in _TRANSPORT_OPTION_NAMES
                stack.append((nested, inspect_nested_text))
        elif isinstance(value, list):
            stack.extend((nested, True) for nested in value)
    return options

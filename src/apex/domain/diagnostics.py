"""Small, safe diagnostic strings for checkpoints, events, and provider errors."""

import re
from collections.abc import Mapping
from typing import Any

MAX_DIAGNOSTIC_CHARS = 4_096
_REDACTED = "[REDACTED]"
_CREDENTIAL_KEY = r"""
    (?:
        password|passwd|pwd|passphrase|secret|client[_-]?secret|
        personal[_-]?access[_-]?token|pat|bearer|jwt|psk|
        (?:access|refresh|identity|id|session|security|api)?[_-]?token|
        api[_-]?key|access[_-]?key|
        (?:private|ssh|signing|encryption|shared|account|storage|subscription|session)[_-]?key|
        session[_-]?id|client[_-]?(?:certificate|cert)|private[_-]?pem|pfx|pkcs12|keystore|
        (?:set[_-]?)?cookie|
        (?:connection|database|db|postgres(?:ql)?|redis|broker|amqp|mongo(?:db)?)[_-]?(?:string|uri|url)|
        dsn|authorization|auth|credential|signature|sig|sas|
        x-amz-(?:credential|signature|security-token)|x-goog-signature
    )
"""
_AUTH_SCHEME_RE = re.compile(r"(?i)\b(?P<scheme>bearer|basic|digest)\s+(?P<credential>[^\s,;]+)")
_URL_USERINFO_RE = re.compile(r"(?i)(?P<scheme>\b[a-z][a-z0-9+.-]*://)(?P<userinfo>[^/@\s?#]+@)")
_FIELD_ASSIGNMENT_RE = re.compile(
    r"""
    (?=(?P<assignment>
        (?P<prefix>
            (?<![A-Za-z0-9_-])
            ["']?(?P<field>[A-Za-z][A-Za-z0-9_.-]{0,127})["']?
            \s*[:=]\s*
        )
        (?P<value>
            "(?:\\.|[^"\\])*" |
            '(?:\\.|[^'\\])*' |
            [^\r\n,;}&]+
        )
    ))
    """,
    flags=re.VERBOSE,
)
_CREDENTIAL_FIELD_RE = re.compile(
    rf"(?:^|[^A-Za-z0-9])(?:{_CREDENTIAL_KEY})(?:$|[^A-Za-z0-9])",
    flags=re.IGNORECASE | re.VERBOSE,
)
_CREDENTIAL_FIELD_SUFFIXES = (
    "password",
    "passwd",
    "passphrase",
    "clientsecret",
    "personalaccesstoken",
    "pat",
    "bearer",
    "jwt",
    "psk",
    "apikey",
    "accesskey",
    "privatekey",
    "sshkey",
    "signingkey",
    "encryptionkey",
    "sharedkey",
    "accountkey",
    "storagekey",
    "subscriptionkey",
    "sessionkey",
    "accesstoken",
    "refreshtoken",
    "identitytoken",
    "idtoken",
    "sessiontoken",
    "securitytoken",
    "authtoken",
    "sastoken",
    "authheader",
    "authorizationheader",
    "basicauth",
    "httpauth",
    "sessionid",
    "clientcertificate",
    "clientcert",
    "privatepem",
    "pfx",
    "pkcs12",
    "keystore",
    "authorization",
    "credential",
    "credentials",
    "signature",
    "connectionstring",
    "databaseuri",
    "databaseurl",
    "postgresuri",
    "postgresurl",
    "postgresqluri",
    "postgresqlurl",
    "redisuri",
    "redisurl",
    "brokeruri",
    "brokerurl",
    "amqpuri",
    "amqpurl",
    "mongouri",
    "mongourl",
    "mongodburi",
    "mongodburl",
    "dsn",
    "cookie",
    "cookies",
    "cookiejar",
)
_NON_CREDENTIAL_FIELD_NAMES = frozenset(
    {
        "authmode",
        "authenticationmode",
        "authtype",
        "authenticationtype",
    }
)
_NON_CREDENTIAL_FIELD_SUFFIXES = ("accesskeyid", "accesskeyidentifier")


def is_credential_field(value: str) -> bool:
    """Whether a mapping/header field name denotes credential material."""

    collapsed = re.sub(r"[^a-z0-9]", "", value.casefold())
    if collapsed in _NON_CREDENTIAL_FIELD_NAMES or any(
        collapsed.endswith(suffix) for suffix in _NON_CREDENTIAL_FIELD_SUFFIXES
    ):
        return False
    if _CREDENTIAL_FIELD_RE.search(value):
        return True
    # Common JSON/SDK fields prepend a provider or account name in camelCase
    # (``stripeApiKey``, ``serviceAccountPrivateKey``). Word-boundary matching
    # alone cannot see those suffixes, while searching for a bare ``key`` or
    # ``token`` would overmatch harmless fields such as ``monkey`` and
    # ``tokenCount``. Collapse separators/case and match only credential-specific
    # terminal forms.
    return any(collapsed.endswith(suffix) for suffix in _CREDENTIAL_FIELD_SUFFIXES)


def _redact_credentials(rendered: str) -> str:
    rendered = _URL_USERINFO_RE.sub(
        lambda match: f"{match.group('scheme')}{_REDACTED}@",
        rendered,
    )
    rendered = _AUTH_SCHEME_RE.sub(
        lambda match: f"{match.group('scheme')} {_REDACTED}",
        rendered,
    )

    def redact_assignment(match: re.Match[str]) -> str:
        value = match.group("value")
        if len(value) >= 2 and value[0] in {'"', "'"} and value[-1] == value[0]:
            replacement = f"{value[0]}{_REDACTED}{value[0]}"
        else:
            replacement = _REDACTED
        return f"{match.group('prefix')}{replacement}"

    # A zero-width lookahead finds nested assignments too. For example, the
    # outer JSON field in ``{"detail":"stripeApiKey=..."}`` must not consume
    # the inner credential assignment before it can be inspected. Prefer an
    # outer credential assignment when credential spans overlap; redacting its
    # complete value already covers every nested match.
    replacements: list[tuple[int, int, str]] = []
    for match in _FIELD_ASSIGNMENT_RE.finditer(rendered):
        if not is_credential_field(match.group("field")):
            continue
        start, end = match.span("assignment")
        if any(
            start >= outer_start and end <= outer_end for outer_start, outer_end, _ in replacements
        ):
            continue
        replacements.append((start, end, redact_assignment(match)))
    for start, end, replacement in reversed(replacements):
        rendered = f"{rendered[:start]}{replacement}{rendered[end:]}"
    return rendered


def bounded_diagnostic(value: Any, *, max_chars: int = MAX_DIAGNOSTIC_CHARS) -> str:
    """Render a bounded, NUL-safe, credential-redacted diagnostic."""

    if max_chars < 1:
        raise ValueError("max_chars must be positive")
    try:
        rendered = str(value)
    except Exception:  # noqa: BLE001 - diagnostics must not mask the original failure
        rendered = f"<{type(value).__name__} diagnostic unavailable>"
    # Redaction needs only a bounded prefix: anything beyond this cannot survive
    # the final response/checkpoint cap. The extra room lets a credential value
    # crossing the final boundary be recognized and removed before truncation.
    rendered = rendered[: max(max_chars * 4, 16_384)].replace("\x00", "\\0")
    return _redact_credentials(rendered)[:max_chars]


def contains_credential_material(value: Any, *, max_nodes: int = 100_000) -> bool:
    """Detect credential-shaped material before a value crosses a durable boundary.

    This deliberately shares the exact field-name and rendered-string rules used
    by diagnostics redaction. The traversal is iterative, cycle-safe, and fails
    closed if a caller somehow supplies more nodes than the generous JSON budget.
    """

    if max_nodes < 1:
        raise ValueError("max_nodes must be positive")
    stack = [value]
    seen: set[int] = set()
    nodes = 0
    while stack:
        current = stack.pop()
        nodes += 1
        if nodes > max_nodes:
            return True
        if isinstance(current, Mapping):
            object_id = id(current)
            if object_id in seen:
                continue
            seen.add(object_id)
            for key, nested in current.items():
                try:
                    rendered_key = str(key)
                except Exception:  # noqa: BLE001 - an uninspectable key must fail closed
                    return True
                if (
                    is_credential_field(rendered_key)
                    or bounded_diagnostic(
                        rendered_key,
                        max_chars=max(1, len(rendered_key)),
                    )
                    != rendered_key
                ):
                    return True
                stack.append(nested)
        elif isinstance(current, (list, tuple)):
            object_id = id(current)
            if object_id in seen:
                continue
            seen.add(object_id)
            stack.extend(current)
        elif (
            isinstance(current, str)
            and bounded_diagnostic(
                current,
                max_chars=max(1, len(current)),
            )
            != current
        ):
            return True
    return False

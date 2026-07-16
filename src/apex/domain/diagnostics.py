"""Small, safe diagnostic strings for checkpoints, events, and provider errors."""

import dataclasses
import re
from collections.abc import Iterator, Mapping
from dataclasses import Field as DataclassField
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from enum import Enum
from types import GetSetDescriptorType, MemberDescriptorType
from typing import Any
from uuid import UUID

from pydantic import BaseModel
from pydantic.fields import FieldInfo

MAX_DIAGNOSTIC_CHARS = 4_096
MAX_CREDENTIAL_SCAN_NODES = 100_000
MAX_CREDENTIAL_SCAN_KEY_CHARS = 1_024
MAX_CREDENTIAL_SCAN_STRING_CHARS = 250_000
MAX_CREDENTIAL_SCAN_TOTAL_CHARS = 1_000_000
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
_COMPACT_JWT_RE = re.compile(
    r"(?<![A-Za-z0-9_-])eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}"
    r"(?![A-Za-z0-9_-])"
)
_PRIVATE_KEY_BLOCK_RE = re.compile(
    r"-----BEGIN (?P<kind>(?:(?:RSA|EC|DSA|OPENSSH|ENCRYPTED) )?PRIVATE KEY)-----"
    r".*?(?:-----END (?P=kind)-----|\Z)",
    flags=re.DOTALL,
)
_PROVIDER_TOKEN_RE = re.compile(
    r"""
    (?<![A-Za-z0-9_-])
    (?:
        gh[pousr]_[A-Za-z0-9]{20,} |
        github_pat_[A-Za-z0-9_]{20,} |
        glpat-[A-Za-z0-9_-]{20,} |
        xox[baprs]-[A-Za-z0-9_-]{20,} |
        xapp-[A-Za-z0-9_-]{20,} |
        [sr]k_(?:live|test)_[A-Za-z0-9]{16,} |
        sk-(?:proj-)?[A-Za-z0-9_-]{20,} |
        npm_[A-Za-z0-9]{20,} |
        pypi-[A-Za-z0-9_-]{20,} |
        hf_[A-Za-z0-9]{20,} |
        AIza[A-Za-z0-9_-]{35} |
        ya29\.[A-Za-z0-9._-]{20,} |
        SG\.[A-Za-z0-9_-]{22}\.[A-Za-z0-9_-]{43} |
        dckr_(?:pat|oat)_[A-Za-z0-9_-]{20,} |
        (?i:[a-z0-9]{14}\.atlasv1\.[a-z0-9_=\-]{60,70}) |
        [A-Za-z0-9]{76}AZDO[A-Za-z0-9]{4}
    )
    (?![A-Za-z0-9_-])
    """,
    flags=re.VERBOSE,
)
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
    "pwd",
    "passphrase",
    "secret",
    "secretkey",
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
    "token",
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
    "authentication",
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
_CREDENTIAL_VALUE_WRAPPERS = ("value", "string", "binary", "text", "data", "hash")
_DATACLASS_FIELD_MARKER = getattr(dataclasses, "_FIELD", None)
_PYDANTIC_DECORATORS_TYPE = type(BaseModel.__pydantic_decorators__)


def is_credential_field(value: str) -> bool:
    """Whether a mapping/header field name denotes credential material."""

    if type(value) is not str:
        return True
    # Field names crossing durable JSON boundaries are much smaller than this.
    # Treat a pathological name as unsafe before regex normalization can perform
    # attacker-sized work.
    if len(value) > MAX_CREDENTIAL_SCAN_KEY_CHARS:
        return True
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
    if any(collapsed.endswith(suffix) for suffix in _CREDENTIAL_FIELD_SUFFIXES):
        return True
    # Secret-manager and provider SDK payloads commonly wrap a credential field
    # in a camelCase ``*Value`` property (``secretValue``, ``apiKeyValue``,
    # ``tokenValue``).  Strip only that exact terminal wrapper, then reapply the
    # credential-specific suffix list.  Preserve the explicit non-credential
    # contracts so fields such as ``authenticationModeValue`` and
    # ``tokenCountValue`` remain safe.
    candidate = collapsed
    for _ in range(3):
        wrapper = next(
            (suffix for suffix in _CREDENTIAL_VALUE_WRAPPERS if candidate.endswith(suffix)),
            None,
        )
        if wrapper is None:
            break
        unwrapped = candidate[: -len(wrapper)]
        if unwrapped in _NON_CREDENTIAL_FIELD_NAMES or any(
            unwrapped.endswith(suffix) for suffix in _NON_CREDENTIAL_FIELD_SUFFIXES
        ):
            return False
        if any(unwrapped.endswith(suffix) for suffix in _CREDENTIAL_FIELD_SUFFIXES):
            return True
        candidate = unwrapped
    return False


def _redact_credentials(rendered: str) -> str:
    # Standalone credential values can cross provider and persistence boundaries
    # under otherwise innocuous field names (for example ``{"value": "ghp_..."}``).
    # Match only high-confidence, structured signatures with conservative length
    # floors so ordinary identifiers and prose remain visible in diagnostics.
    rendered = _PRIVATE_KEY_BLOCK_RE.sub(_REDACTED, rendered)
    rendered = _COMPACT_JWT_RE.sub(_REDACTED, rendered)
    rendered = _PROVIDER_TOKEN_RE.sub(_REDACTED, rendered)
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
    # Redaction needs only a bounded prefix: anything beyond this cannot survive
    # the final response/checkpoint cap. The extra room lets a credential value
    # crossing the final boundary be recognized and removed before truncation.
    window = max(max_chars * 4, 16_384)
    rendered = _safe_diagnostic_text(value, window)
    rendered = rendered[:window].replace("\x00", "\\0")
    return _redact_credentials(rendered)[:max_chars]


def _safe_diagnostic_text(value: Any, limit: int) -> str:
    """Render only exact built-ins; never execute an arbitrary ``__str__`` hook."""

    if type(value) is str:
        return value[:limit]
    if value is None:
        return "None"
    if type(value) is bool:
        return "True" if value else "False"
    if type(value) is int:
        if value.bit_length() > max(256, limit * 4):
            return "<integer diagnostic unavailable>"
        return str(value)
    if type(value) is float:
        return str(value)
    if type(value) is bytes:
        return value[:limit].decode("utf-8", errors="replace")
    if type(value) is bytearray:
        return bytes(value[:limit]).decode("utf-8", errors="replace")
    if type(value) in {date, datetime, time, timedelta, UUID}:
        return str(value)
    if isinstance(value, Enum):
        try:
            raw = object.__getattribute__(value, "_value_")
        except Exception:
            raw = None
        if type(raw) is str:
            return raw[:limit]
    if isinstance(value, BaseException):
        name = _safe_type_name(value)
        try:
            args = object.__getattribute__(value, "args")
        except Exception:
            args = ()
        if type(args) is tuple:
            remaining = max(0, limit - len(name) - 2)
            parts: list[str] = []
            for item in args[:4]:
                if remaining <= 0:
                    break
                rendered = _safe_diagnostic_argument(item, remaining)
                if rendered is None:
                    continue
                parts.append(rendered)
                remaining -= len(rendered) + 2
            if parts:
                return f"{name}: {', '.join(parts)}"
        return f"<{name} diagnostic unavailable>"
    return f"<{_safe_type_name(value)} diagnostic unavailable>"


def _safe_diagnostic_argument(value: Any, limit: int) -> str | None:
    if type(value) is str:
        return value[:limit]
    if value is None or type(value) in {bool, float}:
        return _safe_diagnostic_text(value, limit)
    if type(value) is int and value.bit_length() <= max(256, limit * 4):
        return str(value)[:limit]
    if type(value) is bytes:
        return value[:limit].decode("utf-8", errors="replace")
    return None


def _safe_type_name(value: Any) -> str:
    name = _safe_type_metadata(type(value), "__name__")
    return name[:64] if type(name) is str and name else "unknown"


def safe_type_name(value: Any) -> str:
    """Return bounded type metadata without invoking metaclass descriptors."""

    return _safe_type_name(value)


def _safe_type_metadata(cls: type[Any], name: str, default: Any = None) -> Any:
    """Read type metadata only when no custom metaclass shadows that field."""

    metaclass = type(cls)
    # A higher-order custom metaclass cannot be inspected without recursively
    # trusting another descriptor layer. Such values are opaque at this boundary.
    if type(metaclass) is not type:
        return default
    try:
        metaclass_mro = type.__getattribute__(metaclass, "__mro__")
    except Exception:
        return default
    if type(metaclass_mro) is not tuple:
        return default
    for metaclass_base in metaclass_mro:
        try:
            namespace = type.__getattribute__(metaclass_base, "__dict__")
        except Exception:
            return default
        if metaclass_base is not type and name in namespace:
            return default
    try:
        return type.__getattribute__(cls, name)
    except Exception:
        return default


def _safe_class_attribute(cls: type[Any], name: str, default: Any = None) -> Any:
    """Resolve raw class metadata without invoking metaclass descriptors/hooks."""

    mro = _safe_type_metadata(cls, "__mro__")
    if type(mro) is not tuple:
        return default
    for base in mro:
        namespace = _safe_type_metadata(base, "__dict__")
        if namespace is None:
            return default
        if name in namespace:
            return namespace[name]
    return default


def _pydantic_extras(model: BaseModel) -> Any:
    """Read BaseModel's own slot, bypassing a malicious subclass descriptor."""

    failed = False
    value: Any = None
    try:
        namespace = type.__getattribute__(BaseModel, "__dict__")
        descriptor = namespace["__pydantic_extra__"]
        if type(descriptor) is not MemberDescriptorType:
            raise TypeError("unexpected pydantic extras descriptor")
        value = descriptor.__get__(model, type(model))
    except Exception:
        failed = True
    if failed:
        raise TypeError("pydantic extras are unavailable")
    return value


def _raw_descriptor_value(owner: Any, declaring_class: type[Any], name: str) -> Any:
    """Read one C-level instance slot without executing a Python descriptor."""

    failed = False
    value: Any = None
    try:
        namespace = _safe_type_metadata(declaring_class, "__dict__")
        if namespace is None:
            raise TypeError("instance descriptor metadata is unavailable")
        descriptor = namespace[name]
        if type(descriptor) not in {MemberDescriptorType, GetSetDescriptorType}:
            raise TypeError("instance descriptor is unsafe")
        value = descriptor.__get__(owner, type(owner))
    except Exception:
        failed = True
    if failed:
        raise TypeError("instance field is unavailable")
    return value


def _raw_instance_dict(owner: Any) -> dict[Any, Any]:
    """Read an ordinary instance dictionary through its raw C descriptor."""

    mro = _safe_type_metadata(type(owner), "__mro__")
    if type(mro) is not tuple:
        raise TypeError("instance metadata is invalid")
    for base in mro:
        namespace = _safe_type_metadata(base, "__dict__")
        if namespace is None:
            raise TypeError("instance metadata is unavailable")
        if "__dict__" not in namespace:
            continue
        state = _raw_descriptor_value(owner, base, "__dict__")
        if type(state) is not dict:
            raise TypeError("instance state is invalid")
        return state
    raise TypeError("instance has no dictionary")


def _raw_field_info_value(field: FieldInfo, name: str) -> Any:
    if type(field) is not FieldInfo:
        raise TypeError("pydantic field metadata is invalid")
    return _raw_descriptor_value(field, FieldInfo, name)


def _raw_pydantic_decorator_map(decorators: Any, name: str) -> dict[Any, Any]:
    if type(decorators) is not _PYDANTIC_DECORATORS_TYPE:
        raise TypeError("pydantic decorator metadata is invalid")
    value = _raw_descriptor_value(decorators, _PYDANTIC_DECORATORS_TYPE, name)
    if type(value) is not dict:
        raise TypeError("pydantic decorator metadata is invalid")
    return value


def _field_is_descriptor_shadowed(cls: type[Any], name: str) -> bool:
    """Reject model fields shadowed by subclass properties/data descriptors."""

    try:
        mro = type.__getattribute__(cls, "__mro__")
    except Exception:
        return True
    for base in mro:
        if base is BaseModel:
            break
        try:
            namespace = type.__getattribute__(base, "__dict__")
        except Exception:
            return True
        if name in namespace:
            return True
    return False


def _raw_dataclass_field(owner: Any, name: Any) -> Any:
    if type(name) is not str:
        raise TypeError("dataclass field name is invalid")
    if _safe_class_attribute(type(owner), "__getattribute__") is not object.__getattribute__:
        raise TypeError("dataclass field access is unsafe")
    try:
        state = _raw_instance_dict(owner)
    except TypeError:
        state = None
    if state is not None and name in state:
        return state[name]
    descriptor = _safe_class_attribute(type(owner), name)
    if type(descriptor) is not MemberDescriptorType:
        raise TypeError("dataclass field descriptor is unsafe")
    return descriptor.__get__(owner, type(owner))


def _raw_slot_field(owner: Any, name: Any) -> Any:
    if type(name) is not str or not name:
        raise TypeError("slot name is invalid")
    if _safe_class_attribute(type(owner), "__getattribute__") is not object.__getattribute__:
        raise TypeError("slot access is unsafe")
    descriptor = _safe_class_attribute(type(owner), name)
    if type(descriptor) is not MemberDescriptorType:
        raise TypeError("slot descriptor is unsafe")
    return descriptor.__get__(owner, type(owner))


def contains_credential_material(
    value: Any,
    *,
    max_nodes: int = MAX_CREDENTIAL_SCAN_NODES,
    max_key_chars: int = MAX_CREDENTIAL_SCAN_KEY_CHARS,
    max_string_chars: int = MAX_CREDENTIAL_SCAN_STRING_CHARS,
    max_total_chars: int = MAX_CREDENTIAL_SCAN_TOTAL_CHARS,
) -> bool:
    """Detect credential-shaped material before a value crosses a durable boundary.

    This deliberately shares the exact field-name and rendered-string rules used
    by diagnostics redaction. Traversal is iterative, lazy, cycle-safe, and fails
    closed before an oversized child collection or scalar can be copied/scanned.
    """

    if min(max_nodes, max_key_chars, max_string_chars, max_total_chars) < 1:
        raise ValueError("credential scan limits must be positive")

    # Frames retain iterators, not materialized child lists. A container with a
    # million members therefore costs O(depth) scanner memory and is rejected as
    # soon as the next member would exceed ``max_nodes``.
    value_frame = "value"
    mapping_frame = "mapping"
    sequence_frame = "sequence"
    pydantic_frame = "pydantic"
    dataclass_frame = "dataclass"
    slots_frame = "slots"
    stack: list[tuple[str, Any]] = [(value_frame, value)]
    seen: set[int] = set()
    nodes = 0
    total_chars = 0

    def account_text(text: str, *, key: bool = False) -> bool:
        """Return False once a scalar/key would exceed a fixed scan budget."""

        nonlocal total_chars
        limit = max_key_chars if key else max_string_chars
        if len(text) > limit or total_chars > max_total_chars - len(text):
            return False
        total_chars += len(text)
        return True

    def safe_key(raw_key: Any) -> bool:
        # Durable JSON objects require string keys. Calling ``str`` on a hostile
        # provider-owned object can itself execute or allocate without bound.
        if type(raw_key) is not str:
            return False
        rendered_key = raw_key
        if not account_text(rendered_key, key=True):
            return False
        return not (
            is_credential_field(rendered_key)
            or bounded_diagnostic(
                rendered_key,
                max_chars=max(1, min(len(rendered_key), max_key_chars)),
            )
            != rendered_key
        )

    def pydantic_items(model: BaseModel) -> Iterator[tuple[Any, Any, Any]]:
        """Yield declared/extras fields without invoking ``model_dump`` serializers."""

        model_fields = _safe_class_attribute(type(model), "__pydantic_fields__")
        if type(model_fields) is not dict:
            raise TypeError("pydantic model fields are unavailable")
        state = _raw_instance_dict(model)
        for name, field_info in model_fields.items():
            if (
                type(name) is not str
                or name not in state
                or _field_is_descriptor_shadowed(type(model), name)
            ):
                raise TypeError("pydantic model field is unavailable")
            yield name, field_info, state[name]
        extras = _pydantic_extras(model)
        if extras is None:
            return
        if type(extras) is not dict:
            raise TypeError("pydantic model extras are invalid")
        for name, nested in extras.items():
            yield name, None, nested

    def dataclass_items(fields: dict[Any, Any]) -> Iterator[DataclassField[Any]]:
        """Yield only real instance fields and reject corrupted class metadata."""

        for field in fields.values():
            if type(field) is not DataclassField:
                raise TypeError("dataclass field metadata is invalid")
            field_type = object.__getattribute__(field, "_field_type")
            if field_type is _DATACLASS_FIELD_MARKER:
                yield field

    while stack:
        frame_kind, current = stack.pop()
        if frame_kind != value_frame:
            try:
                child = next(current)
            except StopIteration:
                continue
            except Exception:  # noqa: BLE001 - hostile iteration must fail closed
                return True
            # Resume this iterator only after inspecting its one yielded child.
            # If the node budget is already exhausted, the existence of this child
            # proves the value is over-limit; do not retain or traverse it.
            if nodes >= max_nodes:
                return True
            stack.append((frame_kind, current))
            if frame_kind == mapping_frame:
                try:
                    raw_key, nested = child
                except Exception:  # noqa: BLE001 - malformed mapping item fails closed
                    return True
                if not safe_key(raw_key):
                    return True
                stack.append((value_frame, nested))
            elif frame_kind == sequence_frame:
                stack.append((value_frame, child))
            elif frame_kind == pydantic_frame:
                try:
                    raw_name, field_info, nested = child
                except Exception:  # noqa: BLE001 - malformed model field fails closed
                    return True
                names = [raw_name]
                if field_info is not None:
                    if type(field_info) is not FieldInfo:
                        return True
                    for attribute in ("alias", "serialization_alias"):
                        try:
                            alias = _raw_field_info_value(field_info, attribute)
                        except Exception:  # noqa: BLE001 - hostile field metadata fails closed
                            return True
                        if type(alias) is str and alias not in names:
                            names.append(alias)
                        elif alias is not None and type(alias) is not str:
                            return True
                if any(not safe_key(name) for name in names):
                    return True
                stack.append((value_frame, nested))
            elif frame_kind == slots_frame:
                try:
                    owner, raw_name = child
                except Exception:  # noqa: BLE001 - malformed slot metadata fails closed
                    return True
                if not safe_key(raw_name):
                    return True
                try:
                    nested = _raw_slot_field(owner, raw_name)
                except Exception:  # noqa: BLE001 - unreadable slots fail closed
                    return True
                stack.append((value_frame, nested))
            elif frame_kind == dataclass_frame:
                try:
                    owner, field = child
                except Exception:  # noqa: BLE001 - malformed field metadata fails closed
                    return True
                if not safe_key(field.name):
                    return True
                try:
                    nested = _raw_dataclass_field(owner, field.name)
                except Exception:  # noqa: BLE001 - malformed dataclass fails closed
                    return True
                stack.append((value_frame, nested))
            else:  # pragma: no cover - scanner frames are module-owned
                return True
            continue

        nodes += 1
        if nodes > max_nodes:
            return True

        if type(current) is str:
            if not account_text(current):
                return True
            if (
                bounded_diagnostic(
                    current,
                    max_chars=max(1, min(len(current), max_string_chars)),
                )
                != current
            ):
                return True
        elif current is None or type(current) in {
            bool,
            int,
            float,
            Decimal,
            date,
            datetime,
            time,
            timedelta,
        }:
            # Scalar numeric/temporal values cannot contain credential text and
            # have no attacker-controlled child cardinality to inspect.
            continue
        elif type(current) is UUID:
            # LangGraph's native controls use UUID objects. They are immutable,
            # fixed-size scalar identifiers; walking CPython's implementation
            # slots exposes enum/runtime internals rather than durable content.
            continue
        elif isinstance(current, Enum):
            object_id = id(current)
            if object_id in seen:
                continue
            seen.add(object_id)
            try:
                stack.append((value_frame, object.__getattribute__(current, "_value_")))
            except Exception:  # noqa: BLE001 - malformed enum fails closed
                return True
        elif isinstance(
            current,
            (str, bool, int, float, Decimal, date, datetime, time, timedelta, UUID),
        ):
            # Subclasses can override scalar operations such as ``__len__``,
            # ``bit_length`` and string conversion. They are not durable JSON
            # primitives at this generic boundary.
            return True
        elif type(current) is dict:
            object_id = id(current)
            if object_id in seen:
                continue
            seen.add(object_id)
            try:
                iterator = iter(current.items())
            except Exception:  # noqa: BLE001 - hostile mapping iteration fails closed
                return True
            stack.append((mapping_frame, iterator))
        elif type(current) in {list, tuple}:
            object_id = id(current)
            if object_id in seen:
                continue
            seen.add(object_id)
            try:
                iterator = iter(current)
            except Exception:  # noqa: BLE001 - hostile sequence iteration fails closed
                return True
            stack.append((sequence_frame, iterator))
        elif isinstance(current, (Mapping, list, tuple)):
            # Never enter arbitrary container iterators: a bounded item count
            # cannot bound one hostile ``next()`` call that never returns.
            return True
        elif isinstance(current, BaseModel):
            object_id = id(current)
            if object_id in seen:
                continue
            seen.add(object_id)
            # Computed/custom serializers can execute arbitrary allocation while
            # producing ``model_dump`` output. They cannot be inspected safely at
            # this generic boundary, so fail closed instead of invoking them.
            try:
                if (
                    _safe_class_attribute(type(current), "__getattribute__")
                    is not object.__getattribute__
                ):
                    return True
                decorators = _safe_class_attribute(type(current), "__pydantic_decorators__")
                computed = _safe_class_attribute(type(current), "__pydantic_computed_fields__")
                model_serializers = _raw_pydantic_decorator_map(decorators, "model_serializers")
                field_serializers = _raw_pydantic_decorator_map(decorators, "field_serializers")
                if model_serializers or field_serializers or type(computed) is not dict or computed:
                    return True
                iterator = pydantic_items(current)
            except Exception:  # noqa: BLE001 - malformed model metadata fails closed
                return True
            stack.append((pydantic_frame, iterator))
        elif (
            not isinstance(current, type)
            and type(
                dataclass_fields := _safe_class_attribute(type(current), "__dataclass_fields__")
            )
            is dict
        ):
            object_id = id(current)
            if object_id in seen:
                continue
            seen.add(object_id)
            field_values = dataclass_items(dataclass_fields)
            # ``fields`` returns class-owned metadata; fetch one value at a time.
            stack.append(
                (
                    dataclass_frame,
                    map(lambda field, owner=current: (owner, field), field_values),
                )
            )
        else:
            if _safe_class_attribute(type(current), "model_dump") is not None:
                # Unknown model-like objects may materialize an arbitrary payload in
                # ``model_dump``. Only Pydantic models receive lazy field inspection.
                return True
            object_slots = _safe_class_attribute(type(current), "__slots__", ())
            if type(object_slots) is str:
                object_slots = (object_slots,)
            elif type(object_slots) is not tuple:
                return True
            if len(object_slots) > 0:
                if len(object_slots) > max_nodes - nodes or any(
                    type(name) is not str for name in object_slots
                ):
                    return True
                object_id = id(current)
                if object_id in seen:
                    continue
                seen.add(object_id)
                stack.append(
                    (
                        slots_frame,
                        map(lambda name, owner=current: (owner, name), object_slots),
                    )
                )
                continue
            if (
                _safe_class_attribute(type(current), "__getattribute__")
                is not object.__getattribute__
            ):
                return True
            try:
                object_state = _raw_instance_dict(current)
            except Exception:  # noqa: BLE001 - hostile object state fails closed
                return True
            object_id = id(current)
            if object_id in seen:
                continue
            seen.add(object_id)
            stack.append((value_frame, object_state))
            continue
    return False

"""Artifact store port. Bytes never enter graph state — only refs/URIs do."""

import re
from collections.abc import AsyncIterable, AsyncIterator
from hashlib import sha256
from typing import Protocol, runtime_checkable
from urllib.parse import quote, unquote, urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator

from apex.domain.diagnostics import contains_credential_material


class StoredArtifact(BaseModel):
    """Bounded, capability-free acknowledgement returned by an artifact store."""

    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    key: str = Field(min_length=1, max_length=1_024)
    uri: str = Field(min_length=1, max_length=4_096)
    size: int = Field(ge=0, strict=True)

    @field_validator("key")
    @classmethod
    def validate_key(cls, value: str) -> str:
        if any(ord(char) < 0x20 or ord(char) == 0x7F for char in value):
            raise ValueError("artifact key contains control characters")
        return value

    @field_validator("uri")
    @classmethod
    def validate_uri(cls, value: str) -> str:
        if value != value.strip() or any(ord(char) < 0x20 or ord(char) == 0x7F for char in value):
            raise ValueError("artifact URI contains unsafe characters")
        parsed = urlsplit(value)
        if (
            parsed.scheme not in {"apex-artifact", "memory", "s3"}
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
        ):
            # StoredArtifact is durable/checkpoint-visible. Signed URLs and
            # embedded credentials are capabilities and must never enter it.
            raise ValueError("artifact URI must be capability-free")
        return value


def validate_stored_artifact_ack(
    value: object,
    expected_key: str,
    *,
    expected_size: int | None = None,
    max_size: int | None = None,
) -> StoredArtifact:
    """Validate provider metadata before it can authorize durable finalization."""

    stored: StoredArtifact | None = None
    invalid = False
    try:
        # ``isinstance`` may consult an arbitrary object's ``__class__``
        # descriptor. Provider acknowledgements are untrusted, so recognize
        # only the exact model without executing that hook.
        if type(value) is StoredArtifact:
            if value.__pydantic_extra__:
                raise ValueError("unexpected acknowledgement type")
            source: object = value.__dict__
        else:
            source = value
        if type(source) is not dict:
            raise TypeError("artifact acknowledgement must be an object")
        payload = _bounded_exact_mapping(source, {"key", "size", "uri"})
        key = payload["key"]
        uri = payload["uri"]
        size = payload["size"]
        if (
            type(key) is not str
            or not 1 <= len(key) <= 1_024
            or type(uri) is not str
            or not 1 <= len(uri) <= 4_096
            or type(size) is not int
            or size < 0
        ):
            raise ValueError("unbounded acknowledgement fields")
        stored = StoredArtifact(key=key, uri=uri, size=size)
        if stored.key != expected_key:
            raise ValueError("unexpected key")
        if expected_size is not None and stored.size != expected_size:
            raise ValueError("unexpected size")
        if max_size is not None and stored.size > max_size:
            raise ValueError("oversized acknowledgement")
        if not _artifact_uri_matches_key(stored.uri, expected_key):
            raise ValueError("URI does not identify the expected key")
    except Exception:
        # Provider values can be arbitrarily large or secret-bearing. Keep the
        # contract failure fixed and do not chain validation input into logs.
        invalid = True
    if invalid or stored is None:
        # Raise outside the handler. ``from None`` suppresses display but would
        # still leave provider/Pydantic input reachable through ``__context__``.
        raise RuntimeError("artifact store returned invalid object metadata")
    return stored.model_copy(deep=True)


def _bounded_exact_mapping(
    value: dict[object, object],
    expected_keys: set[str],
) -> dict[str, object]:
    """Read at most one key beyond a tiny exact provider schema."""

    if type(value) is not dict:
        raise ValueError("provider metadata must be a plain object")
    keys: list[str] = []
    iterator = iter(value)
    for _ in range(len(expected_keys) + 1):
        try:
            key = next(iterator)
        except StopIteration:
            break
        if type(key) is not str:
            raise ValueError("provider field names must be strings")
        keys.append(key)
    if len(keys) != len(expected_keys) or set(keys) != expected_keys:
        raise ValueError("provider fields do not match the expected schema")
    return {key: value[key] for key in keys}


def canonical_artifact_uri(key: str) -> str:
    """Return the only provider-independent URI safe for durable references."""

    return f"apex-artifact:///{quote(key, safe='/-._~')}"


def _artifact_uri_matches_key(uri: str, expected_key: str) -> bool:
    parsed = urlsplit(uri)
    if parsed.scheme == "memory":
        encoded_key = f"{parsed.netloc}{parsed.path}"
    elif parsed.scheme == "s3":
        if not parsed.netloc:
            return False
        encoded_key = parsed.path.removeprefix("/")
    elif parsed.scheme == "apex-artifact":
        if parsed.netloc:
            return False
        encoded_key = parsed.path.removeprefix("/")
    else:
        return False
    try:
        return unquote(encoded_key, errors="strict") == expected_key
    except UnicodeError:
        return False


class ArtifactStoreBusyError(RuntimeError):
    """Artifact bytes cannot start streaming without exceeding provider capacity."""


def engine_artifact_namespace(idempotency_key: str) -> str:
    """Opaque, globally stable namespace for one internal engine attempt."""

    if (
        type(idempotency_key) is not str
        or not 1 <= len(idempotency_key) <= 256
        or "\x00" in idempotency_key
        or contains_credential_material(idempotency_key)
    ):
        raise ValueError("idempotency key is unsafe for an engine artifact namespace")
    digest = sha256(idempotency_key.encode("utf-8")).hexdigest()
    return f"engine-runs/{digest}"


def engine_artifact_key(idempotency_key: str, name: str) -> str:
    """Return a traversal-safe key beneath the attempt's private namespace."""

    if (
        type(name) is not str
        or not 1 <= len(name) <= 512
        or "\x00" in name
        or contains_credential_material(name)
    ):
        raise ValueError("artifact name is unsafe for an engine artifact key")
    # Preserve uniqueness prefixes added by adapters even when a provider name
    # contains path separators. Flatten the path instead of taking only basename.
    filename = "_".join(
        part for part in name.replace("\\", "/").split("/") if part not in {"", ".", ".."}
    ).strip()
    if filename in {"", ".", ".."}:
        filename = "artifact.bin"
    filename = filename[:512]
    return f"{engine_artifact_namespace(idempotency_key)}/{filename}"


def transcript_artifact_key(thread_id: str, phase: str, attempt: int) -> str:
    """Traversal-safe, replay-stable key for one phase-attempt transcript."""

    if type(thread_id) is not str or type(phase) is not str:
        raise ValueError("transcript artifact key fields must be strings")
    if (
        not 1 <= len(thread_id) <= 255
        or not 1 <= len(phase) <= 64
        or thread_id != thread_id.strip()
        or phase != phase.strip()
        or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,254}", thread_id)
        or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", phase)
        or contains_credential_material((thread_id, phase))
    ):
        raise ValueError("transcript artifact key fields are unsafe")
    if type(attempt) is not int or not 1 <= attempt <= 1_000_000:
        raise ValueError("transcript artifact attempt must be between 1 and 1000000")
    return f"transcripts/{thread_id}/{phase}/attempt-{attempt}.txt"


@runtime_checkable
class ArtifactStorePort(Protocol):
    async def put(self, key: str, data: bytes, *, content_type: str) -> StoredArtifact: ...

    async def put_stream(
        self,
        key: str,
        data: AsyncIterable[bytes],
        *,
        content_type: str,
        max_bytes: int,
    ) -> StoredArtifact: ...

    async def get(self, key: str) -> bytes: ...

    def iter_bytes(self, key: str, *, chunk_size: int = 64 * 1024) -> AsyncIterator[bytes]: ...

    async def get_url(self, key: str, *, ttl_s: int = 3600) -> str: ...


class ArtifactDeletePort(Protocol):
    async def delete(self, key: str) -> None: ...

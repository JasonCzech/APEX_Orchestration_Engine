"""Bounded script bytes and artifact-store verification; never execute source."""

import hashlib

from apex.ports.artifact_store import ArtifactStorePort, validate_stored_artifact_ack

MAX_SCRIPT_BYTES = 150_000


def script_bytes(source: str) -> bytes:
    data = source.encode("utf-8")
    if not data or len(data) > MAX_SCRIPT_BYTES:
        raise ValueError("Script source must contain 1–150,000 UTF-8 bytes")
    return data


async def read_verified_script(store: ArtifactStorePort, key: str, checksum: str) -> bytes:
    content = bytearray()
    iterator = store.iter_bytes(key, chunk_size=64 * 1024)
    try:
        async for chunk in iterator:
            if not isinstance(chunk, bytes) or len(content) + len(chunk) > MAX_SCRIPT_BYTES:
                raise ValueError("Stored script exceeds the bounded byte contract")
            content.extend(chunk)
    finally:
        close = getattr(iterator, "aclose", None)
        if close:
            await close()
    if not content or hashlib.sha256(content).hexdigest() != checksum:
        raise ValueError("Stored script checksum does not match its approved source")
    return bytes(content)


async def store_verified_script(store: ArtifactStorePort, key: str, data: bytes) -> str:
    if not data or len(data) > MAX_SCRIPT_BYTES:
        raise ValueError("Script source must contain 1–150,000 bytes")
    checksum = hashlib.sha256(data).hexdigest()
    ack = await store.put(key, data, content_type="application/octet-stream")
    validate_stored_artifact_ack(ack, key, expected_size=len(data))
    await read_verified_script(store, key, checksum)
    return checksum

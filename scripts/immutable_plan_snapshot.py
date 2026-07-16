"""Create a bounded, read-only saved-plan snapshot for Terraform subprocesses."""

from __future__ import annotations

import fcntl
import hashlib
import os
import stat
import sys
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import BinaryIO

_F_ADD_SEALS = getattr(fcntl, "F_ADD_SEALS", 1033)
_F_GET_SEALS = getattr(fcntl, "F_GET_SEALS", 1034)
_F_SEAL_SEAL = getattr(fcntl, "F_SEAL_SEAL", 0x0001)
_F_SEAL_SHRINK = getattr(fcntl, "F_SEAL_SHRINK", 0x0002)
_F_SEAL_GROW = getattr(fcntl, "F_SEAL_GROW", 0x0004)
_F_SEAL_WRITE = getattr(fcntl, "F_SEAL_WRITE", 0x0008)
_REQUIRED_SEALS = _F_SEAL_SEAL | _F_SEAL_SHRINK | _F_SEAL_GROW | _F_SEAL_WRITE
_MFD_CLOEXEC = getattr(os, "MFD_CLOEXEC", 0x0001)
_MFD_ALLOW_SEALING = getattr(os, "MFD_ALLOW_SEALING", 0x0002)


class ImmutablePlanSnapshotError(RuntimeError):
    """A saved plan could not be copied into an immutable private snapshot."""


def inherited_descriptor_path(descriptor: int) -> str:
    for directory in (Path("/proc/self/fd"), Path("/dev/fd")):
        if directory.is_dir():
            return str(directory / str(descriptor))
    raise ImmutablePlanSnapshotError(
        "this platform cannot expose an inherited Terraform plan snapshot"
    )


def _copy_plan(source: BinaryIO, destination: BinaryIO, max_bytes: int, chunk_bytes: int) -> str:
    digest = hashlib.sha256()
    copied = 0
    while chunk := source.read(chunk_bytes):
        copied += len(chunk)
        if copied > max_bytes:
            raise ImmutablePlanSnapshotError("Terraform saved plan exceeds the size limit")
        destination.write(chunk)
        digest.update(chunk)
    if copied == 0:
        raise ImmutablePlanSnapshotError("Terraform saved plan is empty")
    return digest.hexdigest()


def _sealed_memfd(source: BinaryIO, max_bytes: int, chunk_bytes: int) -> tuple[int, str]:
    try:
        writable = os.memfd_create(
            "apex-terraform-plan",
            _MFD_CLOEXEC | _MFD_ALLOW_SEALING,
        )
    except OSError:
        raise ImmutablePlanSnapshotError(
            "unable to create a sealable Terraform plan snapshot"
        ) from None

    readonly = -1
    try:
        with os.fdopen(os.dup(writable), "w+b") as destination:
            digest = _copy_plan(source, destination, max_bytes, chunk_bytes)
            destination.flush()
            os.fsync(destination.fileno())
        os.fchmod(writable, 0o400)
        fcntl.fcntl(writable, _F_ADD_SEALS, _REQUIRED_SEALS)
        applied_seals = fcntl.fcntl(writable, _F_GET_SEALS)
        if applied_seals & _REQUIRED_SEALS != _REQUIRED_SEALS:
            raise ImmutablePlanSnapshotError("Terraform plan snapshot sealing was incomplete")
        os.lseek(writable, 0, os.SEEK_SET)
        readonly = os.open(
            inherited_descriptor_path(writable),
            os.O_RDONLY | getattr(os, "O_CLOEXEC", 0),
        )
        if fcntl.fcntl(readonly, fcntl.F_GETFL) & os.O_ACCMODE != os.O_RDONLY:
            raise ImmutablePlanSnapshotError("Terraform plan snapshot is not read-only")
        return readonly, digest
    except ImmutablePlanSnapshotError:
        if readonly >= 0:
            os.close(readonly)
        raise
    except OSError:
        if readonly >= 0:
            os.close(readonly)
        raise ImmutablePlanSnapshotError("unable to seal the Terraform plan snapshot") from None
    finally:
        os.close(writable)


def _unlinked_readonly_file(
    source: BinaryIO,
    max_bytes: int,
    chunk_bytes: int,
) -> tuple[int, str]:
    temporary: Path | None = None
    readonly = -1
    try:
        with tempfile.NamedTemporaryFile(mode="w+b", delete=False) as destination:
            temporary = Path(destination.name)
            os.fchmod(destination.fileno(), 0o600)
            digest = _copy_plan(source, destination, max_bytes, chunk_bytes)
            destination.flush()
            os.fsync(destination.fileno())
            os.fchmod(destination.fileno(), 0o400)
            written = os.fstat(destination.fileno())
        readonly = os.open(
            temporary,
            os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
        )
        reopened = os.fstat(readonly)
        if (written.st_dev, written.st_ino) != (reopened.st_dev, reopened.st_ino):
            raise ImmutablePlanSnapshotError("Terraform plan snapshot identity changed")
        if fcntl.fcntl(readonly, fcntl.F_GETFL) & os.O_ACCMODE != os.O_RDONLY:
            raise ImmutablePlanSnapshotError("Terraform plan snapshot is not read-only")
        temporary.unlink()
        temporary = None
        return readonly, digest
    except ImmutablePlanSnapshotError:
        if readonly >= 0:
            os.close(readonly)
        raise
    except OSError:
        if readonly >= 0:
            os.close(readonly)
        raise ImmutablePlanSnapshotError(
            "unable to create a read-only Terraform plan snapshot"
        ) from None
    finally:
        if temporary is not None:
            try:
                temporary.unlink()
            except OSError:
                pass


@contextmanager
def immutable_plan_stream(
    source: BinaryIO,
    *,
    max_bytes: int,
    chunk_bytes: int,
    require_kernel_seal: bool = False,
) -> Iterator[tuple[str, int, str]]:
    """Copy a stream and yield its read-only descriptor path, fd, and digest.

    Linux uses a sealed ``memfd`` so even another same-UID process that finds
    the descriptor cannot mutate the inode. Other local platforms use an
    unlinked 0400 file with no remaining writable handle. Shared-runner callers
    must set ``require_kernel_seal`` and fail closed if memfd seals are absent.
    """

    if max_bytes <= 0 or chunk_bytes <= 0:
        raise ValueError("snapshot bounds must be positive")
    readonly = -1
    try:
        can_seal = sys.platform.startswith("linux") and hasattr(os, "memfd_create")
        if can_seal:
            readonly, digest = _sealed_memfd(source, max_bytes, chunk_bytes)
        elif require_kernel_seal:
            raise ImmutablePlanSnapshotError(
                "kernel-sealed Terraform plan snapshots are unavailable"
            )
        else:
            readonly, digest = _unlinked_readonly_file(source, max_bytes, chunk_bytes)

        os.lseek(readonly, 0, os.SEEK_SET)
        yield inherited_descriptor_path(readonly), readonly, digest
    finally:
        if readonly >= 0:
            os.close(readonly)


@contextmanager
def immutable_plan_snapshot(
    plan_path: Path,
    *,
    max_bytes: int,
    chunk_bytes: int,
    require_kernel_seal: bool = False,
) -> Iterator[tuple[str, int, str]]:
    """Open a non-symlink plan path and snapshot its exact bounded bytes."""

    if max_bytes <= 0 or chunk_bytes <= 0:
        raise ValueError("snapshot bounds must be positive")
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        source_descriptor = os.open(plan_path, flags)
    except OSError:
        raise ImmutablePlanSnapshotError("Terraform saved plan is unavailable or unsafe") from None

    with os.fdopen(source_descriptor, "rb") as source:
        metadata = os.fstat(source.fileno())
        if not stat.S_ISREG(metadata.st_mode):
            raise ImmutablePlanSnapshotError("Terraform saved plan is not a regular file")
        if metadata.st_size <= 0:
            raise ImmutablePlanSnapshotError("Terraform saved plan is empty")
        if metadata.st_size > max_bytes:
            raise ImmutablePlanSnapshotError("Terraform saved plan exceeds the size limit")
        with immutable_plan_stream(
            source,
            max_bytes=max_bytes,
            chunk_bytes=chunk_bytes,
            require_kernel_seal=require_kernel_seal,
        ) as snapshot:
            yield snapshot


__all__ = [
    "ImmutablePlanSnapshotError",
    "immutable_plan_snapshot",
    "immutable_plan_stream",
    "inherited_descriptor_path",
]

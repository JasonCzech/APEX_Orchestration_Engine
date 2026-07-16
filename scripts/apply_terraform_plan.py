"""Decrypt, verify, and apply an approved Terraform plan without a plaintext path."""

from __future__ import annotations

import argparse
import hmac
import os
import re
import signal
import stat
import subprocess
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

try:
    from scripts.immutable_plan_snapshot import (
        ImmutablePlanSnapshotError,
        immutable_plan_stream,
    )
except ModuleNotFoundError:  # Direct execution puts scripts/ on sys.path.
    from immutable_plan_snapshot import ImmutablePlanSnapshotError, immutable_plan_stream

MAX_SAVED_PLAN_BYTES = 512 * 1024 * 1024
MAX_ENCRYPTED_PLAN_BYTES = MAX_SAVED_PLAN_BYTES + 1024 * 1024
_COPY_CHUNK_BYTES = 1024 * 1024
_SHA256 = re.compile(r"[0-9a-f]{64}")
_PASSPHRASE_ENV = "TFPLAN_PASSPHRASE"
_APPLY_TIMEOUT_SECONDS = 55 * 60
_DECRYPT_EXIT_TIMEOUT_SECONDS = 5
_PROCESS_TERMINATION_GRACE_SECONDS = 5
_PROCESS_KILL_WAIT_SECONDS = 1


class PlanApplyError(RuntimeError):
    """The approved plan could not be applied without weakening its identity."""


class _ApplySignalInterrupt(Exception):
    """Unwind a blocking Popen.wait after forwarding a termination signal."""


def _scrubbed_environment() -> dict[str, str]:
    environment = os.environ.copy()
    environment.pop(_PASSPHRASE_ENV, None)
    return environment


def _openssl_environment(passphrase: str) -> dict[str, str]:
    environment = _scrubbed_environment()
    environment[_PASSPHRASE_ENV] = passphrase
    return environment


def _remove(path: Path) -> None:
    try:
        path.unlink()
    except FileNotFoundError:
        pass
    except OSError:
        raise PlanApplyError("unable to remove a Terraform plan artifact") from None


@contextmanager
def _decrypted_plan_snapshot(
    encrypted_path: Path,
    *,
    passphrase: str,
    require_kernel_seal: bool,
) -> Iterator[tuple[str, int, str]]:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(encrypted_path, flags)
    except OSError:
        raise PlanApplyError("encrypted Terraform plan is unavailable or unsafe") from None

    decrypt: subprocess.Popen[bytes] | None = None
    try:
        with os.fdopen(descriptor, "rb") as encrypted:
            metadata = os.fstat(encrypted.fileno())
            if (
                not stat.S_ISREG(metadata.st_mode)
                or metadata.st_size <= 0
                or metadata.st_size > MAX_ENCRYPTED_PLAN_BYTES
            ):
                raise PlanApplyError("encrypted Terraform plan size or type is invalid")
            try:
                decrypt = subprocess.Popen(
                    [
                        "openssl",
                        "enc",
                        "-d",
                        "-aes-256-cbc",
                        "-pbkdf2",
                        "-iter",
                        "200000",
                        "-pass",
                        "env:TFPLAN_PASSPHRASE",
                    ],
                    stdin=encrypted,
                    stdout=subprocess.PIPE,
                    env=_openssl_environment(passphrase),
                )
            except OSError:
                raise PlanApplyError("unable to start Terraform plan decryption") from None
            _remove(encrypted_path)
            assert decrypt.stdout is not None
            try:
                with immutable_plan_stream(
                    decrypt.stdout,
                    max_bytes=MAX_SAVED_PLAN_BYTES,
                    chunk_bytes=_COPY_CHUNK_BYTES,
                    require_kernel_seal=require_kernel_seal,
                ) as snapshot:
                    decrypt.stdout.close()
                    try:
                        decrypt_returncode = decrypt.wait(timeout=_DECRYPT_EXIT_TIMEOUT_SECONDS)
                    except subprocess.TimeoutExpired:
                        decrypt.kill()
                        raise PlanApplyError(
                            "Terraform plan decryption exceeded the time limit"
                        ) from None
                    if decrypt_returncode != 0:
                        raise PlanApplyError("Terraform plan decryption failed")
                    yield snapshot
            finally:
                decrypt.stdout.close()
    except ImmutablePlanSnapshotError as exc:
        raise PlanApplyError(str(exc)) from None
    finally:
        if decrypt is not None and decrypt.poll() is None:
            decrypt.kill()
            try:
                decrypt.wait(timeout=_PROCESS_KILL_WAIT_SECONDS)
            except subprocess.TimeoutExpired:
                pass
        _remove(encrypted_path)


def _terminate_process_group(process: subprocess.Popen[bytes]) -> None:
    # Terraform providers are descendants of the CLI.  The CLI can exit after
    # TERM while a provider that ignored the signal remains in its process
    # group, so always probe the group with KILL after the bounded grace period
    # instead of treating the CLI's exit as proof that every descendant died.
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    except OSError:
        if process.poll() is None:
            process.terminate()
    try:
        process.wait(timeout=_PROCESS_TERMINATION_GRACE_SECONDS)
    except subprocess.TimeoutExpired:
        pass
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    except OSError:
        if process.poll() is None:
            process.kill()
    if process.poll() is None:
        try:
            process.kill()
        except OSError:
            pass
    try:
        process.wait(timeout=_PROCESS_KILL_WAIT_SECONDS)
    except (OSError, subprocess.TimeoutExpired):
        # A concurrent handler may already have reaped the process. A process
        # stuck in uninterruptible kernel sleep can also outlive SIGKILL; never
        # turn the explicit apply deadline into another unbounded wait.
        pass


def _apply_snapshot(snapshot_path: str, descriptor: int) -> None:
    watched = tuple(
        candidate
        for candidate in (
            getattr(signal, "SIGHUP", None),
            getattr(signal, "SIGINT", None),
            getattr(signal, "SIGTERM", None),
        )
        if candidate is not None
    )
    previous = {candidate: signal.getsignal(candidate) for candidate in watched}
    process: subprocess.Popen[bytes] | None = None
    interrupted_by: int | None = None
    termination_started = False

    def terminate() -> None:
        nonlocal termination_started
        if process is not None and not termination_started:
            termination_started = True
            _terminate_process_group(process)

    def interrupt(signum: int, _frame: object) -> None:
        nonlocal interrupted_by
        if interrupted_by is None:
            interrupted_by = signum
            for candidate in watched:
                signal.signal(candidate, signal.SIG_IGN)
            if process is not None:
                # Do not call Popen.wait from a Python signal handler: the
                # signal may have interrupted Popen.wait while its internal
                # waitpid lock was held. Forward TERM, then unwind so normal
                # control flow can perform the bounded wait/KILL sequence.
                try:
                    os.killpg(process.pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
                except OSError:
                    pass
                raise _ApplySignalInterrupt

    try:
        # Install handlers before spawning Terraform.  A signal in the narrow
        # spawn window is recorded and the new process group is terminated as
        # soon as Popen returns instead of escaping with the inherited plan fd.
        for candidate in watched:
            signal.signal(candidate, interrupt)
        try:
            process = subprocess.Popen(
                ["terraform", "apply", "-input=false", snapshot_path],
                pass_fds=(descriptor,),
                start_new_session=True,
                env=_scrubbed_environment(),
            )
        except OSError:
            raise PlanApplyError("unable to start Terraform apply") from None

        try:
            if interrupted_by is not None:
                terminate()
                returncode = process.returncode
            else:
                returncode = process.wait(timeout=_APPLY_TIMEOUT_SECONDS)
        except _ApplySignalInterrupt:
            terminate()
            returncode = process.returncode
        except subprocess.TimeoutExpired:
            terminate()
            raise PlanApplyError("Terraform apply exceeded the time limit") from None
        except OSError:
            terminate()
            raise PlanApplyError("Terraform apply could not complete safely") from None
    finally:
        # Also covers an unexpected KeyboardInterrupt or interpreter-level
        # exception that did not pass through the installed signal handlers.
        if process is not None and process.poll() is None:
            terminate()
        for candidate, handler in previous.items():
            signal.signal(candidate, handler)

    if interrupted_by is not None:
        raise PlanApplyError("Terraform apply was interrupted")
    if returncode != 0:
        raise PlanApplyError("Terraform apply failed")


def apply_encrypted_terraform_plan(
    expected_digest: str,
    encrypted_path: Path,
    *,
    passphrase: str,
    require_kernel_seal: bool = True,
) -> None:
    if _SHA256.fullmatch(expected_digest) is None:
        raise PlanApplyError("approved Terraform plan digest is invalid")
    if len(passphrase) < 32:
        raise PlanApplyError("plan passphrase is missing or too short")
    with _decrypted_plan_snapshot(
        encrypted_path,
        passphrase=passphrase,
        require_kernel_seal=require_kernel_seal,
    ) as (snapshot_path, descriptor, actual_digest):
        if not hmac.compare_digest(actual_digest, expected_digest):
            raise PlanApplyError("approved Terraform plan digest mismatch")
        _apply_snapshot(snapshot_path, descriptor)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("expected_sha256")
    parser.add_argument("encrypted_plan")
    args = parser.parse_args(argv)
    # Scrub before any Terraform process can inherit the decryption secret.
    passphrase = os.environ.pop(_PASSPHRASE_ENV, "")
    if len(passphrase) < 32:
        print(
            "Terraform plan apply failed: plan passphrase is missing or too short",
            file=sys.stderr,
        )
        return 2
    try:
        apply_encrypted_terraform_plan(
            args.expected_sha256,
            Path(args.encrypted_plan),
            passphrase=passphrase,
        )
    except PlanApplyError as exc:
        print(f"Terraform plan apply failed: {exc}", file=sys.stderr)
        return 3
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

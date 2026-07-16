"""Policy-check and encrypt one immutable Terraform saved-plan snapshot.

The plan and apply jobs run on persistent self-hosted runners.  Reopening the
workspace plan for ``terraform show``, hashing, and encryption would allow a
concurrent path replacement to make the approval artifact differ from the
policy-checked bytes.  This helper copies the plan into an anonymous bounded
file, removes the workspace path, and performs every operation through that
single descriptor.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import BinaryIO

try:
    from scripts.immutable_plan_snapshot import (
        ImmutablePlanSnapshotError,
        immutable_plan_snapshot,
    )
except ModuleNotFoundError:  # Direct execution puts scripts/ on sys.path.
    from immutable_plan_snapshot import ImmutablePlanSnapshotError, immutable_plan_snapshot

REPO_ROOT = Path(__file__).resolve().parent.parent
POLICY_SCRIPT = REPO_ROOT / "scripts" / "terraform_plan_policy.py"
MAX_SAVED_PLAN_BYTES = 512 * 1024 * 1024
_COPY_CHUNK_BYTES = 1024 * 1024
_COMMAND_TIMEOUT_SECONDS = 300
_PASSPHRASE_ENV = "TFPLAN_PASSPHRASE"


class PlanSealError(RuntimeError):
    """The saved plan could not be sealed without weakening its identity."""


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
        raise PlanSealError("unable to remove a Terraform plan workspace file") from None


@contextmanager
def _saved_plan_snapshot(plan_path: Path, *, require_kernel_seal: bool = True):
    try:
        with immutable_plan_snapshot(
            plan_path,
            max_bytes=MAX_SAVED_PLAN_BYTES,
            chunk_bytes=_COPY_CHUNK_BYTES,
            require_kernel_seal=require_kernel_seal,
        ) as snapshot:
            yield snapshot
    except ImmutablePlanSnapshotError as exc:
        raise PlanSealError(str(exc)) from None


def _enforce_snapshot_policy(environment: str, snapshot_path: str, descriptor: int) -> None:
    try:
        show = subprocess.Popen(
            ["terraform", "show", "-json", snapshot_path],
            stdout=subprocess.PIPE,
            pass_fds=(descriptor,),
            env=_scrubbed_environment(),
        )
    except OSError:
        raise PlanSealError("unable to inspect the Terraform plan snapshot") from None

    assert show.stdout is not None
    try:
        try:
            policy = subprocess.run(
                [sys.executable, str(POLICY_SCRIPT), environment, "-"],
                stdin=show.stdout,
                stdout=sys.stderr,
                check=False,
                timeout=_COMMAND_TIMEOUT_SECONDS,
                env=_scrubbed_environment(),
            )
        finally:
            show.stdout.close()
        try:
            show_returncode = show.wait(timeout=_COMMAND_TIMEOUT_SECONDS)
        except subprocess.TimeoutExpired:
            show.kill()
            show.wait()
            raise PlanSealError("Terraform plan inspection exceeded the time limit") from None
    except (OSError, subprocess.TimeoutExpired):
        show.kill()
        show.wait()
        raise PlanSealError("Terraform plan policy could not complete safely") from None

    if show_returncode != 0 or policy.returncode != 0:
        raise PlanSealError("Terraform plan snapshot failed policy inspection")


def _rewind_snapshot(descriptor: int) -> None:
    try:
        os.lseek(descriptor, 0, os.SEEK_SET)
    except OSError:
        raise PlanSealError("unable to rewind the Terraform plan snapshot") from None


def _encrypt_snapshot(
    snapshot_path: str,
    descriptor: int,
    destination: BinaryIO,
    passphrase: str,
) -> None:
    try:
        result = subprocess.run(
            [
                "openssl",
                "enc",
                "-aes-256-cbc",
                "-pbkdf2",
                "-iter",
                "200000",
                "-salt",
                "-in",
                snapshot_path,
                "-pass",
                "env:TFPLAN_PASSPHRASE",
            ],
            stdout=destination,
            check=False,
            pass_fds=(descriptor,),
            timeout=_COMMAND_TIMEOUT_SECONDS,
            env=_openssl_environment(passphrase),
        )
    except (OSError, subprocess.TimeoutExpired):
        raise PlanSealError("Terraform plan encryption could not complete safely") from None
    if result.returncode != 0:
        raise PlanSealError("Terraform plan encryption failed")


@contextmanager
def _atomic_output(path: Path) -> Iterator[BinaryIO]:
    temporary: Path | None = None
    try:
        path.parent.mkdir(parents=False, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            mode="w+b",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as stream:
            temporary = Path(stream.name)
            os.fchmod(stream.fileno(), 0o600)
            yield stream
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        temporary = None
    except OSError:
        raise PlanSealError("unable to write a private Terraform plan artifact") from None
    finally:
        if temporary is not None:
            try:
                temporary.unlink()
            except OSError:
                pass


def seal_terraform_plan(
    environment: str,
    plan_path: Path,
    encrypted_path: Path,
    checksum_path: Path,
    *,
    passphrase: str,
    require_kernel_seal: bool = True,
) -> str:
    """Seal a policy-approved plan and return its plaintext SHA-256 digest."""

    if len(passphrase) < 32:
        raise PlanSealError("plan passphrase is missing or too short")
    _remove(encrypted_path)
    _remove(checksum_path)
    try:
        with _saved_plan_snapshot(
            plan_path,
            require_kernel_seal=require_kernel_seal,
        ) as (snapshot_path, descriptor, digest):
            _remove(plan_path)
            _rewind_snapshot(descriptor)
            _enforce_snapshot_policy(environment, snapshot_path, descriptor)
            _rewind_snapshot(descriptor)
            with _atomic_output(encrypted_path) as encrypted:
                _encrypt_snapshot(snapshot_path, descriptor, encrypted, passphrase)
        with _atomic_output(checksum_path) as checksum:
            checksum.write(f"{digest}  tfplan\n".encode("ascii"))
        return digest
    except Exception:
        _remove(encrypted_path)
        _remove(checksum_path)
        raise
    finally:
        _remove(plan_path)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("environment")
    parser.add_argument("plan")
    parser.add_argument("encrypted_plan")
    parser.add_argument("checksum")
    args = parser.parse_args(argv)

    # Remove the secret from this process before Terraform or the policy helper
    # can be spawned. Only the one openssl child receives a one-off copy.
    passphrase = os.environ.pop(_PASSPHRASE_ENV, "")
    if len(passphrase) < 32:
        print(
            "Terraform plan sealing failed: plan passphrase is missing or too short",
            file=sys.stderr,
        )
        return 2
    try:
        digest = seal_terraform_plan(
            args.environment,
            Path(args.plan),
            Path(args.encrypted_plan),
            Path(args.checksum),
            passphrase=passphrase,
        )
    except PlanSealError as exc:
        print(f"Terraform plan sealing failed: {exc}", file=sys.stderr)
        return 3
    print(digest)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

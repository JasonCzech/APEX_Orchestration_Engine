"""Regression tests for immutable pre-approval Terraform plan sealing."""

from __future__ import annotations

import fcntl
import hashlib
import io
import os
import subprocess
import sys
from pathlib import Path
from typing import BinaryIO

import pytest
from scripts import immutable_plan_snapshot as immutable
from scripts import seal_terraform_plan as seal

_PASSPHRASE = "plan-passphrase-that-is-long-enough"


def test_policy_and_encryption_share_snapshot_when_workspace_path_is_replaced(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    plan = tmp_path / "tfplan"
    encrypted = tmp_path / "tfplan.enc"
    checksum = tmp_path / "tfplan.sha256"
    reviewed = b"policy-reviewed saved plan"
    replacement = b"unreviewed replacement"
    plan.write_bytes(reviewed)
    policy_payloads: list[bytes] = []
    encrypted_payloads: list[bytes] = []

    def enforce(_environment: str, snapshot_path: str, descriptor: int) -> None:
        assert fcntl.fcntl(descriptor, fcntl.F_GETFL) & os.O_ACCMODE == os.O_RDONLY
        policy_payloads.append(Path(snapshot_path).read_bytes())
        plan.write_bytes(replacement)

    def encrypt(
        snapshot_path: str,
        descriptor: int,
        destination: BinaryIO,
        passphrase: str,
    ) -> None:
        assert passphrase == _PASSPHRASE
        assert fcntl.fcntl(descriptor, fcntl.F_GETFL) & os.O_ACCMODE == os.O_RDONLY
        payload = Path(snapshot_path).read_bytes()
        encrypted_payloads.append(payload)
        destination.write(b"sealed:" + payload)

    monkeypatch.setattr(seal, "_enforce_snapshot_policy", enforce)
    monkeypatch.setattr(seal, "_encrypt_snapshot", encrypt)

    digest = seal.seal_terraform_plan(
        "prod",
        plan,
        encrypted,
        checksum,
        passphrase=_PASSPHRASE,
        require_kernel_seal=False,
    )

    expected_digest = hashlib.sha256(reviewed).hexdigest()
    assert digest == expected_digest
    assert policy_payloads == [reviewed]
    assert encrypted_payloads == [reviewed]
    assert encrypted.read_bytes() == b"sealed:" + reviewed
    assert checksum.read_text() == f"{expected_digest}  tfplan\n"
    assert not plan.exists()


def test_plan_snapshot_rejects_symlink_without_consuming_target(tmp_path: Path) -> None:
    target = tmp_path / "target"
    target.write_bytes(b"sensitive target")
    plan = tmp_path / "tfplan"
    plan.symlink_to(target)

    with pytest.raises(seal.PlanSealError, match="unavailable or unsafe"):
        with seal._saved_plan_snapshot(plan):
            pytest.fail("unsafe plan must not yield a snapshot")

    assert target.read_bytes() == b"sensitive target"


@pytest.mark.skipif(
    not sys.platform.startswith("linux") or not hasattr(os, "memfd_create"),
    reason="Linux memfd seals are the shared-runner enforcement primitive",
)
def test_shared_runner_snapshot_is_kernel_sealed_and_read_only(tmp_path: Path) -> None:
    plan = tmp_path / "tfplan"
    plan.write_bytes(b"exact approved plan")

    with immutable.immutable_plan_snapshot(
        plan,
        max_bytes=1024,
        chunk_bytes=16,
        require_kernel_seal=True,
    ) as (_snapshot_path, descriptor, _digest):
        flags = fcntl.fcntl(descriptor, fcntl.F_GETFL)
        seals = fcntl.fcntl(descriptor, immutable._F_GET_SEALS)
        assert flags & os.O_ACCMODE == os.O_RDONLY
        assert seals & immutable._REQUIRED_SEALS == immutable._REQUIRED_SEALS


def test_failed_policy_leaves_no_approval_artifacts(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    plan = tmp_path / "tfplan"
    encrypted = tmp_path / "tfplan.enc"
    checksum = tmp_path / "tfplan.sha256"
    plan.write_bytes(b"rejected plan")
    encrypted.write_bytes(b"stale ciphertext")
    checksum.write_text("stale checksum")

    def reject(_environment: str, _snapshot_path: str, _descriptor: int) -> None:
        raise seal.PlanSealError("policy rejected the snapshot")

    monkeypatch.setattr(seal, "_enforce_snapshot_policy", reject)

    with pytest.raises(seal.PlanSealError, match="policy rejected"):
        seal.seal_terraform_plan(
            "prod",
            plan,
            encrypted,
            checksum,
            passphrase=_PASSPHRASE,
            require_kernel_seal=False,
        )

    assert not plan.exists()
    assert not encrypted.exists()
    assert not checksum.exists()


def test_only_openssl_inherits_the_plan_passphrase(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    canary = "passphrase-canary-that-must-not-reach-terraform"
    monkeypatch.setenv("TFPLAN_PASSPHRASE", "ambient-secret-must-be-filtered")
    popen_calls: list[tuple[list[str], dict[str, str]]] = []
    run_calls: list[tuple[list[str], dict[str, str]]] = []

    def captured_environment(kwargs: dict[str, object]) -> dict[str, str]:
        raw = kwargs.get("env")
        assert isinstance(raw, dict)
        return {str(key): str(value) for key, value in raw.items()}

    class FakeShow:
        def __init__(self) -> None:
            self.stdout = io.BytesIO(b"{}")

        def wait(self, timeout: float | None = None) -> int:
            assert timeout == seal._COMMAND_TIMEOUT_SECONDS
            return 0

        def kill(self) -> None:
            pytest.fail("successful terraform show must not be killed")

    def fake_popen(command: list[str], **kwargs: object) -> FakeShow:
        popen_calls.append((command, captured_environment(kwargs)))
        return FakeShow()

    def fake_run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        run_calls.append((command, captured_environment(kwargs)))
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(seal.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(seal.subprocess, "run", fake_run)

    seal._enforce_snapshot_policy("prod", "/proc/self/fd/91", 91)
    seal._encrypt_snapshot("/proc/self/fd/91", 91, io.BytesIO(), canary)

    assert popen_calls[0][0][:3] == ["terraform", "show", "-json"]
    assert "TFPLAN_PASSPHRASE" not in popen_calls[0][1]
    policy_call, openssl_call = run_calls
    assert policy_call[0][0] == sys.executable
    assert "TFPLAN_PASSPHRASE" not in policy_call[1]
    assert openssl_call[0][:2] == ["openssl", "enc"]
    assert openssl_call[1]["TFPLAN_PASSPHRASE"] == canary


def test_main_removes_passphrase_from_its_process_before_sealing(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    canary = "main-passphrase-canary-that-is-long-enough"
    received: list[str] = []
    monkeypatch.setenv("TFPLAN_PASSPHRASE", canary)

    def fake_seal(
        _environment: str,
        _plan: Path,
        _encrypted: Path,
        _checksum: Path,
        *,
        passphrase: str,
        require_kernel_seal: bool = True,
    ) -> str:
        assert require_kernel_seal is True
        assert "TFPLAN_PASSPHRASE" not in os.environ
        received.append(passphrase)
        return "a" * 64

    monkeypatch.setattr(seal, "seal_terraform_plan", fake_seal)

    assert (
        seal.main(
            [
                "prod",
                str(tmp_path / "tfplan"),
                str(tmp_path / "tfplan.enc"),
                str(tmp_path / "tfplan.sha256"),
            ]
        )
        == 0
    )
    assert received == [canary]

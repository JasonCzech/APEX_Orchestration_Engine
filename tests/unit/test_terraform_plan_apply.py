"""Regression tests for anonymous approved-plan decryption and apply."""

from __future__ import annotations

import fcntl
import hashlib
import io
import os
import signal
import subprocess
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest
from scripts import apply_terraform_plan as apply_plan
from scripts.immutable_plan_snapshot import immutable_plan_stream

_PASSPHRASE = "plan-passphrase-that-is-long-enough"


def test_apply_uses_verified_read_only_snapshot_not_a_plaintext_workspace_path(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    encrypted = tmp_path / "tfplan.enc"
    encrypted.write_bytes(b"ciphertext placeholder")
    approved = b"exact approved binary plan"
    applied: list[bytes] = []

    @contextmanager
    def decrypt(
        _path: Path,
        *,
        passphrase: str,
        require_kernel_seal: bool,
    ) -> Iterator[tuple[str, int, str]]:
        assert passphrase == _PASSPHRASE
        assert require_kernel_seal is False
        with immutable_plan_stream(
            io.BytesIO(approved),
            max_bytes=1024,
            chunk_bytes=16,
        ) as snapshot:
            yield snapshot

    def apply(snapshot_path: str, descriptor: int) -> None:
        assert fcntl.fcntl(descriptor, fcntl.F_GETFL) & os.O_ACCMODE == os.O_RDONLY
        encrypted.write_bytes(b"same-runner replacement")
        applied.append(Path(snapshot_path).read_bytes())

    monkeypatch.setattr(apply_plan, "_decrypted_plan_snapshot", decrypt)
    monkeypatch.setattr(apply_plan, "_apply_snapshot", apply)

    apply_plan.apply_encrypted_terraform_plan(
        hashlib.sha256(approved).hexdigest(),
        encrypted,
        passphrase=_PASSPHRASE,
        require_kernel_seal=False,
    )

    assert applied == [approved]


def test_digest_mismatch_never_reaches_terraform(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    encrypted = tmp_path / "tfplan.enc"
    encrypted.write_bytes(b"ciphertext placeholder")

    @contextmanager
    def decrypt(
        _path: Path,
        *,
        passphrase: str,
        require_kernel_seal: bool,
    ) -> Iterator[tuple[str, int, str]]:
        assert passphrase == _PASSPHRASE
        del require_kernel_seal
        with immutable_plan_stream(
            io.BytesIO(b"different plan"),
            max_bytes=1024,
            chunk_bytes=16,
        ) as snapshot:
            yield snapshot

    monkeypatch.setattr(apply_plan, "_decrypted_plan_snapshot", decrypt)
    monkeypatch.setattr(
        apply_plan,
        "_apply_snapshot",
        lambda *_args: pytest.fail("digest mismatch must not reach Terraform"),
    )

    with pytest.raises(apply_plan.PlanApplyError, match="digest mismatch"):
        apply_plan.apply_encrypted_terraform_plan(
            "0" * 64,
            encrypted,
            passphrase=_PASSPHRASE,
            require_kernel_seal=False,
        )


def test_terraform_apply_never_inherits_plan_passphrase(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    class CompletedApply:
        pid = 31_337
        returncode = 0

        def wait(self, timeout: float | None = None) -> int:
            assert timeout == apply_plan._APPLY_TIMEOUT_SECONDS
            return 0

        def poll(self) -> int:
            return 0

    def popen(command: list[str], **kwargs: object) -> CompletedApply:
        captured.update(kwargs)
        assert command[:3] == ["terraform", "apply", "-input=false"]
        return CompletedApply()

    monkeypatch.setenv("TFPLAN_PASSPHRASE", "ambient-passphrase-canary")
    monkeypatch.setattr(apply_plan.subprocess, "Popen", popen)

    apply_plan._apply_snapshot("/proc/self/fd/42", 42)

    environment = captured["env"]
    assert isinstance(environment, dict)
    assert "TFPLAN_PASSPHRASE" not in environment
    assert captured["pass_fds"] == (42,)
    assert captured["start_new_session"] is True


def test_process_group_kill_clears_descendants_after_leader_exits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sent: list[int] = []
    waits: list[float | None] = []

    class ExitedLeader:
        pid = 41_337
        returncode = 0

        def wait(self, timeout: float | None = None) -> int:
            waits.append(timeout)
            return 0

        def poll(self) -> int:
            return 0

        def terminate(self) -> None:
            pytest.fail("process-group signaling should succeed")

        def kill(self) -> None:
            pytest.fail("process-group signaling should succeed")

    monkeypatch.setattr(apply_plan.os, "killpg", lambda _pid, signum: sent.append(signum))

    apply_plan._terminate_process_group(ExitedLeader())  # type: ignore[arg-type]

    assert sent == [signal.SIGTERM, signal.SIGKILL]
    assert waits == [
        apply_plan._PROCESS_TERMINATION_GRACE_SECONDS,
        apply_plan._PROCESS_KILL_WAIT_SECONDS,
    ]


def test_apply_timeout_terminates_the_complete_process_group(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sent: list[int] = []

    class StalledApply:
        pid = 51_337
        returncode: int | None = None
        waits = 0

        def wait(self, timeout: float | None = None) -> int:
            self.waits += 1
            if self.waits == 1:
                assert timeout is not None
                raise subprocess.TimeoutExpired("terraform", timeout)
            self.returncode = -signal.SIGTERM
            return self.returncode

        def poll(self) -> int | None:
            return self.returncode

        def terminate(self) -> None:
            self.returncode = -signal.SIGTERM

        def kill(self) -> None:
            self.returncode = -signal.SIGKILL

    monkeypatch.setattr(apply_plan.subprocess, "Popen", lambda *_args, **_kwargs: StalledApply())
    monkeypatch.setattr(apply_plan.os, "killpg", lambda _pid, signum: sent.append(signum))

    with pytest.raises(apply_plan.PlanApplyError, match="exceeded the time limit"):
        apply_plan._apply_snapshot("/proc/self/fd/52", 52)

    assert sent == [signal.SIGTERM, signal.SIGKILL]


def test_main_scrubs_passphrase_before_any_terraform_work(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    canary = "main-apply-passphrase-canary-long-enough"
    received: list[str] = []
    monkeypatch.setenv("TFPLAN_PASSPHRASE", canary)

    def apply(
        _digest: str,
        _encrypted: Path,
        *,
        passphrase: str,
        require_kernel_seal: bool = True,
    ) -> None:
        assert require_kernel_seal is True
        assert "TFPLAN_PASSPHRASE" not in os.environ
        received.append(passphrase)

    monkeypatch.setattr(apply_plan, "apply_encrypted_terraform_plan", apply)

    assert apply_plan.main(["a" * 64, str(tmp_path / "tfplan.enc")]) == 0
    assert received == [canary]

"""Failure-path coverage for release-scoped CSI hook compensation."""

from __future__ import annotations

import signal
import subprocess
from collections.abc import Callable

import pytest
from scripts import cleanup_csi_hook_material as cleanup
from scripts import deploy


class _CompletedProcess:
    def __init__(self, command: list[str], returncode: int) -> None:
        self.command = command
        self.returncode = returncode
        self.pid = 10_000 + id(self) % 10_000
        self.completed = False

    def poll(self) -> int | None:
        return self.returncode if self.completed else None

    def wait(self, timeout: float | None = None) -> int:
        del timeout
        self.completed = True
        return self.returncode

    def terminate(self) -> None:
        self.returncode = -signal.SIGTERM
        self.completed = True

    def kill(self) -> None:
        self.returncode = -signal.SIGKILL
        self.completed = True


def _install_completed_kubectl(
    monkeypatch: pytest.MonkeyPatch,
    returncode: Callable[[int, list[str]], int] = lambda _index, _command: 0,
) -> tuple[list[list[str]], list[float]]:
    commands: list[list[str]] = []
    waits: list[float] = []

    def popen(
        command: list[str],
        *,
        stdout: int,
        stderr: int,
        start_new_session: bool,
    ) -> _CompletedProcess:
        assert stdout == subprocess.DEVNULL
        assert stderr == subprocess.DEVNULL
        assert start_new_session is True
        index = len(commands)
        commands.append(command)
        process = _CompletedProcess(command, returncode(index, command))
        original_wait = process.wait

        def wait(timeout: float | None = None) -> int:
            assert timeout is not None
            waits.append(timeout)
            return original_wait(timeout)

        process.wait = wait  # type: ignore[method-assign]
        return process

    monkeypatch.setattr(cleanup.subprocess, "Popen", popen)
    return commands, waits


def test_cleanup_attempts_every_resource_after_an_intermediate_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    commands, waits = _install_completed_kubectl(
        monkeypatch,
        lambda _index, command: (
            9 if "secretproviderclasses.secrets-store.csi.x-k8s.io" in command else 0
        ),
    )

    succeeded = cleanup.cleanup_csi_hook_material(
        namespace="apex",
        release="prod",
        hook_secrets=("apex-database-admin", "apex-admin", "apex-admin"),
    )

    assert succeeded is False
    assert len(commands) == 8
    assert len(waits) == 8
    assert all(0 < wait <= 120 for wait in waits)
    assert all(wait <= 1 for wait in waits[:3])
    assert [command[4] for command in commands[:3]] == [
        "rolebindings.rbac.authorization.k8s.io",
        "roles.rbac.authorization.k8s.io",
        "serviceaccounts",
    ]
    assert commands[3][4] == "jobs"
    assert "csi-sync" in commands[3][6]
    assert "--cascade=foreground" in commands[3]
    assert commands[4][4] == "pods"
    assert commands[4][6] == commands[3][6]
    assert commands[6][4] == "secrets"
    assert "csi-hook-secret" in commands[6][6]
    assert commands[7][-3:] == ["--", "apex-database-admin", "apex-admin"]
    assert all(token != "apex-database" for command in commands for token in command)
    serialized = "\n".join(" ".join(command) for command in commands)
    assert "csi-runtime-hook" in serialized


@pytest.mark.parametrize("timeout", ["", "0s", "601s", "11m", "1h", "120s\nforged"])
def test_cleanup_rejects_invalid_or_unbounded_timeout_before_kubectl(
    monkeypatch: pytest.MonkeyPatch,
    timeout: str,
) -> None:
    monkeypatch.setattr(
        cleanup.subprocess,
        "Popen",
        lambda *_args, **_kwargs: pytest.fail("invalid timeout must not reach kubectl"),
    )

    with pytest.raises(ValueError, match="no greater than 10m"):
        cleanup.cleanup_csi_hook_material(namespace="apex", release="prod", timeout=timeout)


@pytest.mark.parametrize("failed_call", [0, 1])
def test_cleanup_never_deletes_secret_material_unless_both_drains_succeed(
    monkeypatch: pytest.MonkeyPatch,
    failed_call: int,
) -> None:
    drain_call = failed_call + 3
    commands, _waits = _install_completed_kubectl(
        monkeypatch,
        lambda index, _command: 9 if index == drain_call else 0,
    )

    assert (
        cleanup.cleanup_csi_hook_material(
            namespace="apex",
            release="prod",
            hook_secrets=("apex-database-admin",),
        )
        is False
    )
    assert len(commands) == 5
    assert [command[4] for command in commands[:3]] == [
        "rolebindings.rbac.authorization.k8s.io",
        "roles.rbac.authorization.k8s.io",
        "serviceaccounts",
    ]
    assert commands[3][4] == "jobs"
    assert commands[4][4] == "pods"
    resources = [command[4] for command in commands]
    assert "secretproviderclasses.secrets-store.csi.x-k8s.io" not in resources
    assert "secrets" not in resources


def test_cleanup_uses_one_aggregate_budget_and_reaches_both_drains(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = 0.0
    commands: list[list[str]] = []
    command_waits: list[float] = []
    group_signals: list[int] = []

    class HangingProcess(_CompletedProcess):
        def __init__(self, command: list[str]) -> None:
            super().__init__(command, 0)
            self.killed = False

        def wait(self, timeout: float | None = None) -> int:
            nonlocal clock
            assert timeout is not None
            if self.killed:
                self.completed = True
                return -signal.SIGKILL
            command_waits.append(timeout)
            clock += timeout
            raise subprocess.TimeoutExpired(self.command, timeout)

    processes: dict[int, HangingProcess] = {}

    def popen(command: list[str], **kwargs: object) -> HangingProcess:
        assert kwargs["start_new_session"] is True
        commands.append(command)
        process = HangingProcess(command)
        processes[process.pid] = process
        return process

    def killpg(pid: int, sent_signal: int) -> None:
        group_signals.append(sent_signal)
        if sent_signal == signal.SIGKILL:
            processes[pid].killed = True

    monkeypatch.setattr(cleanup.time, "monotonic", lambda: clock)
    monkeypatch.setattr(cleanup.subprocess, "Popen", popen)
    monkeypatch.setattr(cleanup.os, "killpg", killpg)

    assert (
        cleanup.cleanup_csi_hook_material(namespace="apex", release="prod", timeout="20s") is False
    )

    resources = [command[4] for command in commands]
    assert resources[:3] == [
        "rolebindings.rbac.authorization.k8s.io",
        "roles.rbac.authorization.k8s.io",
        "serviceaccounts",
    ]
    assert resources[3:] == ["jobs", "pods"]
    assert sum(command_waits[:3]) <= 3.0
    assert clock <= 20.0
    assert signal.SIGTERM in group_signals
    assert signal.SIGKILL in group_signals
    assert not any(
        "secretproviderclasses" in resource or resource == "secrets" for resource in resources
    )


def test_termination_kills_descendants_even_when_group_leader_exits_after_term(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    process = _CompletedProcess(["kubectl"], 0)
    sent: list[int] = []

    def killpg(_pid: int, sent_signal: int) -> None:
        sent.append(sent_signal)

    monkeypatch.setattr(cleanup.os, "killpg", killpg)

    cleanup._terminate_process_group(process, deadline=cleanup.time.monotonic() + 1)

    # A successful wait observes only the group leader. SIGKILL is still sent
    # to clear a same-session child that ignored the earlier TERM.
    assert sent == [signal.SIGTERM, signal.SIGKILL]


@pytest.mark.parametrize("sent_signal", [signal.SIGHUP, signal.SIGINT, signal.SIGTERM])
def test_cleanup_signal_shortens_deadline_and_terminates_active_group(
    monkeypatch: pytest.MonkeyPatch,
    sent_signal: int,
) -> None:
    clock = 100.0
    runner = cleanup._CleanupRunner(120)
    process = _CompletedProcess(["kubectl"], 0)
    runner.active = process
    group_signals: list[tuple[int, int]] = []
    monkeypatch.setattr(cleanup.time, "monotonic", lambda: clock)
    monkeypatch.setattr(
        cleanup.os,
        "killpg",
        lambda pid, signal_number: group_signals.append((pid, signal_number)),
    )

    with pytest.raises(cleanup._CleanupInterrupted):
        runner.handle_signal(sent_signal, None)

    assert runner.signal_count == 1
    assert runner.deadline == clock + cleanup._SIGNAL_CLEANUP_SECONDS
    assert group_signals == [(process.pid, signal.SIGTERM)]


def test_main_rejects_arguments_without_reflecting_them(
    capsys: pytest.CaptureFixture[str],
) -> None:
    canary = "selector-injection-canary"

    assert cleanup.main(["--namespace", f"apex;{canary}", "--release", "prod"]) == 2

    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == "invalid CSI hook cleanup arguments\n"
    assert canary not in captured.err


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("namespace", "apex;delete"),
        ("release", "prod,app.kubernetes.io/component=csi-hook"),
        ("hook_secret", "../runtime-secret"),
    ],
)
def test_cleanup_rejects_selector_or_name_injection_before_kubectl(
    monkeypatch: pytest.MonkeyPatch,
    field: str,
    value: str,
) -> None:
    called = False

    def fake_run(*_args: object, **_kwargs: object) -> object:
        nonlocal called
        called = True
        return object()

    monkeypatch.setattr(cleanup.subprocess, "Popen", fake_run)
    kwargs: dict[str, object] = {
        "namespace": "apex",
        "release": "prod",
        "hook_secrets": ("apex-admin",),
    }
    if field == "hook_secret":
        kwargs["hook_secrets"] = (value,)
    else:
        kwargs[field] = value

    with pytest.raises(ValueError):
        cleanup.cleanup_csi_hook_material(**kwargs)  # type: ignore[arg-type]

    assert called is False


def test_helm_install_enforces_atomic_wait_and_cleanup_on_fail(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    commands: list[list[str]] = []
    monkeypatch.setattr(deploy, "run", commands.append)

    deploy.helm_install(["prod", "apex", "--timeout", "20m"])

    assert commands[0][-5:] == [
        "--timeout",
        "20m",
        "--atomic",
        "--cleanup-on-fail",
        "--wait",
    ]


def test_failed_generic_helm_install_runs_release_scoped_csi_compensation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cleanup_calls: list[tuple[str, str, tuple[str, ...]]] = []

    def fail_helm(_command: list[str]) -> None:
        raise SystemExit(17)

    def compensate(
        *,
        namespace: str,
        release: str,
        hook_secrets: tuple[str, ...],
    ) -> bool:
        cleanup_calls.append((namespace, release, hook_secrets))
        return True

    monkeypatch.setattr(deploy, "run", fail_helm)
    monkeypatch.setattr(deploy, "cleanup_csi_hook_material", compensate)

    with pytest.raises(SystemExit) as caught:
        deploy.helm_install(["custom", "tenant"])

    assert caught.value.code == 17
    assert cleanup_calls == [("tenant", "custom", ())]


def test_failed_helm_scope_compensates_once_and_preserves_original_failure() -> None:
    cleanup_calls = 0

    def compensate() -> None:
        nonlocal cleanup_calls
        cleanup_calls += 1

    with pytest.raises(SystemExit, match="helm failed"):
        with deploy._cleanup_on_failed_helm(compensate):
            raise SystemExit("helm failed")

    assert cleanup_calls == 1

    with deploy._cleanup_on_failed_helm(compensate):
        pass
    assert cleanup_calls == 1


def test_failed_helm_scope_treats_termination_as_compensated_interrupt() -> None:
    cleanup_calls = 0
    original = signal.getsignal(signal.SIGTERM)

    def compensate() -> None:
        nonlocal cleanup_calls
        cleanup_calls += 1

    with pytest.raises(KeyboardInterrupt, match="signal"):
        with deploy._cleanup_on_failed_helm(compensate):
            handler = signal.getsignal(signal.SIGTERM)
            assert callable(handler)
            handler(signal.SIGTERM, None)

    assert cleanup_calls == 1
    assert signal.getsignal(signal.SIGTERM) == original

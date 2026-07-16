"""Remove release-scoped CSI hook credentials after a failed Helm operation.

Helm hook resources are not ordinary release resources, so an interrupted
install/upgrade can leave the CSI sync pod (and therefore its synchronized
native Secrets) behind even when ``--atomic`` rolls the release back.  The
supported deploy paths invoke this helper from their failure/signal handlers.
It deliberately selects only release-labelled hook resources and explicitly
named hook-only Secrets; runtime CSI resources and Secrets are out of scope.
"""

from __future__ import annotations

import argparse
import math
import os
import re
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from types import FrameType
from typing import Protocol

_DNS_SUBDOMAIN = re.compile(
    r"[a-z0-9](?:[-a-z0-9]*[a-z0-9])?(?:\.[a-z0-9](?:[-a-z0-9]*[a-z0-9])?)*"
)
_DNS_LABEL = re.compile(r"[a-z0-9](?:[-a-z0-9]*[a-z0-9])?")
_RELEASE = re.compile(r"[a-z0-9](?:[-a-z0-9]*[a-z0-9])?")
_TIMEOUT = re.compile(r"([1-9][0-9]{0,5})(ms|s|m)")
_MAX_TIMEOUT_SECONDS = 600.0
_IDENTITY_COMMAND_MAX_SECONDS = 1.0
_SIGNAL_CLEANUP_SECONDS = 8.0
_TERMINATION_GRACE_SECONDS = 0.5
_MIN_COMMAND_SECONDS = 0.01
_TIMEOUT_MARKER = "__APEX_AGGREGATE_TIMEOUT__"

_HOOK_COMPONENTS = (
    "bootstrap",
    "csi-cleanup",
    "csi-post-cleanup",
    "csi-sync",
    "database-grants",
    "database-role-cleanup",
    "database-role-provisioning",
    "migrate",
)
_PRIVILEGED_COMPONENTS = (
    "csi-cleanup",
    "csi-hook",
    "csi-post-cleanup",
    "database-role-cleanup",
)


class _Process(Protocol):
    pid: int

    def poll(self) -> int | None: ...

    def wait(self, timeout: float | None = None) -> int: ...

    def terminate(self) -> None: ...

    def kill(self) -> None: ...


class _CleanupInterrupted(BaseException):
    """Break an in-flight wait so group termination runs outside a signal handler."""


def _validated_dns_subdomain(value: str, *, label: str) -> str:
    if len(value) > 253 or _DNS_SUBDOMAIN.fullmatch(value) is None:
        raise ValueError(f"{label} must be a valid Kubernetes DNS subdomain")
    return value


def _validated_namespace(value: str) -> str:
    if len(value) > 63 or _DNS_LABEL.fullmatch(value) is None:
        raise ValueError("namespace must be a valid Kubernetes DNS label")
    return value


def _validated_release(value: str) -> str:
    if len(value) > 53 or _RELEASE.fullmatch(value) is None:
        raise ValueError("release must be a valid Helm release name")
    return value


def _validated_timeout(value: str) -> tuple[str, float]:
    match = _TIMEOUT.fullmatch(value)
    if match is None:
        raise ValueError("timeout must be a positive Kubernetes duration no greater than 10m")
    amount = int(match.group(1))
    unit = match.group(2)
    seconds = amount / 1_000 if unit == "ms" else amount * (60 if unit == "m" else 1)
    if seconds > _MAX_TIMEOUT_SECONDS:
        raise ValueError("timeout must be a positive Kubernetes duration no greater than 10m")
    return value, seconds


def _selector(release: str, components: Sequence[str]) -> str:
    joined = ",".join(components)
    return f"app.kubernetes.io/instance={release},app.kubernetes.io/component in ({joined})"


def _kubernetes_timeout(seconds: float) -> str:
    milliseconds = max(1, math.floor(seconds * 1_000))
    return f"{milliseconds}ms"


def _terminate_process_group(
    process: _Process,
    *,
    deadline: float,
) -> None:
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    except OSError:
        try:
            process.terminate()
        except OSError:
            pass

    remaining = max(0.0, min(_TERMINATION_GRACE_SECONDS, deadline - time.monotonic()))
    try:
        process.wait(timeout=remaining)
    except subprocess.TimeoutExpired:
        pass
    # The group leader exiting after TERM does not prove that its children
    # exited. Always probe/kill the complete session after the grace period so
    # a TERM-ignoring kubectl descendant cannot retain credentials or mounts.
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    except OSError:
        try:
            process.kill()
        except OSError:
            pass
    try:
        process.wait(timeout=max(0.0, deadline - time.monotonic()))
    except subprocess.TimeoutExpired:
        # SIGKILL has already been sent to the complete process group. Never
        # let a pathological kernel wait consume the remaining cleanup window.
        pass


class _CleanupRunner:
    """Run kubectl commands inside one wall-clock and process-group boundary."""

    def __init__(self, timeout_seconds: float) -> None:
        self.deadline = time.monotonic() + timeout_seconds
        self.active: _Process | None = None
        self.signal_count = 0
        self._handling_signal = False

    def handle_signal(self, _signum: int, _frame: FrameType | None) -> None:
        self.signal_count += 1
        self.deadline = min(self.deadline, time.monotonic() + _SIGNAL_CLEANUP_SECONDS)
        if self._handling_signal:
            return
        active = self.active
        if active is not None:
            self._handling_signal = True
            try:
                # Signal handlers must not call Popen.wait: re-entering its
                # internal waitpid lock can deadlock. Nudge the group, then
                # unwind to ``run`` for bounded TERM -> SIGKILL escalation.
                try:
                    os.killpg(active.pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
                except OSError:
                    try:
                        active.terminate()
                    except OSError:
                        pass
            finally:
                self._handling_signal = False
            raise _CleanupInterrupted

    def run(
        self,
        command: list[str],
        *,
        budget_fraction: float,
        max_seconds: float | None = None,
    ) -> bool:
        remaining = self.deadline - time.monotonic()
        budget = remaining * budget_fraction
        if max_seconds is not None:
            budget = min(budget, max_seconds)
        if budget < _MIN_COMMAND_SECONDS:
            return False
        command_deadline = min(self.deadline, time.monotonic() + budget)

        duration = _kubernetes_timeout(budget)
        bounded_command = [
            f"--timeout={duration}" if token == _TIMEOUT_MARKER else token for token in command
        ]
        generation = self.signal_count
        try:
            process = subprocess.Popen(
                bounded_command,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
        except OSError:
            return False
        self.active = process
        try:
            try:
                # Close the spawn/signal race: a signal just before ``active``
                # was assigned still terminates this newly-created group.
                if self.signal_count != generation:
                    _terminate_process_group(process, deadline=self.deadline)
                return process.wait(timeout=max(0.0, command_deadline - time.monotonic())) == 0
            except _CleanupInterrupted:
                # Prevent a second signal from re-entering Popen.wait while the
                # bounded escalation below owns the process group.
                self.active = None
                _terminate_process_group(process, deadline=self.deadline)
                return False
            except subprocess.TimeoutExpired:
                self.active = None
                _terminate_process_group(process, deadline=command_deadline)
                return False
            except OSError:
                self.active = None
                _terminate_process_group(process, deadline=self.deadline)
                return False
        finally:
            if self.active is process:
                self.active = None


@contextmanager
def _cleanup_signal_handlers(runner: _CleanupRunner) -> Iterator[None]:
    if threading.current_thread() is not threading.main_thread():
        yield
        return
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
    try:
        for candidate in watched:
            signal.signal(candidate, runner.handle_signal)
        yield
    finally:
        active = runner.active
        if active is not None:
            _terminate_process_group(active, deadline=runner.deadline)
        for candidate, handler in previous.items():
            signal.signal(candidate, handler)


def cleanup_csi_hook_material(
    *,
    namespace: str,
    release: str,
    hook_secrets: Sequence[str] = (),
    timeout: str = "120s",
    kubectl: str = "kubectl",
) -> bool:
    """Attempt every cleanup operation and return whether all of them succeeded."""

    namespace = _validated_namespace(namespace)
    release = _validated_release(release)
    timeout, timeout_seconds = _validated_timeout(timeout)
    exact_secrets = tuple(
        dict.fromkeys(_validated_dns_subdomain(name, label="hook secret") for name in hook_secrets)
    )
    common = [
        kubectl,
        "-n",
        namespace,
        "delete",
    ]
    del timeout
    wait_flags = ["--ignore-not-found=true", "--wait=true", f"--timeout={_TIMEOUT_MARKER}"]
    drain_commands = [
        # Stop every release hook Job first. Kubernetes otherwise defaults Job
        # deletion to background propagation, so ``--wait`` can return after
        # the Job object disappears while its CSI-mounted Pod is still running.
        [
            *common,
            "jobs",
            "-l",
            _selector(release, _HOOK_COMPONENTS),
            "--cascade=foreground",
            *wait_flags,
        ],
        # Explicitly drain matching Pods as a fail-safe when foreground Job
        # deletion timed out or a stranded hook Pod lost its owner reference.
        # Native synchronized Secrets must not be removed until these mounts
        # have disappeared, or the CSI driver can recreate them concurrently.
        [*common, "pods", "-l", _selector(release, _HOOK_COMPONENTS), *wait_flags],
    ]
    secret_commands = [
        [
            *common,
            "secretproviderclasses.secrets-store.csi.x-k8s.io",
            "-l",
            _selector(release, ("csi-hook", "csi-runtime-hook")),
            *wait_flags,
        ],
        # New hook Secrets carry this release/component label. Exact names are
        # also deleted for first-adoption upgrades from an older unlabelled chart.
        [
            *common,
            "secrets",
            "-l",
            _selector(release, ("csi-hook-secret",)),
            *wait_flags,
        ],
    ]
    if exact_secrets:
        secret_commands.append([*common, "secrets", *wait_flags, "--", *exact_secrets])
    identity_commands = [
        [
            *common,
            "rolebindings.rbac.authorization.k8s.io",
            "-l",
            _selector(release, _PRIVILEGED_COMPONENTS),
            *wait_flags,
        ],
        [
            *common,
            "roles.rbac.authorization.k8s.io",
            "-l",
            _selector(release, _PRIVILEGED_COMPONENTS),
            *wait_flags,
        ],
        [
            *common,
            "serviceaccounts",
            "-l",
            _selector(release, _PRIVILEGED_COMPONENTS),
            *wait_flags,
        ],
    ]

    # Revoke reusable hook identities first. This helper runs under the trusted
    # deploy identity, so removing hook RBAC cannot prevent its later drains;
    # each revocation receives at most one second and 5% of the remaining
    # aggregate window, so all three types are attempted without consuming the
    # Job/Pod drain or safe-Secret-cleanup reserves.
    runner = _CleanupRunner(timeout_seconds)
    succeeded = True
    with _cleanup_signal_handlers(runner):
        for command in identity_commands:
            if not runner.run(
                command,
                budget_fraction=0.05,
                max_seconds=_IDENTITY_COMMAND_MAX_SECONDS,
            ):
                succeeded = False

        # Job foreground deletion gets at most 40% of what remains; the Pod
        # fail-safe then gets at most 55%. Even when either command hangs, the
        # other drain is attempted and a bounded tail remains. Secret deletion
        # is still strictly gated on both successful drains.
        job_drained = runner.run(drain_commands[0], budget_fraction=0.40)
        pod_drained = runner.run(drain_commands[1], budget_fraction=0.55)
        quiesced = job_drained and pod_drained
        succeeded = succeeded and quiesced
        if quiesced:
            for index, command in enumerate(secret_commands):
                commands_left = len(secret_commands) - index
                if not runner.run(command, budget_fraction=1.0 / commands_left):
                    succeeded = False

    return succeeded and runner.signal_count == 0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--namespace", required=True)
    parser.add_argument("--release", required=True)
    parser.add_argument("--hook-secret", action="append", default=[])
    parser.add_argument("--timeout", default="120s")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        succeeded = cleanup_csi_hook_material(
            namespace=args.namespace,
            release=args.release,
            hook_secrets=args.hook_secret,
            timeout=args.timeout,
        )
    except ValueError:
        # Keep all caller-controlled identifiers and duration text out of CI
        # diagnostics. The parser's detailed validation is useful to API
        # callers, but this executable boundary must remain non-reflective.
        print("invalid CSI hook cleanup arguments", file=sys.stderr)
        return 2
    if not succeeded:
        print("one or more CSI hook cleanup operations failed", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

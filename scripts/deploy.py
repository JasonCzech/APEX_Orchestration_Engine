"""Turnkey deploy/operate tasks for APEX (stdlib only; mirrors scripts/dev.py).

Wraps the image build, Compose stacks, Helm install, and the AKS bring-up so each
target is one command. Shells out to docker / helm / terraform / az / kubectl —
those must be on PATH. Run with no args to list tasks.

  uv run python scripts/deploy.py image-build [tag]
  uv run python scripts/deploy.py compose-up
  uv run python scripts/deploy.py aks-up            # needs APEX_ENV + az login (see README)
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shlex
import signal
import subprocess
import sys
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path

try:
    from scripts.cleanup_csi_hook_material import cleanup_csi_hook_material
    from scripts.immutable_plan_snapshot import (
        ImmutablePlanSnapshotError,
        immutable_plan_snapshot,
    )
    from scripts.terraform_plan_policy import MAX_PLAN_JSON_BYTES, enforce_plan_policy
except ModuleNotFoundError:  # `python scripts/deploy.py` puts scripts/ on sys.path.
    from cleanup_csi_hook_material import cleanup_csi_hook_material
    from immutable_plan_snapshot import ImmutablePlanSnapshotError, immutable_plan_snapshot
    from terraform_plan_policy import MAX_PLAN_JSON_BYTES, enforce_plan_policy

REPO_ROOT = Path(__file__).resolve().parent.parent
CHART = "deploy/helm/apex-orchestration-engine"
TF = "deploy/terraform"
BACKUP_TF = "deploy/terraform/backup"
DEFAULT_TAG = "local"
LOCKED_ENVIRONMENTS = frozenset({"staging", "prod"})
TRIVY_VERSION = "0.70.0"
MAX_CAPTURE_BYTES = 8 * 1024 * 1024
MAX_SAVED_PLAN_BYTES = 512 * 1024 * 1024
_STREAM_CHUNK_BYTES = 1024 * 1024
_OCI_DIGEST = re.compile(r"sha256:[0-9a-f]{64}")
_OCI_TAG = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}")
_DNS_LABEL = re.compile(r"[a-z0-9](?:[-a-z0-9]*[a-z0-9])?")
_KUBERNETES_VERSION = re.compile(r"1\.[0-9]+(?:\.[0-9]+)?")
_DISPLAY_REDACTED = "[REDACTED]"
_DISPLAY_URL_USERINFO = re.compile(
    r"(?i)(?P<scheme>\b[a-z][a-z0-9+.-]*://)(?P<userinfo>[^/@\s?#]+@)"
)
_DISPLAY_SIGNED_QUERY = re.compile(
    r"(?i)(?P<prefix>[?&](?:password|passwd|pwd|passphrase|secret|client[_-]?secret|"
    r"authorization|credential|sig(?:nature)?|token|api[_-]?key|access[_-]?key|"
    r"x-amz-(?:credential|signature|security-token)|x-goog-signature)=)[^&#\s]*"
)
_DISPLAY_SENSITIVE_NAME = re.compile(
    r"(?i)(?:^|[._-])(?:password|passwd|pwd|passphrase|secret|client[_-]?secret|"
    r"(?:access|refresh|identity|id|session|security|api|auth|sas)?[_-]?token|"
    r"api[_-]?key|access[_-]?key|private[_-]?key|ssh[_-]?key|signing[_-]?key|"
    r"encryption[_-]?key|authorization|credential|credentials|signature|dsn|"
    r"connection[_-]?string|database[_-]?(?:uri|url))$"
)
_DISPLAY_VALUE_OPTIONS = frozenset(
    {
        "--docker-password",
        "--key",
        "--password",
        "--private-key",
        "--token",
    }
)
_DISPLAY_ASSIGNMENT_OPTIONS = frozenset(
    {
        "--backend-config",
        "--from-literal",
        "--set",
        "--set-file",
        "--set-json",
        "--set-string",
        "-backend-config",
        "-var",
    }
)

_AZURE_CSI_HOOK_SECRETS = (
    "apex-database-admin",
    "apex-database-role-claim",
    "apex-database-bootstrap",
    "apex-database-migration",
    "apex-admin",
    "apex-hook-auth",
)

Task = Callable[[list[str]], None]
TASKS: dict[str, tuple[str, Task]] = {}


def task(name: str, help_text: str) -> Callable[[Task], Task]:
    def register(func: Task) -> Task:
        TASKS[name] = (help_text, func)
        return func

    return register


def run(command: list[str], *, pass_fds: tuple[int, ...] = ()) -> None:
    # Display is a separate trust boundary from execution. Commands can carry
    # Helm/Terraform assignments, registry userinfo, signed URLs, and local
    # private-key paths. Keep subprocess argv exact while ensuring CI logs never
    # become a credential sink or accept multiline terminal injection.
    print("+ " + _display_command(command), flush=True)
    try:
        if pass_fds:
            subprocess.run(command, cwd=REPO_ROOT, check=True, pass_fds=pass_fds)
        else:
            subprocess.run(command, cwd=REPO_ROOT, check=True)
    except FileNotFoundError:
        executable = _single_line_display(command[0]) if command else "<empty command>"
        raise SystemExit(
            f"Required executable not found: {executable}. Install it / add to PATH."
        ) from None
    except OSError:
        raise SystemExit("Unable to execute the required command.") from None
    except subprocess.CalledProcessError as exc:
        raise SystemExit(exc.returncode) from None


def _display_command(command: list[str]) -> str:
    """Render argv for logs with secrets removed and controls made visible."""

    rendered: list[str] = []
    redact_next = False
    assignment_next = False
    for raw_argument in command:
        argument = str(raw_argument)
        if redact_next:
            rendered.append(_DISPLAY_REDACTED)
            redact_next = False
            continue
        if assignment_next:
            rendered.append(_redact_display_argument(argument, assignment=True))
            assignment_next = False
            continue

        option, separator, inline_value = argument.partition("=")
        if option in _DISPLAY_VALUE_OPTIONS:
            if separator:
                rendered.append(f"{option}={_DISPLAY_REDACTED}")
            else:
                rendered.append(_single_line_display(option))
                redact_next = True
            continue
        if option.startswith("-") and _DISPLAY_SENSITIVE_NAME.search(option.lstrip("-")):
            if separator:
                rendered.append(f"{_single_line_display(option)}={_DISPLAY_REDACTED}")
            else:
                rendered.append(_single_line_display(option))
                redact_next = True
            continue
        if option in _DISPLAY_ASSIGNMENT_OPTIONS:
            if separator:
                rendered.append(
                    f"{option}={_redact_display_argument(inline_value, assignment=True)}"
                )
            else:
                rendered.append(_single_line_display(option))
                assignment_next = True
            continue
        rendered.append(_redact_display_argument(argument, assignment=False))
    return shlex.join(rendered)


def _redact_display_argument(argument: str, *, assignment: bool) -> str:
    """Redact one display-only argument without changing the executed argv."""

    value = _single_line_display(argument)
    value = _DISPLAY_URL_USERINFO.sub(
        lambda match: f"{match.group('scheme')}{_DISPLAY_REDACTED}@",
        value,
    )
    value = _DISPLAY_SIGNED_QUERY.sub(
        lambda match: f"{match.group('prefix')}{_DISPLAY_REDACTED}",
        value,
    )
    if assignment and _assignment_contains_secret(value):
        return _DISPLAY_REDACTED
    return value


def _assignment_contains_secret(value: str) -> bool:
    # Helm accepts comma-separated path=value assignments; Terraform accepts a
    # single name=value pair. Redact the complete structured argument when any
    # field is credential-bearing so quoting/escaping cannot expose fragments.
    for candidate in re.split(r"(?<!\\),", value):
        name, separator, _raw_value = candidate.partition("=")
        if not separator:
            continue
        leaf = re.sub(r"\[[^]]*\]", "", name).rsplit(".", 1)[-1]
        if _DISPLAY_SENSITIVE_NAME.search(leaf) is not None:
            return True
    return False


def _single_line_display(value: str, *, limit: int = 4_096) -> str:
    """Escape log-forging controls and bound one rendered argv component."""

    pieces: list[str] = []
    length = 0
    for character in value:
        codepoint = ord(character)
        if character == "\n":
            piece = r"\n"
        elif character == "\r":
            piece = r"\r"
        elif character == "\t":
            piece = r"\t"
        elif codepoint < 0x20 or codepoint == 0x7F:
            piece = f"\\x{codepoint:02x}"
        elif character in {"\u2028", "\u2029"}:
            piece = f"\\u{codepoint:04x}"
        else:
            piece = character
        if length + len(piece) > limit:
            pieces.append("...[truncated]")
            break
        pieces.append(piece)
        length += len(piece)
    return "".join(pieces)


def capture(command: list[str]) -> str:
    return _capture_bounded(
        command,
        max_output_bytes=MAX_CAPTURE_BYTES,
        output_description="Command output",
    )


def _capture_bounded(
    command: list[str],
    *,
    max_output_bytes: int,
    output_description: str,
    allow_failure: bool = False,
    pass_fds: tuple[int, ...] = (),
) -> str:
    """Capture one command without allowing stdout to exhaust process memory."""

    try:
        process = subprocess.Popen(
            command,
            cwd=REPO_ROOT,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            pass_fds=pass_fds,
        )
    except FileNotFoundError:
        executable = _single_line_display(command[0]) if command else "<empty command>"
        raise SystemExit(
            f"Required executable not found: {executable}. Install it / add to PATH."
        ) from None
    except OSError:
        raise SystemExit("Unable to execute the required command.") from None

    output = bytearray()
    assert process.stdout is not None
    try:
        while len(output) <= max_output_bytes:
            chunk = process.stdout.read(
                min(_STREAM_CHUNK_BYTES, max_output_bytes + 1 - len(output))
            )
            if not chunk:
                break
            output.extend(chunk)
        if len(output) > max_output_bytes:
            process.kill()
            process.wait()
            raise SystemExit(f"{output_description} exceeds the size limit.")
        return_code = process.wait()
    except BaseException:
        if process.poll() is None:
            process.kill()
            process.wait()
        raise
    finally:
        process.stdout.close()

    if return_code != 0 and not allow_failure:
        raise SystemExit(
            f"Command failed with exit code {return_code}: {_display_command(command)}"
        )
    if return_code != 0:
        return ""
    try:
        return output.decode("utf-8").strip()
    except UnicodeDecodeError:
        raise SystemExit(f"{output_description} is not valid UTF-8.") from None


def require_env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise SystemExit(f"${name} is required for this task.")
    return value


def _arg(args: list[str], index: int, default: str) -> str:
    return args[index] if len(args) > index else default


# ── images ────────────────────────────────────────────────────────────────────


def _image_reference_tag(image: str) -> str:
    """Return a validated tag from an image reference (or Docker's ``latest``)."""

    last_slash = image.rfind("/")
    last_colon = image.rfind(":")
    tag = image[last_colon + 1 :] if last_colon > last_slash else "latest"
    return _validate_oci_tag(tag)


def _build_server_image(image: str, version: str) -> None:
    run(
        [
            "uv",
            "run",
            "langgraph",
            "build",
            "-t",
            image,
            "--build-arg",
            f"APEX_BUILD_VERSION={version}",
        ]
    )


def _require_clean_locked_worktree(environment: str) -> None:
    """Keep commit-derived image identities honest in staging and production."""

    if environment not in LOCKED_ENVIRONMENTS:
        return
    status = capture(["git", "status", "--porcelain=v1", "--untracked-files=all"])
    if status:
        # Do not print the status: untracked or renamed paths can themselves
        # contain sensitive/operator-controlled text.
        raise SystemExit(
            "A clean Git worktree is required for staging/prod image builds; "
            "commit or remove local changes before deployment."
        )


def _scan_release_images(tag: str) -> None:
    """Apply the same fail-closed HIGH/CRITICAL gate as deploy-aks CI."""

    version_output = capture(["trivy", "--version"])
    if re.search(rf"(?m)^Version:\s*{re.escape(TRIVY_VERSION)}(?:\s|$)", version_output) is None:
        raise SystemExit(f"Trivy {TRIVY_VERSION} is required for reviewed image scans.")
    for image in (f"apex-orchestration-engine:{tag}", f"apex-dashboard:{tag}"):
        run(
            [
                "trivy",
                "image",
                "--scanners",
                "vuln",
                "--pkg-types",
                "os,library",
                "--severity",
                "HIGH,CRITICAL",
                "--ignore-unfixed=false",
                "--exit-code",
                "1",
                "--format",
                "table",
                image,
            ]
        )


@task("image-build", "Build the server (langgraph) + dashboard images. Args: [tag]")
def image_build(args: list[str]) -> None:
    tag = _validate_oci_tag(_arg(args, 0, DEFAULT_TAG))
    _build_server_image(f"apex-orchestration-engine:{tag}", tag)
    run(["docker", "build", "-f", "apps/dashboard/Dockerfile", "-t", f"apex-dashboard:{tag}", "."])


@task("image-push", "Tag + push both images to a registry. Args: <registry> [tag]")
def image_push(args: list[str]) -> None:
    if not args:
        raise SystemExit("usage: image-push <registry> [tag]")
    registry = _validate_registry(args[0].rstrip("/"))
    tag = _validate_oci_tag(_arg(args, 1, DEFAULT_TAG))
    for name in ("apex-orchestration-engine", "apex-dashboard"):
        run(["docker", "tag", f"{name}:{tag}", f"{registry}/{name}:{tag}"])
        run(["docker", "push", f"{registry}/{name}:{tag}"])


def _pushed_image_digest(image_ref: str) -> str:
    """Read the exact locally-pushed manifest digest without re-resolving its tag."""

    raw_repo_digests = capture(
        ["docker", "image", "inspect", "--format={{json .RepoDigests}}", image_ref]
    )
    try:
        repo_digests = json.loads(raw_repo_digests)
    except json.JSONDecodeError:
        repo_digests = None
    last_slash = image_ref.rfind("/")
    last_colon = image_ref.rfind(":")
    repository = image_ref[:last_colon] if last_colon > last_slash else image_ref
    prefix = f"{repository}@"
    candidates = {
        value.removeprefix(prefix)
        for value in repo_digests or []
        if isinstance(value, str) and value.startswith(prefix)
    }
    if len(candidates) != 1:
        raise SystemExit("Docker returned an invalid pushed digest.")
    digest = candidates.pop()
    if _OCI_DIGEST.fullmatch(digest) is None:
        raise SystemExit("Docker returned an invalid pushed digest.")
    return digest


# ── compose ─────────────────────────────────────────────────────────────────


@task("compose-up", "Build + start the full local stack (infra + server + dashboard).")
def compose_up(_: list[str]) -> None:
    image = os.environ.get("APEX_IMAGE", "apex-orchestration-engine:local")
    _build_server_image(image, _image_reference_tag(image))
    run(["docker", "compose", "-f", "docker-compose.yaml", "up", "-d", "--build", "--wait"])


@task("compose-down", "Stop the full local stack.")
def compose_down(_: list[str]) -> None:
    run(["docker", "compose", "-f", "docker-compose.yaml", "down"])


@task("compose-ha-up", "Start the HA soak rig (2 replicas + nginx). Needs the license env vars.")
def compose_ha_up(_: list[str]) -> None:
    image = os.environ.get("APEX_IMAGE", "apex-orchestration-engine:local")
    _build_server_image(image, _image_reference_tag(image))
    run(
        [
            "docker",
            "compose",
            "-f",
            "deploy/compose-ha/docker-compose.ha.yaml",
            "up",
            "-d",
            "--wait",
        ]
    )


# ── kubernetes / helm ─────────────────────────────────────────────────────────


@contextmanager
def _cleanup_on_failed_helm(cleanup: Callable[[], None]) -> Iterator[None]:
    """Run compensating cleanup on command failure, interrupt, HUP, or TERM."""

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

    def interrupted(signum: int, _frame: object) -> None:
        raise KeyboardInterrupt(f"deployment interrupted by signal {signum}")

    for candidate in watched:
        signal.signal(candidate, interrupted)
    restored = False

    def restore() -> None:
        nonlocal restored
        if restored:
            return
        for candidate, handler in previous.items():
            signal.signal(candidate, handler)
        restored = True

    try:
        yield
    except BaseException:
        # A second termination signal must not interrupt compensation halfway.
        for candidate in watched:
            signal.signal(candidate, signal.SIG_IGN)
        try:
            cleanup()
        finally:
            restore()
        raise
    finally:
        restore()


def _cleanup_failed_csi_release(
    namespace: str,
    release: str,
    *,
    hook_secrets: tuple[str, ...] = (),
) -> None:
    """Best-effort but exhaustive cleanup for a supported Helm deploy path."""

    try:
        succeeded = cleanup_csi_hook_material(
            namespace=namespace,
            release=release,
            hook_secrets=hook_secrets,
        )
    except ValueError:
        # Helm rejects these identifiers before creating Kubernetes resources;
        # never reflect attacker-controlled identifier text into deployment logs.
        succeeded = False
    if not succeeded:
        print(
            "WARNING: CSI hook cleanup was incomplete; keep the deployment failed and "
            "re-run scripts/cleanup_csi_hook_material.py before retrying.",
            file=sys.stderr,
        )


@task("helm-install", "helm upgrade --install. Args: [release] [namespace] [extra helm args...]")
def helm_install(args: list[str]) -> None:
    release = _arg(args, 0, "apex")
    namespace = _arg(args, 1, "apex")
    extra = args[2:]
    helm_command = [
        "helm",
        "upgrade",
        "--install",
        release,
        CHART,
        "-n",
        namespace,
        "--create-namespace",
        *extra,
        # Hook resources are not ordinary release resources. Atomic rollback
        # invokes the chart's post-rollback CSI cleanup hook, and
        # cleanup-on-fail removes newly-created ordinary resources.
        "--atomic",
        "--cleanup-on-fail",
        "--wait",
    ]
    # The chart labels every newly synchronized hook-only Secret, so generic
    # callers do not need to disclose or parse custom Secret names here. The
    # pre-upgrade exact-name ledger removes unlabelled pre-adoption names first.
    with _cleanup_on_failed_helm(lambda: _cleanup_failed_csi_release(namespace, release)):
        run(helm_command)


# ── azure aks (turnkey) ───────────────────────────────────────────────────────


def _terraform_output(terraform_dir: str, name: str) -> str:
    return capture(["terraform", f"-chdir={terraform_dir}", "output", "-raw", name])


def _tf_output(name: str) -> str:
    return _terraform_output(TF, name)


def _validate_azure_environment(value: str) -> str:
    environment = value.strip().lower()
    if environment not in {"dev", "staging", "prod"}:
        raise SystemExit("$APEX_ENV must be dev, staging, or prod.")
    return environment


def _configure_kubernetes_version(environment: str) -> str | None:
    value = os.environ.get("APEX_KUBERNETES_VERSION") or os.environ.get("TF_VAR_kubernetes_version")
    if value is None:
        if environment in LOCKED_ENVIRONMENTS:
            raise SystemExit(
                "$APEX_KUBERNETES_VERSION is required for reproducible staging/prod plans."
            )
        return None
    if _KUBERNETES_VERSION.fullmatch(value) is None:
        raise SystemExit("$APEX_KUBERNETES_VERSION must be a reviewed 1.x minor/patch version.")
    os.environ["TF_VAR_kubernetes_version"] = value
    return value


def _saved_plan_path(terraform_dir: str, environment: str, action: str) -> Path:
    return REPO_ROOT / terraform_dir / f"apex-{environment}-{action}.tfplan"


def _saved_plan_digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(_STREAM_CHUNK_BYTES):
            digest.update(chunk)
    return digest.hexdigest()


def _remove_saved_plan(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
    except OSError:
        raise SystemExit("Unable to remove the sensitive Terraform plan file.") from None


def _saved_plan_confirmation_token(action: str, environment: str, path: Path) -> str:
    return f"{action}:{environment}:{_saved_plan_digest(path)}"


@contextmanager
def _private_umask() -> Iterator[None]:
    """Prevent Terraform from ever creating credential-bearing plans world-readable."""

    previous = os.umask(0o077)
    try:
        yield
    finally:
        os.umask(previous)


def _confirm_saved_plan(action: str, environment: str, path: Path) -> str:
    """Require approval and return the exact digest that was approved."""

    try:
        approved_digest = _saved_plan_digest(path)
    except OSError:
        raise SystemExit("Approved Terraform plan is no longer available.") from None
    return _confirm_saved_plan_digest(action, environment, approved_digest)


def _confirm_saved_plan_digest(action: str, environment: str, approved_digest: str) -> str:
    """Require operator approval for a digest already bound to a private snapshot."""

    if environment not in LOCKED_ENVIRONMENTS:
        return approved_digest
    expected = f"{action}:{environment}:{approved_digest}"
    variable = (
        "APEX_TERRAFORM_DESTROY_CONFIRM" if action == "destroy" else "APEX_TERRAFORM_APPLY_CONFIRM"
    )
    print(f"Saved plan SHA-256: {expected.rsplit(':', 1)[1]}", flush=True)
    supplied = os.environ.get(variable, "")
    if not supplied and sys.stdin.isatty():
        supplied = input(f'Type "{expected}" to approve this exact plan: ').strip()
    if supplied != expected:
        raise SystemExit(
            f"Exact saved-plan confirmation required. Set ${variable} to {expected!r}."
        )
    return approved_digest


def _assert_saved_plan_unchanged(path: Path, approved_digest: str) -> None:
    """Refuse a saved plan replaced after policy review/operator approval."""

    try:
        current_digest = _saved_plan_digest(path)
    except OSError:
        # The OS exception retains the local plan path and platform details;
        # neither is needed at the operator-facing boundary.
        raise SystemExit("Approved Terraform plan is no longer available.") from None
    if current_digest != approved_digest:
        raise SystemExit("Approved Terraform plan changed before apply; refusing deployment.")


def _inherited_descriptor_path(descriptor: int) -> str:
    for directory in (Path("/proc/self/fd"), Path("/dev/fd")):
        if directory.is_dir():
            return str(directory / str(descriptor))
    raise SystemExit("This platform cannot safely apply a verified Terraform plan snapshot.")


@contextmanager
def _saved_plan_snapshot(path: Path) -> Iterator[tuple[str, int, str]]:
    """Yield an inherited read-only snapshot and the digest of its exact bytes."""

    try:
        with immutable_plan_snapshot(
            path,
            max_bytes=MAX_SAVED_PLAN_BYTES,
            chunk_bytes=_STREAM_CHUNK_BYTES,
        ) as snapshot:
            yield snapshot
    except ImmutablePlanSnapshotError as exc:
        raise SystemExit(str(exc)) from None


@contextmanager
def _verified_saved_plan_snapshot(
    path: Path,
    approved_digest: str,
) -> Iterator[tuple[str, int]]:
    """Yield an anonymous snapshot only when it matches the approved digest."""

    with _saved_plan_snapshot(path) as (snapshot_path, descriptor, digest):
        if digest != approved_digest:
            raise SystemExit("Approved Terraform plan changed before apply; refusing deployment.")
        yield snapshot_path, descriptor


def _apply_verified_saved_plan(
    terraform_dir: str,
    path: Path,
    approved_digest: str,
) -> None:
    with _verified_saved_plan_snapshot(path, approved_digest) as (snapshot_path, descriptor):
        run(
            ["terraform", f"-chdir={terraform_dir}", "apply", "-input=false", snapshot_path],
            pass_fds=(descriptor,),
        )


def _rewind_plan_snapshot(descriptor: int) -> None:
    try:
        os.lseek(descriptor, 0, os.SEEK_SET)
    except OSError:
        raise SystemExit("Unable to rewind the private Terraform plan snapshot.") from None


def _enforce_saved_plan_policy(
    terraform_dir: str,
    environment: str,
    plan_name: str,
    pass_fds: tuple[int, ...] = (),
) -> None:
    payload = _capture_bounded(
        ["terraform", f"-chdir={terraform_dir}", "show", "-json", plan_name],
        max_output_bytes=MAX_PLAN_JSON_BYTES,
        output_description="Terraform saved-plan JSON",
        pass_fds=pass_fds,
    )
    try:
        plan = json.loads(payload)
    except (json.JSONDecodeError, RecursionError):
        raise SystemExit("Terraform emitted an invalid saved-plan document.") from None
    if not isinstance(plan, dict):
        raise SystemExit("Terraform emitted an invalid saved-plan document.")
    try:
        enforce_plan_policy(plan, environment)
    except ValueError as exc:
        raise SystemExit(str(exc)) from None


def _plan_and_apply(
    terraform_dir: str,
    environment: str,
    *,
    plan_arguments: list[str],
) -> None:
    plan_name = f"apex-{environment}-apply.tfplan"
    plan_path = _saved_plan_path(terraform_dir, environment, "apply")
    try:
        # A pre-existing plan can retain a permissive mode when Terraform
        # truncates it. Remove it before planning so the private umask controls
        # the mode from the first credential-bearing byte written.
        _remove_saved_plan(plan_path)
        with _private_umask():
            run(
                [
                    "terraform",
                    f"-chdir={terraform_dir}",
                    "plan",
                    "-input=false",
                    f"-out={plan_name}",
                    *plan_arguments,
                ]
            )
        # Policy review, operator approval, and apply all consume the same
        # anonymous snapshot. A replaced workspace path can therefore neither
        # bypass policy nor swap bytes after approval.
        with _saved_plan_snapshot(plan_path) as (snapshot_path, descriptor, digest):
            _remove_saved_plan(plan_path)
            _enforce_saved_plan_policy(
                terraform_dir,
                environment,
                snapshot_path,
                (descriptor,),
            )
            # /dev/fd may duplicate the inherited file description rather than
            # opening an independent one. Rewind after `terraform show` so the
            # subsequent apply always begins at byte zero on every POSIX host.
            _rewind_plan_snapshot(descriptor)
            _confirm_saved_plan_digest("apply", environment, digest)
            run(
                [
                    "terraform",
                    f"-chdir={terraform_dir}",
                    "apply",
                    "-input=false",
                    snapshot_path,
                ],
                pass_fds=(descriptor,),
            )
    finally:
        # Binary plans can contain sensitive values. Do not retain one when a
        # policy check, confirmation, or apply fails partway through the flow.
        _remove_saved_plan(plan_path)


def _helm_fullname(release: str, chart: str = "apex-orchestration-engine") -> str:
    """Mirror the chart's fullname helper, including Kubernetes truncation."""
    value = release if chart in release else f"{release}-{chart}"
    return value[:63].removesuffix("-")


def _helm_suffixed_name(base: str, suffix: str) -> str:
    """Mirror apex.suffixedName, reserving space for the complete suffix."""
    suffix = suffix.removeprefix("-")
    if not 1 <= len(suffix) <= 61:
        raise ValueError("Helm name suffix must be between 1 and 61 characters")
    max_base_length = 62 - len(suffix)
    truncated_base = base[:max_base_length].removesuffix("-")
    return f"{truncated_base}-{suffix}"


def _validate_dns_name(value: str, *, label: str, max_length: int = 253) -> str:
    normalized = value.strip().lower()
    parts = normalized.split(".")
    if (
        not normalized
        or len(normalized) > max_length
        or any(not part or len(part) > 63 or _DNS_LABEL.fullmatch(part) is None for part in parts)
    ):
        raise SystemExit(
            f"${label} must be a lowercase DNS name no longer than {max_length} chars."
        )
    return normalized


def _validate_dns_label(value: str, *, label: str, max_length: int = 63) -> str:
    normalized = _validate_dns_name(value, label=label, max_length=max_length)
    if "." in normalized:
        raise SystemExit(f"${label} must be one lowercase DNS label, not a dotted name.")
    return normalized


def _validate_oci_tag(value: str) -> str:
    if _OCI_TAG.fullmatch(value) is None:
        raise SystemExit("$APEX_TAG must be a valid OCI image tag (1-128 safe characters).")
    return value


def _validate_registry(value: str) -> str:
    """Accept the hostname[:port] form supported by the image-push task."""

    hostname, separator, raw_port = value.rpartition(":")
    if separator and raw_port.isdecimal() and ":" not in hostname:
        if not 1 <= int(raw_port) <= 65_535:
            raise SystemExit("Registry must be a valid lowercase DNS name with an optional port.")
        candidate = hostname
    elif separator:
        raise SystemExit("Registry must be a valid lowercase DNS name with an optional port.")
    else:
        candidate = value
    normalized = _validate_dns_name(candidate, label="registry")
    if normalized != candidate:
        raise SystemExit("Registry must be a valid lowercase DNS name with an optional port.")
    return value


def _apply_manifest(manifest: str, description: str) -> None:
    print(f"+ kubectl apply -f - <{description}>", flush=True)
    subprocess.run(
        ["kubectl", "apply", "-f", "-"],
        cwd=REPO_ROOT,
        check=True,
        input=manifest,
        text=True,
    )


def _ensure_tls_secret(namespace: str, secret_name: str, hostname: str) -> None:
    """Create/update a TLS Secret from local files, or verify an existing one."""
    cert_file = os.environ.get("APEX_TLS_CERTIFICATE_FILE")
    key_file = os.environ.get("APEX_TLS_PRIVATE_KEY_FILE")
    if bool(cert_file) != bool(key_file):
        raise SystemExit(
            "$APEX_TLS_CERTIFICATE_FILE and $APEX_TLS_PRIVATE_KEY_FILE must be set together."
        )
    if cert_file and key_file:
        cert_path = Path(cert_file).expanduser().resolve()
        key_path = Path(key_file).expanduser().resolve()
        if not cert_path.is_file() or not key_path.is_file():
            raise SystemExit("TLS certificate/private-key file does not exist.")
        run(["openssl", "x509", "-in", str(cert_path), "-noout", "-checkend", "86400"])
        run(
            [
                "openssl",
                "x509",
                "-in",
                str(cert_path),
                "-noout",
                "-checkhost",
                hostname,
            ]
        )
        # kubectl parses the PEM pair and rejects a certificate/key mismatch.
        manifest = capture(
            [
                "kubectl",
                "-n",
                namespace,
                "create",
                "secret",
                "tls",
                secret_name,
                f"--cert={cert_path}",
                f"--key={key_path}",
                "--dry-run=client",
                "-o",
                "json",
            ]
        )
        _apply_manifest(manifest, "tls-secret")
    else:
        run(["kubectl", "-n", namespace, "get", "secret", secret_name])
    secret_type = capture(
        [
            "kubectl",
            "-n",
            namespace,
            "get",
            "secret",
            secret_name,
            "-o",
            "jsonpath={.type}",
        ]
    )
    if secret_type != "kubernetes.io/tls":
        raise SystemExit(f"Secret {namespace}/{secret_name} is not type kubernetes.io/tls.")


def _verify_artifact_backup(namespace: str, storage_account: str, environment: str) -> None:
    """Run the CronJob now and prove its sentinel reached Azure Blob."""
    # A unique key prevents a stale object from an earlier successful deploy
    # from masking a backup failure in this deploy.
    smoke_object = f".apex-backup-smoke/{environment}/{time.time_ns()}"
    run(
        [
            "kubectl",
            "-n",
            namespace,
            "exec",
            "deployment/apex-minio",
            "--",
            "env",
            f"SMOKE_OBJECT={smoke_object}",
            "sh",
            "-ec",
            'mc alias set local http://127.0.0.1:9000 "$MINIO_ROOT_USER" '
            '"$MINIO_ROOT_PASSWORD" >/dev/null; '
            "mc mb --ignore-existing local/apex-artifacts >/dev/null; "
            'printf "%s\\n" "APEX backup deployment smoke" '
            '| mc pipe "local/apex-artifacts/$SMOKE_OBJECT"',
        ]
    )
    job = "apex-minio-backup-smoke"
    run(["kubectl", "-n", namespace, "delete", "job", job, "--ignore-not-found", "--wait=true"])
    run(
        [
            "kubectl",
            "-n",
            namespace,
            "create",
            "job",
            "--from=cronjob/apex-minio-backup",
            job,
        ]
    )
    wait_result = subprocess.run(
        [
            "kubectl",
            "-n",
            namespace,
            "wait",
            "--for=condition=complete",
            f"job/{job}",
            "--timeout=15m",
        ],
        cwd=REPO_ROOT,
        check=False,
    )
    if wait_result.returncode:
        run(["kubectl", "-n", namespace, "logs", f"job/{job}", "--all-containers=true"])
        raise SystemExit(wait_result.returncode)
    run(["kubectl", "-n", namespace, "logs", f"job/{job}", "--all-containers=true"])
    for _ in range(30):
        exists = _capture_bounded(
            [
                "az",
                "storage",
                "blob",
                "exists",
                "--account-name",
                storage_account,
                "--container-name",
                "apex-artifacts-backup",
                "--name",
                smoke_object,
                "--auth-mode",
                "login",
                "--query",
                "exists",
                "-o",
                "tsv",
            ],
            max_output_bytes=16 * 1024,
            output_description="Azure blob probe output",
            allow_failure=True,
        )
        if exists == "true":
            # Remove the source sentinel after proving the copy. The next sync
            # removes its current backup object while soft-delete retains the
            # recovery evidence only for the configured bounded window.
            run(
                [
                    "kubectl",
                    "-n",
                    namespace,
                    "exec",
                    "deployment/apex-minio",
                    "--",
                    "env",
                    f"SMOKE_OBJECT={smoke_object}",
                    "sh",
                    "-ec",
                    'mc alias set local http://127.0.0.1:9000 "$MINIO_ROOT_USER" '
                    '"$MINIO_ROOT_PASSWORD" >/dev/null; '
                    'mc rm "local/apex-artifacts/$SMOKE_OBJECT"',
                ]
            )
            run(["kubectl", "-n", namespace, "delete", "job", job, "--wait=false"])
            return
        time.sleep(10)
    raise SystemExit("Backup job completed but sentinel is absent from Azure Blob.")


@task("aks-up", "Provision Azure + deploy. Needs APEX_ENV (dev|staging|prod) and `az login`.")
def aks_up(_: list[str]) -> None:
    env = _validate_azure_environment(require_env("APEX_ENV"))
    _require_clean_locked_worktree(env)
    _configure_kubernetes_version(env)
    hostname = _validate_dns_name(require_env("APEX_HOSTNAME"), label="APEX_HOSTNAME")
    tls_secret = _validate_dns_name(require_env("APEX_TLS_SECRET"), label="APEX_TLS_SECRET")
    tag = _validate_oci_tag(
        os.environ.get("APEX_TAG") or f"sha-{capture(['git', 'rev-parse', '--short=12', 'HEAD'])}"
    )
    namespace = _validate_dns_label(
        os.environ.get("APEX_NAMESPACE", "apex"),
        label="APEX_NAMESPACE",
        max_length=63,
    )
    release = _validate_dns_label(
        os.environ.get("APEX_RELEASE", "apex"),
        label="APEX_RELEASE",
        max_length=53,
    )

    # 1) Provision (the backend must already be initialized — see deploy/terraform/README.md).
    fullname = _helm_fullname(release)
    backend_deployment = f"deployment/{fullname}"
    dashboard_deployment = f"deployment/{_helm_suffixed_name(fullname, 'dashboard')}"
    _plan_and_apply(
        TF,
        env,
        plan_arguments=[
            f"-var-file=env/{env}.tfvars",
            f"-var=workload_namespace={namespace}",
        ],
    )
    acr = _tf_output("acr_login_server")
    aks = _tf_output("aks_cluster_name")
    rg = _tf_output("resource_group")
    kv = _tf_output("key_vault_name")
    hook_kv = _tf_output("hook_key_vault_name")
    tenant = _tf_output("tenant_id")
    client_id = _tf_output("workload_identity_client_id")
    hook_client_id = _tf_output("hook_identity_client_id")
    backup_client_id = _tf_output("backup_identity_client_id")
    backup_principal_id = _tf_output("backup_identity_principal_id")
    service_account = _tf_output("workload_service_account")
    hook_service_account = _tf_output("workload_hook_service_account")
    backup_service_account = _tf_output("backup_service_account")
    location = _tf_output("location")
    deployer_object_id = _tf_output("deployer_object_id")
    database_credential_generation = _tf_output("database_credential_generation")

    # Backups have an independent state key and protected resource group. The
    # live stack can now be destroyed without owning its recovery copy.
    _plan_and_apply(
        BACKUP_TF,
        env,
        plan_arguments=[
            f"-var=environment={env}",
            f"-var=location={location}",
            f"-var=backup_identity_principal_id={backup_principal_id}",
            f"-var=deployer_object_id={deployer_object_id}",
        ],
    )
    storage_account = _terraform_output(BACKUP_TF, "storage_account_name")

    # 2) Build + push images to ACR (langgraph build has no Dockerfile -> can't use `az acr build`).
    image_build([tag])
    _scan_release_images(tag)
    run(["az", "acr", "login", "--name", acr.split(".")[0]])
    image_push([acr, tag])
    backend_digest = _pushed_image_digest(f"{acr}/apex-orchestration-engine:{tag}")
    dashboard_digest = _pushed_image_digest(f"{acr}/apex-dashboard:{tag}")

    # 3) Cluster credentials.
    run(
        [
            "az",
            "aks",
            "get-credentials",
            "--resource-group",
            rg,
            "--name",
            aks,
            "--overwrite-existing",
            "--format",
            "exec",
        ]
    )
    run(["kubelogin", "convert-kubeconfig", "-l", "azurecli"])
    for attempt in range(30):
        access = _capture_bounded(
            ["kubectl", "auth", "can-i", "*", "*", "--all-namespaces"],
            max_output_bytes=16 * 1024,
            output_description="Kubernetes authorization probe output",
            allow_failure=True,
        )
        if access == "yes":
            break
        print(f"Waiting for AKS Azure RBAC propagation ({attempt + 1}/30)", flush=True)
        time.sleep(10)
    else:
        raise SystemExit("Azure deployer did not receive AKS RBAC cluster-admin access.")

    # 4) Prepare a fresh namespace and wait for the Terraform-enabled CSI API.
    # The chart's ordered pre-install hooks mount Key Vault and synthesize Secrets
    # before migration/bootstrap consume them.
    namespace_manifest = capture(
        ["kubectl", "create", "namespace", namespace, "--dry-run=client", "-o", "json"]
    )
    _apply_manifest(namespace_manifest, "namespace-manifest")
    run(
        [
            "kubectl",
            "wait",
            "--for=condition=Established",
            "crd/secretproviderclasses.secrets-store.csi.x-k8s.io",
            "--timeout=5m",
        ]
    )
    _ensure_tls_secret(namespace, tls_secret, hostname)

    # 5) MinIO may remain Pending until the chart's CSI sync hook creates its Secret.
    run(["kubectl", "apply", "-n", namespace, "-f", "deploy/azure/k8s/minio/minio.yaml"])

    # 6) Deploy. Migration + bootstrap run as pre-upgrade hooks (migrate-then-roll).
    helm_command = [
        "helm",
        "upgrade",
        "--install",
        release,
        CHART,
        "-n",
        namespace,
        "--create-namespace",
        "-f",
        "deploy/azure/helm/values-azure.yaml",
        "--set",
        f"image.repository={acr}/apex-orchestration-engine",
        "--set",
        f"image.tag={tag}",
        "--set-string",
        f"image.digest={backend_digest}",
        "--set",
        f"dashboard.image.repository={acr}/apex-dashboard",
        "--set",
        f"dashboard.image.tag={tag}",
        "--set-string",
        f"dashboard.image.digest={dashboard_digest}",
        "--set-string",
        f"dashboard.backendUpstream=http://{fullname}:80",
        "--set-string",
        "bootstrap.document.connections[0].options.endpoint=apex-minio:9000",
        "--set-string",
        f"ingress.hosts[0].host={hostname}",
        "--set-string",
        f"ingress.tls[0].hosts[0]={hostname}",
        "--set-string",
        f"ingress.tls[0].secretName={tls_secret}",
        "--set-string",
        f'apexSettings.APEX_CORS_ORIGINS=["https://{hostname}"]',
        "--set",
        f"secretBackend.csi.keyvaultName={kv}",
        "--set",
        f"secretBackend.csi.hookKeyvaultName={hook_kv}",
        "--set",
        f"secretBackend.csi.tenantId={tenant}",
        "--set",
        f"workloadIdentity.clientId={client_id}",
        "--set",
        f"hookWorkloadIdentity.clientId={hook_client_id}",
        "--set",
        f"backupWorkloadIdentity.clientId={backup_client_id}",
        "--set",
        f"serviceAccount.name={service_account}",
        "--set",
        f"hookServiceAccountName={hook_service_account}",
        "--set",
        f"backupWorkloadIdentity.serviceAccountName={backup_service_account}",
        "--set-string",
        f"databaseRoleProvisioning.credentialGeneration={database_credential_generation}",
        "--atomic",
        "--cleanup-on-fail",
        "--wait",
        "--timeout",
        "15m",
    ]
    with _cleanup_on_failed_helm(
        lambda: _cleanup_failed_csi_release(
            namespace,
            release,
            hook_secrets=_AZURE_CSI_HOOK_SECRETS,
        )
    ):
        run(helm_command)
    # A reused image tag or rotated Secret does not change the pod template.
    run(
        [
            "kubectl",
            "-n",
            namespace,
            "rollout",
            "restart",
            backend_deployment,
            dashboard_deployment,
        ]
    )
    backup_config = capture(
        [
            "kubectl",
            "-n",
            namespace,
            "create",
            "configmap",
            "apex-minio-backup-config",
            f"--from-literal=AZURE_STORAGE_ACCOUNT={storage_account}",
            "--dry-run=client",
            "-o",
            "json",
        ]
    )
    _apply_manifest(backup_config, "backup-config")
    run(
        [
            "kubectl",
            "-n",
            namespace,
            "rollout",
            "status",
            backend_deployment,
            "--timeout=15m",
        ]
    )
    run(
        [
            "kubectl",
            "-n",
            namespace,
            "rollout",
            "status",
            dashboard_deployment,
            "--timeout=15m",
        ]
    )
    # Enforce MinIO isolation only after the new backend capability label is
    # Ready; applying it before Helm would cut off legacy unlabeled pods during
    # the first adoption upgrade.
    run(
        [
            "kubectl",
            "apply",
            "-n",
            namespace,
            "-f",
            "deploy/azure/k8s/minio/networkpolicy.yaml",
        ]
    )
    run(["kubectl", "-n", namespace, "rollout", "restart", "deployment/apex-minio"])
    run(
        [
            "kubectl",
            "-n",
            namespace,
            "rollout",
            "status",
            "deployment/apex-minio",
            "--timeout=5m",
        ]
    )
    run(
        [
            "kubectl",
            "apply",
            "-n",
            namespace,
            "-f",
            "deploy/azure/k8s/minio/backup-cronjob.yaml",
        ]
    )
    _verify_artifact_backup(namespace, storage_account, env)
    run(["helm", "test", release, "-n", namespace])
    run(
        [
            "kubectl",
            "-n",
            namespace,
            "wait",
            "--for=jsonpath={.status.loadBalancer.ingress}",
            f"ingress/{fullname}",
            "--timeout=10m",
        ]
    )
    ingress_class = capture(
        [
            "kubectl",
            "-n",
            namespace,
            "get",
            f"ingress/{fullname}",
            "-o",
            "jsonpath={.spec.ingressClassName}",
        ]
    )
    if ingress_class != "webapprouting.kubernetes.azure.com":
        raise SystemExit("AKS ingress did not use the required ingress class.")
    run(
        [
            "curl",
            "--fail",
            "--silent",
            "--show-error",
            "--retry",
            "12",
            "--retry-all-errors",
            "--connect-timeout",
            "10",
            "--max-time",
            "30",
            f"https://{hostname}/ok",
        ]
    )
    print("aks-up complete. Internal and public HTTPS smoke checks passed.")


@task("aks-down", "Destroy the Azure stack for APEX_ENV. Irreversible.")
def aks_down(_: list[str]) -> None:
    env = _validate_azure_environment(require_env("APEX_ENV"))
    _configure_kubernetes_version(env)
    plan_name = f"apex-{env}-destroy.tfplan"
    plan_path = _saved_plan_path(TF, env, "destroy")
    try:
        _remove_saved_plan(plan_path)
        with _private_umask():
            run(
                [
                    "terraform",
                    f"-chdir={TF}",
                    "plan",
                    "-destroy",
                    "-input=false",
                    f"-out={plan_name}",
                    f"-var-file=env/{env}.tfvars",
                ]
            )
        # Destruction is an explicit break-glass task, not an ordinary deploy. The
        # confirmation includes the full binary-plan digest and target environment;
        # the separately stateful backup account is outside this plan and survives.
        with _saved_plan_snapshot(plan_path) as (snapshot_path, descriptor, digest):
            _remove_saved_plan(plan_path)
            _confirm_saved_plan_digest("destroy", env, digest)
            run(
                ["terraform", f"-chdir={TF}", "apply", "-input=false", snapshot_path],
                pass_fds=(descriptor,),
            )
    finally:
        _remove_saved_plan(plan_path)


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="APEX deploy task runner")
    parser.add_argument("task", nargs="?", choices=sorted(TASKS))
    parser.add_argument("extra", nargs=argparse.REMAINDER)
    return parser.parse_args(argv)


def print_tasks() -> None:
    print("Available tasks:")
    for name in sorted(TASKS):
        help_text, _ = TASKS[name]
        print(f"  {name:<14} {help_text}")


def main(argv: list[str]) -> int:
    args = parse_args(argv)
    if args.task is None:
        print_tasks()
        return 0
    extra = args.extra[1:] if args.extra[:1] == ["--"] else args.extra
    TASKS[args.task][1](extra)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

"""Tests for deployment helpers mirrored outside the Helm chart."""

import fcntl
import hashlib
import os
import sys
import tempfile
from collections.abc import Callable
from pathlib import Path

import pytest
from scripts import deploy
from scripts.deploy import (
    TRIVY_VERSION,
    _assert_saved_plan_unchanged,
    _configure_kubernetes_version,
    _confirm_saved_plan,
    _display_command,
    _ensure_tls_secret,
    _helm_fullname,
    _helm_suffixed_name,
    _pushed_image_digest,
    _require_clean_locked_worktree,
    _saved_plan_confirmation_token,
    _scan_release_images,
    _validate_azure_environment,
    _validate_dns_label,
    _validate_dns_name,
    _validate_oci_tag,
)


def test_run_redacts_display_only_without_changing_subprocess_argv(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    canaries = {
        "helm": "helm-password-canary",
        "token": "terraform-token-canary",
        "userinfo": "registry-userinfo-canary",
        "signature": "signed-query-canary",
        "key_path": "/private/tls-key-canary.pem",
    }
    command = [
        "helm",
        "upgrade",
        "--set-string",
        f"database.password={canaries['helm']}",
        f"-var=api_token={canaries['token']}",
        f"https://user:{canaries['userinfo']}@registry.example/apex",
        f"https://blob.example/object?X-Amz-Signature={canaries['signature']}&part=1",
        f"--key={canaries['key_path']}",
        "line\nforged\x1b[31m",
    ]
    executed: list[list[str]] = []

    def fake_subprocess_run(argv: list[str], **_kwargs: object) -> object:
        executed.append(argv)
        return object()

    monkeypatch.setattr(deploy.subprocess, "run", fake_subprocess_run)

    deploy.run(command)

    output = capsys.readouterr().out
    assert executed == [command]
    assert all(canary not in output for canary in canaries.values())
    assert output.count("\n") == 1
    assert "\\n" in output
    assert "\x1b" not in output
    assert "[REDACTED]" in output


@pytest.mark.parametrize(
    "command",
    [
        ["helm", "upgrade", "--set", "nested.clientSecret=secret-canary,safe=value"],
        ["terraform", "plan", "-backend-config=access_key=secret-canary"],
        ["kubectl", "create", "secret", "tls", "apex", "--key", "secret-canary"],
        ["docker", "login", "--password=secret-canary", "registry.example"],
        ["az", "login", "--client-secret", "secret-canary"],
        ["client", "https://example.test/path?password=secret-canary&safe=value"],
    ],
)
def test_display_command_redacts_split_and_inline_secret_options(command: list[str]) -> None:
    rendered = _display_command(command)

    assert "secret-canary" not in rendered
    assert "[REDACTED]" in rendered


def test_capture_failure_does_not_echo_raw_argv_or_captured_output(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    argv_canary = "capture-argv-secret-canary"
    output_canary = "capture-output-secret-canary"
    monkeypatch.setenv("APEX_CAPTURE_OUTPUT_CANARY", output_canary)
    command = [
        sys.executable,
        "-c",
        "import os,sys; print(os.environ['APEX_CAPTURE_OUTPUT_CANARY']); sys.exit(9)",
        "--key",
        argv_canary,
    ]

    with pytest.raises(SystemExit) as caught:
        deploy.capture(command)

    rendered = str(caught.value)
    assert argv_canary not in rendered
    assert output_canary not in rendered
    assert "exit code 9" in rendered
    assert "[REDACTED]" in rendered


def test_capture_applies_the_default_output_bound() -> None:
    with pytest.raises(SystemExit, match="Command output exceeds the size limit"):
        deploy.capture([sys.executable, "-c", f"print('x' * ({deploy.MAX_CAPTURE_BYTES} + 1))"])


def test_bounded_capture_stops_oversized_command_output() -> None:
    canary = "oversized-output-canary"
    with pytest.raises(SystemExit) as caught:
        deploy._capture_bounded(
            [
                sys.executable,
                "-c",
                f"import sys; sys.stdout.write({canary!r} * 1024)",
            ],
            max_output_bytes=128,
            output_description="Terraform saved-plan JSON",
        )

    assert str(caught.value) == "Terraform saved-plan JSON exceeds the size limit."
    assert canary not in str(caught.value)


def test_bounded_capture_inherits_anonymous_plan_descriptor() -> None:
    with tempfile.TemporaryFile(mode="w+b") as snapshot:
        snapshot.write(b"exact-anonymous-plan")
        snapshot.seek(0)
        descriptor = snapshot.fileno()
        descriptor_path = deploy._inherited_descriptor_path(descriptor)

        captured = deploy._capture_bounded(
            [
                sys.executable,
                "-c",
                "import pathlib,sys; print(pathlib.Path(sys.argv[1]).read_text())",
                descriptor_path,
            ],
            max_output_bytes=1_024,
            output_description="Plan snapshot",
            pass_fds=(descriptor,),
        )

    assert captured == "exact-anonymous-plan"


def test_saved_plan_policy_translates_malformed_json_without_reflection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    canary = "malformed-plan-output-canary"
    monkeypatch.setattr(deploy, "_capture_bounded", lambda *_, **__: canary)

    with pytest.raises(SystemExit) as caught:
        deploy._enforce_saved_plan_policy("deploy/terraform", "prod", "plan.tfplan")

    assert str(caught.value) == "Terraform emitted an invalid saved-plan document."
    assert caught.value.__cause__ is None
    assert canary not in str(caught.value)


def test_locked_image_build_requires_a_clean_worktree_without_echoing_paths(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    commands: list[list[str]] = []

    def dirty(command: list[str]) -> str:
        commands.append(command)
        return "?? credential-bearing-untracked-name"

    monkeypatch.setattr(deploy, "capture", dirty)

    with pytest.raises(SystemExit) as caught:
        _require_clean_locked_worktree("prod")

    assert commands == [["git", "status", "--porcelain=v1", "--untracked-files=all"]]
    assert "credential-bearing" not in str(caught.value)


def test_dev_image_build_allows_a_dirty_worktree_without_inspection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        deploy,
        "capture",
        lambda _command: pytest.fail("dev must not inspect the worktree"),
    )

    _require_clean_locked_worktree("dev")


def test_local_aks_scans_both_images_before_push_with_the_reviewed_trivy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    commands: list[list[str]] = []
    monkeypatch.setattr(deploy, "capture", lambda command: f"Version: {TRIVY_VERSION}")
    monkeypatch.setattr(deploy, "run", commands.append)

    _scan_release_images("sha-reviewed")

    assert len(commands) == 2
    assert {command[-1] for command in commands} == {
        "apex-orchestration-engine:sha-reviewed",
        "apex-dashboard:sha-reviewed",
    }
    for command in commands:
        assert command[:-1] == [
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
        ]


def test_local_aks_rejects_an_unreviewed_trivy_version(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(deploy, "capture", lambda _command: "Version: 0.71.0")
    monkeypatch.setattr(
        deploy,
        "run",
        lambda _command: pytest.fail("images must not be scanned with an unreviewed binary"),
    )

    with pytest.raises(SystemExit, match=TRIVY_VERSION):
        _scan_release_images("sha-reviewed")


def test_missing_approved_plan_does_not_retain_os_exception_details(tmp_path: Path) -> None:
    missing = tmp_path / "credential-bearing-plan-name.tfplan"

    with pytest.raises(SystemExit) as caught:
        _assert_saved_plan_unchanged(missing, "a" * 64)

    assert caught.value.__cause__ is None
    assert str(missing) not in repr(caught.value)


@pytest.mark.parametrize(
    "suffix",
    ["dashboard", "settings", "bootstrap", "migrate", "csi-sync", "hooks", "inventory"],
)
def test_helm_suffixed_name_reserves_the_complete_suffix(suffix: str) -> None:
    release = "this-is-a-very-long-apex-production-release-name"
    fullname = _helm_fullname(release)

    derived = _helm_suffixed_name(fullname, suffix)

    assert len(fullname) == 63
    assert len(derived) <= 63
    assert derived.endswith(f"-{suffix}")
    assert derived != fullname


@pytest.mark.parametrize("suffix", ["", "-", "x" * 62])
def test_helm_suffixed_name_rejects_invalid_suffixes(suffix: str) -> None:
    with pytest.raises(ValueError, match="between 1 and 61"):
        _helm_suffixed_name("apex", suffix)


@pytest.mark.parametrize(
    ("task", "compose_file"),
    [
        (deploy.compose_up, "docker-compose.yaml"),
        (deploy.compose_ha_up, "deploy/compose-ha/docker-compose.ha.yaml"),
    ],
)
def test_compose_tasks_build_the_configured_server_image_first(
    monkeypatch: pytest.MonkeyPatch,
    task: Callable[[list[str]], None],
    compose_file: str,
) -> None:
    commands: list[list[str]] = []
    monkeypatch.setenv("APEX_IMAGE", "registry.example/apex:test")
    monkeypatch.setattr(deploy, "run", commands.append)

    task([])

    assert commands[0] == [
        "uv",
        "run",
        "langgraph",
        "build",
        "-t",
        "registry.example/apex:test",
        "--build-arg",
        "APEX_BUILD_VERSION=test",
    ]
    assert commands[1][:4] == ["docker", "compose", "-f", compose_file]


def test_tls_secret_requires_certificate_and_key_together(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("APEX_TLS_CERTIFICATE_FILE", "/tmp/tls.crt")
    monkeypatch.delenv("APEX_TLS_PRIVATE_KEY_FILE", raising=False)

    with pytest.raises(SystemExit, match="must be set together"):
        _ensure_tls_secret("apex", "apex-tls", "apex.example.com")


def test_tls_secret_can_reuse_an_existing_typed_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    commands: list[list[str]] = []
    monkeypatch.delenv("APEX_TLS_CERTIFICATE_FILE", raising=False)
    monkeypatch.delenv("APEX_TLS_PRIVATE_KEY_FILE", raising=False)
    monkeypatch.setattr(deploy, "run", commands.append)
    monkeypatch.setattr(deploy, "capture", lambda _: "kubernetes.io/tls")

    _ensure_tls_secret("apex", "apex-tls", "apex.example.com")

    assert commands == [["kubectl", "-n", "apex", "get", "secret", "apex-tls"]]


def test_tls_secret_files_are_validated_and_applied(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    cert = tmp_path / "tls.crt"
    key = tmp_path / "tls.key"
    cert.write_text("certificate")
    key.write_text("private-key")
    commands: list[list[str]] = []
    applied: list[tuple[str, str]] = []
    monkeypatch.setenv("APEX_TLS_CERTIFICATE_FILE", str(cert))
    monkeypatch.setenv("APEX_TLS_PRIVATE_KEY_FILE", str(key))
    monkeypatch.setattr(deploy, "run", commands.append)

    def fake_capture(command: list[str]) -> str:
        if "jsonpath={.type}" in command:
            return "kubernetes.io/tls"
        return '{"kind":"Secret"}'

    monkeypatch.setattr(deploy, "capture", fake_capture)
    monkeypatch.setattr(
        deploy,
        "_apply_manifest",
        lambda manifest, description: applied.append((manifest, description)),
    )

    _ensure_tls_secret("apex", "apex-tls", "apex.example.com")

    assert commands[0][-2:] == ["-checkend", "86400"]
    assert commands[1][-2:] == ["-checkhost", "apex.example.com"]
    assert applied == [('{"kind":"Secret"}', "tls-secret")]


@pytest.mark.parametrize("environment", ["dev", "staging", "prod"])
def test_azure_environment_is_strictly_validated(environment: str) -> None:
    assert _validate_azure_environment(environment.upper()) == environment


def test_azure_environment_rejects_arbitrary_tfvars() -> None:
    with pytest.raises(SystemExit, match="dev, staging, or prod"):
        _validate_azure_environment("../../prod")


def test_locked_environment_requires_and_exports_reviewed_kubernetes_version(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("APEX_KUBERNETES_VERSION", raising=False)
    monkeypatch.delenv("TF_VAR_kubernetes_version", raising=False)
    with pytest.raises(SystemExit, match="required"):
        _configure_kubernetes_version("prod")

    monkeypatch.setenv("APEX_KUBERNETES_VERSION", "1.32.4")
    assert _configure_kubernetes_version("prod") == "1.32.4"
    assert deploy.os.environ["TF_VAR_kubernetes_version"] == "1.32.4"


def test_kubernetes_version_rejects_terraform_or_helm_injection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("APEX_KUBERNETES_VERSION", "1.32,-var=unsafe")
    with pytest.raises(SystemExit, match="reviewed 1.x"):
        _configure_kubernetes_version("staging")


def test_pushed_image_digest_is_validated_without_resolving_mutable_tag(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    digest = f"sha256:{'a' * 64}"
    commands: list[list[str]] = []

    def fake_capture(command: list[str]) -> str:
        commands.append(command)
        return '["unrelated.example/apex@sha256:' + "b" * 64 + f'","apex.azurecr.io/apex@{digest}"]'

    monkeypatch.setattr(deploy, "capture", fake_capture)

    image_ref = "apex.azurecr.io/apex:sha-123"
    assert _pushed_image_digest(image_ref) == digest
    assert commands == [["docker", "image", "inspect", "--format={{json .RepoDigests}}", image_ref]]

    monkeypatch.setattr(deploy, "capture", lambda _: "sha256:not-a-digest")
    with pytest.raises(SystemExit, match="invalid pushed digest"):
        _pushed_image_digest(image_ref)


def test_pushed_image_digest_requires_the_requested_repository(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        deploy,
        "capture",
        lambda _: '["other.example/apex@sha256:' + "a" * 64 + '"]',
    )

    with pytest.raises(SystemExit, match="invalid pushed digest"):
        _pushed_image_digest("apex.azurecr.io/apex:release")


@pytest.mark.parametrize(
    "registry",
    [
        "registry.example/repository",
        "https://registry.example",
        "user:secret@registry.example",
        "registry.example:70000",
        "registry.example\nforged",
    ],
)
def test_image_push_rejects_unsafe_registry_before_docker(
    monkeypatch: pytest.MonkeyPatch,
    registry: str,
) -> None:
    monkeypatch.setattr(
        deploy,
        "run",
        lambda _command: pytest.fail("invalid registry must not reach Docker"),
    )

    with pytest.raises(SystemExit):
        deploy.image_push([registry, "release"])


def test_pushed_digest_error_does_not_reflect_image_reference(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    canary = "image-reference-canary"
    monkeypatch.setattr(deploy, "capture", lambda _command: "not-json")

    with pytest.raises(SystemExit) as caught:
        _pushed_image_digest(f"registry.example/{canary}:release")

    assert canary not in str(caught.value)


@pytest.mark.parametrize(
    ("value", "validator"),
    [
        ("bad host,chart.value=true", lambda value: _validate_dns_name(value, label="HOST")),
        ("namespace.with.dots", lambda value: _validate_dns_label(value, label="NAMESPACE")),
        ("bad,tag", _validate_oci_tag),
    ],
)
def test_deployment_identifiers_cannot_inject_helm_values(
    value: str, validator: Callable[[str], str]
) -> None:
    with pytest.raises(SystemExit):
        validator(value)


def test_locked_apply_confirmation_is_bound_to_exact_plan(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    plan = tmp_path / "prod.tfplan"
    plan.write_bytes(b"exact binary plan")
    token = _saved_plan_confirmation_token("apply", "prod", plan)
    monkeypatch.setenv("APEX_TERRAFORM_APPLY_CONFIRM", token)

    _confirm_saved_plan("apply", "prod", plan)

    plan.write_bytes(b"changed binary plan")
    with pytest.raises(SystemExit, match="Exact saved-plan confirmation required"):
        _confirm_saved_plan("apply", "prod", plan)


def test_prod_destroy_requires_plan_digest_confirmation(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    plan = tmp_path / "prod-destroy.tfplan"
    plan.write_bytes(b"destroy exact production plan")
    monkeypatch.delenv("APEX_TERRAFORM_DESTROY_CONFIRM", raising=False)
    monkeypatch.setattr(deploy.sys.stdin, "isatty", lambda: False)

    with pytest.raises(SystemExit, match="APEX_TERRAFORM_DESTROY_CONFIRM"):
        _confirm_saved_plan("destroy", "prod", plan)


def test_failed_apply_removes_sensitive_saved_plan(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    plan = tmp_path / "apex-dev-apply.tfplan"
    plan.write_bytes(b"stale plan")
    plan.chmod(0o644)

    created_modes: list[int] = []

    def fake_run(command: list[str], *, pass_fds: tuple[int, ...] = ()) -> None:
        del pass_fds
        if "plan" in command:
            assert not plan.exists()
            plan.write_bytes(b"sensitive plan")
            created_modes.append(plan.stat().st_mode & 0o777)
        elif "apply" in command:
            raise SystemExit("apply failed")

    monkeypatch.setattr(deploy, "run", fake_run)
    monkeypatch.setattr(deploy, "_saved_plan_path", lambda *_: plan)
    monkeypatch.setattr(deploy, "_enforce_saved_plan_policy", lambda *_: None)

    with pytest.raises(SystemExit, match="apply failed"):
        deploy._plan_and_apply("deploy/terraform", "dev", plan_arguments=[])

    assert not plan.exists()
    assert created_modes == [0o600]


def test_policy_approval_and_apply_share_snapshot_when_plan_path_is_replaced(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    plan = tmp_path / "apex-prod-apply.tfplan"
    reviewed = b"policy-reviewed plan"
    replacement = b"different unapproved plan"
    policy_payloads: list[bytes] = []
    applied_payloads: list[bytes] = []
    confirmed_digests: list[str] = []

    def fake_run(command: list[str], *, pass_fds: tuple[int, ...] = ()) -> None:
        if "plan" in command:
            plan.write_bytes(reviewed)
        elif "apply" in command:
            descriptor = int(command[-1].rsplit("/", 1)[-1])
            assert pass_fds == (descriptor,)
            assert fcntl.fcntl(descriptor, fcntl.F_GETFL) & os.O_ACCMODE == os.O_RDONLY
            applied_payloads.append(Path(command[-1]).read_bytes())

    def enforce(
        _terraform_dir: str,
        _environment: str,
        snapshot_path: str,
        pass_fds: tuple[int, ...],
    ) -> None:
        descriptor = int(snapshot_path.rsplit("/", 1)[-1])
        assert pass_fds == (descriptor,)
        policy_payloads.append(Path(snapshot_path).read_bytes())
        # Win the old path-based race after policy review. Approval and apply
        # must remain bound to the already-open anonymous snapshot.
        plan.write_bytes(replacement)

    def confirm(_action: str, _environment: str, digest: str) -> str:
        confirmed_digests.append(digest)
        return digest

    monkeypatch.setattr(deploy, "run", fake_run)
    monkeypatch.setattr(deploy, "_saved_plan_path", lambda *_: plan)
    monkeypatch.setattr(deploy, "_enforce_saved_plan_policy", enforce)
    monkeypatch.setattr(deploy, "_confirm_saved_plan_digest", confirm)

    deploy._plan_and_apply("deploy/terraform", "prod", plan_arguments=[])

    assert policy_payloads == [reviewed]
    assert confirmed_digests == [hashlib.sha256(reviewed).hexdigest()]
    assert applied_payloads == [reviewed]
    assert not plan.exists()


def test_apply_reads_anonymous_snapshot_even_if_approved_path_is_replaced(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    plan = tmp_path / "approved.tfplan"
    approved = b"exact policy-reviewed plan"
    plan.write_bytes(approved)
    approved_digest = deploy._saved_plan_digest(plan)
    applied_payloads: list[bytes] = []

    def fake_run(command: list[str], *, pass_fds: tuple[int, ...] = ()) -> None:
        plan.write_bytes(b"unapproved replacement")
        descriptor = int(command[-1].rsplit("/", 1)[-1])
        assert pass_fds == (descriptor,)
        applied_payloads.append(Path(command[-1]).read_bytes())

    monkeypatch.setattr(deploy, "run", fake_run)

    deploy._apply_verified_saved_plan("deploy/terraform", plan, approved_digest)

    assert applied_payloads == [approved]


def test_rejected_destroy_removes_sensitive_saved_plan(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    plan = tmp_path / "apex-prod-destroy.tfplan"
    monkeypatch.setenv("APEX_ENV", "prod")

    def fake_run(command: list[str], *, pass_fds: tuple[int, ...] = ()) -> None:
        del pass_fds
        if "plan" in command:
            plan.write_bytes(b"sensitive destroy plan")

    def reject_confirmation(*_: object) -> None:
        raise SystemExit("confirmation rejected")

    monkeypatch.setattr(deploy, "run", fake_run)
    monkeypatch.setattr(deploy, "_saved_plan_path", lambda *_: plan)
    monkeypatch.setattr(deploy, "_confirm_saved_plan_digest", reject_confirmation)

    with pytest.raises(SystemExit, match="confirmation rejected"):
        deploy.aks_down([])

    assert not plan.exists()

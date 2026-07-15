"""Tests for deployment helpers mirrored outside the Helm chart."""

import subprocess
from collections.abc import Callable
from pathlib import Path

import pytest
from scripts import deploy
from scripts.deploy import (
    _configure_kubernetes_version,
    _confirm_saved_plan,
    _display_command,
    _ensure_tls_secret,
    _helm_fullname,
    _helm_suffixed_name,
    _pushed_image_digest,
    _saved_plan_confirmation_token,
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
    command = ["kubectl", "--key", argv_canary, "get", "secret"]

    def fail(argv: list[str], **_kwargs: object) -> object:
        raise subprocess.CalledProcessError(
            9,
            argv,
            output=f"stdout {output_canary}",
            stderr=f"stderr {output_canary}",
        )

    monkeypatch.setattr(deploy.subprocess, "run", fail)

    with pytest.raises(SystemExit) as caught:
        deploy.capture(command)

    rendered = str(caught.value)
    assert argv_canary not in rendered
    assert output_canary not in rendered
    assert "exit code 9" in rendered
    assert "[REDACTED]" in rendered


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

    created_modes: list[int] = []

    def fake_run(command: list[str]) -> None:
        if "plan" in command:
            plan.write_bytes(b"sensitive plan")
            created_modes.append(plan.stat().st_mode & 0o777)
        elif "apply" in command:
            raise SystemExit("apply failed")

    monkeypatch.setattr(deploy, "run", fake_run)
    monkeypatch.setattr(deploy, "_saved_plan_path", lambda *_: plan)
    monkeypatch.setattr(deploy, "_enforce_saved_plan_policy", lambda *_: None)
    monkeypatch.setattr(deploy, "_confirm_saved_plan", lambda *_: None)

    with pytest.raises(SystemExit, match="apply failed"):
        deploy._plan_and_apply("deploy/terraform", "dev", plan_arguments=[])

    assert not plan.exists()
    assert created_modes == [0o600]


def test_rejected_destroy_removes_sensitive_saved_plan(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    plan = tmp_path / "apex-prod-destroy.tfplan"
    monkeypatch.setenv("APEX_ENV", "prod")

    def fake_run(command: list[str]) -> None:
        if "plan" in command:
            plan.write_bytes(b"sensitive destroy plan")

    def reject_confirmation(*_: object) -> None:
        raise SystemExit("confirmation rejected")

    monkeypatch.setattr(deploy, "run", fake_run)
    monkeypatch.setattr(deploy, "_saved_plan_path", lambda *_: plan)
    monkeypatch.setattr(deploy, "_confirm_saved_plan", reject_confirmation)

    with pytest.raises(SystemExit, match="confirmation rejected"):
        deploy.aks_down([])

    assert not plan.exists()

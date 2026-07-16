"""Regressions for authenticated TLS in generated managed-service credentials."""

import ast
import hashlib
import hmac
import json
import os
import re
import shutil
import signal
import subprocess
import tempfile
import textwrap
import time
from pathlib import Path

import pytest
import yaml
from scripts.tool_versions import TOOL_CHECKSUMS, TOOL_VERSIONS, github_outputs

REPO_ROOT = Path(__file__).resolve().parents[2]
KEYVAULT_TERRAFORM = REPO_ROOT / "deploy/terraform/keyvault.tf"
DATABASE_ROLE_JOB = (
    REPO_ROOT / "deploy/helm/apex-orchestration-engine/templates/database-role-job.yaml"
)
DATABASE_ROLE_CLEANUP_JOB = (
    REPO_ROOT / "deploy/helm/apex-orchestration-engine/templates/database-role-cleanup-job.yaml"
)
HELM_VALUES = REPO_ROOT / "deploy/helm/apex-orchestration-engine/values.yaml"
HELM_CHART = REPO_ROOT / "deploy/helm/apex-orchestration-engine"
HELM_VALIDATION = REPO_ROOT / "deploy/helm/apex-orchestration-engine/templates/validate.yaml"
HELM_HELPERS = REPO_ROOT / "deploy/helm/apex-orchestration-engine/templates/_helpers.tpl"
HELM_BOOTSTRAP_JOB = (
    REPO_ROOT / "deploy/helm/apex-orchestration-engine/templates/bootstrap-job.yaml"
)
HELM_DEPLOYMENT = REPO_ROOT / "deploy/helm/apex-orchestration-engine/templates/deployment.yaml"
CSI_POST_CLEANUP = (
    REPO_ROOT / "deploy/helm/apex-orchestration-engine/templates/csi-post-cleanup-job.yaml"
)
CSI_PRE_CLEANUP = REPO_ROOT / "deploy/helm/apex-orchestration-engine/templates/csi-cleanup-job.yaml"
CSI_HOOK_SECRET_PROVIDER = (
    REPO_ROOT / "deploy/helm/apex-orchestration-engine/templates/csi-hook-secretproviderclass.yaml"
)
CSI_SECRET_LEDGER = (
    REPO_ROOT / "deploy/helm/apex-orchestration-engine/templates/csi-hook-secret-ledger-role.yaml"
)
AZURE_VALUES = REPO_ROOT / "deploy/azure/helm/values-azure.yaml"
DEPLOYMENT_RUNBOOK = REPO_ROOT / "docs/runbooks/deployment.md"
TERRAFORM_README = REPO_ROOT / "deploy/terraform/README.md"
HELM_TEST = REPO_ROOT / "deploy/helm/apex-orchestration-engine/templates/tests/test-connection.yaml"
COMPOSE = REPO_ROOT / "docker-compose.yaml"
COMPOSE_HA = REPO_ROOT / "deploy/compose-ha/docker-compose.ha.yaml"
CI_WORKFLOW = REPO_ROOT / ".github/workflows/ci.yaml"
DEPLOY_WORKFLOW = REPO_ROOT / ".github/workflows/deploy-aks.yaml"
RELEASE_WORKFLOW = REPO_ROOT / ".github/workflows/release.yaml"
DASHBOARD_ENTRYPOINT = REPO_ROOT / "apps/dashboard/docker-entrypoint.sh"
DASHBOARD_NGINX = REPO_ROOT / "apps/dashboard/nginx.conf"
DASHBOARD_STATIC_HEADERS = REPO_ROOT / "apps/dashboard/public/_headers"
DASHBOARD_VITE = REPO_ROOT / "apps/dashboard/vite.config.ts"
MIGRATION_JOB = REPO_ROOT / "deploy/helm/apex-orchestration-engine/templates/migration-job.yaml"
DATABASE_GRANTS_JOB = (
    REPO_ROOT / "deploy/helm/apex-orchestration-engine/templates/database-grants-job.yaml"
)
MIGRATION_RUNNER = REPO_ROOT / "src/apex/persistence/migrate.py"
MIGRATION_ENV = REPO_ROOT / "src/apex/persistence/migrations/env.py"
DOCKER_IGNORE = REPO_ROOT / ".dockerignore"
DASHBOARD_DOCKER_IGNORE = REPO_ROOT / "apps/dashboard/Dockerfile.dockerignore"
TRIVY_ACTION = "aquasecurity/trivy-action@ed142fd0673e97e23eac54620cfb913e5ce36c25"


def test_terraform_generated_postgres_uris_authenticate_the_server() -> None:
    source = KEYVAULT_TERRAFORM.read_text()

    assert (
        'database_uri                = "postgresql://${local.postgres_runtime_userinfo}'
        "@${local.pg_fqdn}:5432/${var.postgres_database}?sslmode=verify-full"
        '&sslrootcert=system"'
    ) in source
    for local_name in (
        "database_admin_uri",
        "apex_database_uri",
        "apex_database_migration_uri",
    ):
        assignment = next(
            line.strip() for line in source.splitlines() if line.strip().startswith(local_name)
        )
        assert assignment.endswith('?sslmode=verify-full"')
        assert "ssl=true" not in assignment

    assert "sslmode=require" not in source


def test_terraform_generated_redis_uri_keeps_verifying_rediss_defaults() -> None:
    source = KEYVAULT_TERRAFORM.read_text()
    assignment = next(
        line.strip() for line in source.splitlines() if line.strip().startswith("redis_uri")
    )

    assert '= "rediss://' in assignment
    assert "ssl_cert_reqs" not in assignment
    assert "ssl_check_hostname" not in assignment


def test_database_generation_roles_are_owned_before_any_privilege_grant() -> None:
    source = DATABASE_ROLE_JOB.read_text()

    assert "hmac.new(" in source
    assert "apex-role-claim-v2:" in source
    assert "role_claim_key.encode()" in source
    assert "admin_password.encode()" not in source
    assert "APEX_DATABASE_ROLE_CLAIM_KEY" in source
    assert "valueFrom:" in source
    assert "secretKeyRef:" in source
    assert "value: {{ .Values.databaseRoleProvisioning.claimSecret" not in source
    assert "print(role_claim_key)" not in source
    assert "logger" not in source
    assert "if not hmac.compare_digest(" in source
    assert "AND c.relowner IN (" in source
    assert "FROM pg_auth_members AS membership" in source
    assert 'f"database login role {role_name!r} has unexpected members"' in source
    assert "if parents != {expected_parent}:" in source
    assert "database generation role {raw_name!r} is unclaimed" in source
    assert "async with conn.transaction():" in source
    assert 'ownership_marker("schema", schema_name)' in source
    assert "obj_description(n.oid, 'pg_namespace') AS marker" in source
    assert 'f"CREATE SCHEMA {schema} AUTHORIZATION {migration_owner}"' in source
    assert "application schema exists under an unclaimed owner" in source
    assert "application schema is managed by another release" in source
    assert "ALTER SCHEMA apex OWNER" not in source
    assert "CREATE SCHEMA IF NOT EXISTS apex" not in source

    create_owner = source.index('f"CREATE ROLE {owner} NOLOGIN')
    mark_owner = source.index('f"COMMENT ON ROLE {owner} IS {marker_literal}"', create_owner)
    reject_unclaimed_owner = source.index("if not hmac.compare_digest(", mark_owner)
    create_role = source.index('f"CREATE ROLE {role} LOGIN')
    mark_role = source.index('f"COMMENT ON ROLE {role} IS {marker_literal}"', create_role)
    verify_marker = source.index("if not hmac.compare_digest(", mark_role)
    grant_runtime = source.index('f"GRANT {runtime_owner} TO {runtime}"', verify_marker)
    assert (
        create_owner
        < mark_owner
        < reject_unclaimed_owner
        < create_role
        < mark_role
        < verify_marker
        < grant_runtime
    )


def test_database_role_claim_rejects_predictable_comments_and_wrong_context() -> None:
    def marker(key: str, owner_id: str, kind: str, role_name: str) -> str:
        prefix = "apex-role-claim-v2"
        payload = f"{prefix}:{owner_id}:{kind}:{role_name}".encode()
        digest = hmac.new(key.encode(), payload, hashlib.sha256).hexdigest()
        return f"{prefix}:{owner_id}:{kind}:{digest}"

    key = "A" * 64
    expected = marker(key, "tenant-a/release-a", "runtime-owner", "apex_runtime")

    assert not hmac.compare_digest(expected, "apex-role-owner:tenant-a/release-a")
    assert not hmac.compare_digest(
        expected,
        marker("B" * 64, "tenant-a/release-a", "runtime-owner", "apex_runtime"),
    )
    assert not hmac.compare_digest(
        expected,
        marker(key, "tenant-b/release-a", "runtime-owner", "apex_runtime"),
    )
    assert not hmac.compare_digest(
        expected,
        marker(key, "tenant-a/release-a", "runtime-owner", "apex_runtime_v2"),
    )


def test_database_role_claim_key_is_dedicated_mandatory_and_hook_only() -> None:
    values = HELM_VALUES.read_text()
    validation = HELM_VALIDATION.read_text()
    azure_values = AZURE_VALUES.read_text()
    terraform = KEYVAULT_TERRAFORM.read_text()
    runbook = DEPLOYMENT_RUNBOOK.read_text()
    terraform_readme = TERRAFORM_README.read_text()

    assert "claimSecret: apex-database-role-claim" in values
    assert "claimSecretKey: APEX_DATABASE_ROLE_CLAIM_KEY" in values
    assert "databaseRoleProvisioning.claimSecret and claimSecretKey are required" in validation
    assert "objectName: database-role-claim" in azure_values
    assert 'resource "random_password" "database_role_claim"' in terraform
    assert (
        "length  = 64"
        in terraform[terraform.index('resource "random_password" "database_role_claim"') :]
    )
    assert "random_password.database_role_claim.result" in terraform
    assert (
        "admin_password"
        not in terraform[
            terraform.index('resource "random_password" "database_role_claim"') : terraform.index(
                'resource "azurerm_key_vault" "main"'
            )
        ]
    )
    assert (
        "API-key"
        not in terraform[
            terraform.index('resource "random_password" "database_role_claim"') : terraform.index(
                'resource "azurerm_key_vault" "main"'
            )
        ]
    )
    assert "restore the original" in runbook
    assert "not backward-compatible" in runbook
    assert "every **direct** member" in runbook
    assert "one database transaction" in runbook
    assert "Never overlap old-key and new-key Helm jobs" in " ".join(runbook.split())
    assert "admin-password" in terraform_readme
    assert "Never copy a claim key between" in terraform_readme


def test_locked_csi_deployments_remove_privileged_hook_material_after_rollout() -> None:
    validation = HELM_VALIDATION.read_text()
    post_cleanup = CSI_POST_CLEANUP.read_text()
    pre_cleanup = CSI_PRE_CLEANUP.read_text()

    assert (
        "secretBackend.csi.cleanup.enabled=true is required for secretsStoreCSI in locked "
        "environments"
    ) in validation
    assert '"helm.sh/hook": post-install,post-upgrade' in post_cleanup
    assert '"helm.sh/hook-weight": "50"' in post_cleanup
    assert "Database-role cleanup is weight 10" in post_cleanup
    for cleanup in (pre_cleanup, post_cleanup):
        assert 'resources: ["secrets"]' in cleanup
        assert 'resources: ["pods"]' in cleanup
        assert 'verbs: ["get", "list"]' in cleanup
        assert 'verbs: ["get", "list", "delete"' not in cleanup
        assert '"deletecollection"' not in cleanup
        assert "quiesce_job()" in cleanup
        assert "delete job --cascade=foreground" in cleanup
        assert 'get pods --selector="batch.kubernetes.io/job-name=$job_name"' in cleanup
        assert 'elif [ -n "$remaining_pods" ]' in cleanup
        assert "delete_resource secretproviderclass" in cleanup
        assert "delete_resource rolebinding" in cleanup
        assert "delete_resource role" in cleanup
        assert "delete_resource serviceaccount" in cleanup
        assert "delete_resource secret" in cleanup
        assert "hook-succeeded,hook-failed" in cleanup

    assert "keys $cleanupSecrets | sortAlpha" in pre_cleanup
    assert "keys $hookOnlySecrets | sortAlpha" in post_cleanup

    assert '"helm.sh/hook": pre-upgrade,post-delete,post-rollback' in pre_cleanup
    assert 'printf "csi-post-cleanup-r%d"' in pre_cleanup
    assert 'quiesce_job "$post_cleanup_name"' in pre_cleanup
    assert "for secret_consumer_job_name in $SECRET_CONSUMER_JOB_NAMES" in pre_cleanup
    assert 'quiesce_job "$secret_consumer_job_name"' in pre_cleanup
    assert '$secretConsumerJobNames | join " " | quote' in pre_cleanup
    for suffix in ("db-roles", "migrate", "db-grants", "bootstrap", "db-role-cleanup"):
        assert f'"suffix" "{suffix}"' in pre_cleanup
    assert "csi-hook-secret-ledger" in pre_cleanup
    assert "csi-prior-secret-cleanup" in pre_cleanup
    assert "jsonpath={.rules[0].resourceNames[*]}" in pre_cleanup
    assert "--ignore-not-found=true" in pre_cleanup
    assert "2>/dev/null || true" not in pre_cleanup
    assert "two-stage name migration" in pre_cleanup
    assert "shift 12" in pre_cleanup
    assert "untilStep (sub $revision 1 | int) 0 -1" in pre_cleanup
    revision_guard = re.search(r"\$maxTrackedCleanupRevision := (\d+)", pre_cleanup)
    assert revision_guard is not None
    max_tracked_revision = int(revision_guard.group(1))
    assert max_tracked_revision == 128
    assert "gt $revision $maxTrackedCleanupRevision" in pre_cleanup
    assert "uninstall the release, and reinstall it to reset the revision" in pre_cleanup
    assert max_tracked_revision * 64 < 128 * 1_024  # each joined argv stays far below 128 KiB
    assert max_tracked_revision * 11 <= 1_408  # bound sequential cleanup API operations
    assert 'printf "csi-cleanup-r%d" $priorRevision' in pre_cleanup
    assert 'printf "csi-prior-secret-cleanup-r%d" $priorRevision' in pre_cleanup
    assert '$staleCleanupNames | join " "' in pre_cleanup
    assert '$stalePriorBindingNames | join " "' in pre_cleanup
    assert 'get role "$9"' in pre_cleanup
    assert '"helm.sh/hook-weight": "-70"' in pre_cleanup
    assert '"helm.sh/hook-weight": "30"' in post_cleanup
    assert '"suffix" "csi-post-cleanup"' not in pre_cleanup
    assert '"suffix" "csi-post-cleanup"' not in post_cleanup

    ledger = CSI_SECRET_LEDGER.read_text()
    assert "deliberately left unbound between releases" in ledger
    assert "kind: Role\n" in ledger
    assert "kind: RoleBinding" not in ledger
    assert 'resources: ["secrets"]' in ledger
    assert "previousHookSecretNames" in ledger
    assert "previousHookSecretNames" in HELM_VALUES.read_text()


@pytest.mark.skipif(shutil.which("helm") is None, reason="Helm is not installed")
def test_rendered_pre_cleanup_has_exact_secret_consumer_job_authorization_and_gate() -> None:
    helm = shutil.which("helm")
    assert helm is not None
    digest = "sha256:" + "a" * 64
    rendered = subprocess.run(
        [
            helm,
            "template",
            "cleanup-render",
            str(HELM_CHART),
            "-f",
            str(AZURE_VALUES),
            "--set-string",
            f"image.digest={digest}",
            "--set-string",
            f"dashboard.image.digest={digest}",
            "--set-string",
            "databaseRoleProvisioning.credentialGeneration=0123456789abcdef",
        ],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    documents = [
        document for document in yaml.safe_load_all(rendered) if isinstance(document, dict)
    ]
    cleanup_resources = [
        document
        for document in documents
        if document.get("metadata", {}).get("labels", {}).get("app.kubernetes.io/component")
        == "csi-cleanup"
    ]
    cleanup_job = next(document for document in cleanup_resources if document.get("kind") == "Job")
    cleanup_role = next(
        document for document in cleanup_resources if document.get("kind") == "Role"
    )
    container = cleanup_job["spec"]["template"]["spec"]["containers"][0]
    environment = {entry["name"]: entry.get("value") for entry in container["env"]}
    job_names = environment["SECRET_CONSUMER_JOB_NAMES"].split()

    expected_suffixes = {"db-roles", "migrate", "db-grants", "bootstrap", "db-role-cleanup"}
    assert len(job_names) == len(expected_suffixes)
    assert {
        suffix
        for suffix in expected_suffixes
        if any(name.endswith(f"-{suffix}") for name in job_names)
    } == expected_suffixes
    job_rule = next(rule for rule in cleanup_role["rules"] if rule.get("resources") == ["jobs"])
    assert set(job_names) <= set(job_rule["resourceNames"])
    assert job_rule["verbs"] == ["get", "delete"]

    script = container["args"][0]
    quiescence_loop = script.index("for secret_consumer_job_name in $SECRET_CONSUMER_JOB_NAMES")
    deletion_gate = script.index('if [ "$quiesced" -eq 1 ]', quiescence_loop)
    secret_material_deletion = script.index("delete_resource secretproviderclass", deletion_gate)
    assert quiescence_loop < deletion_gate < secret_material_deletion


def test_csi_names_are_validated_and_never_rendered_as_shell_source() -> None:
    validation = HELM_VALIDATION.read_text()

    assert 'list "objects" "hookObjects"' in validation
    assert ".secretName must be a valid 1-253 character Kubernetes DNS subdomain" in validation
    assert ".objectName must be a valid 1-127 character Azure Key Vault secret name" in validation
    assert ".key must be a valid 1-253 character Kubernetes Secret data key" in validation
    assert "must not reuse a runtime Secret name" in validation
    assert "previousHookSecretNames[%d] must not reuse a current runtime Secret name" in validation
    assert "hookServiceAccountName must be distinct from the runtime service account" in validation

    for path in (CSI_PRE_CLEANUP, CSI_POST_CLEANUP):
        source = path.read_text()
        script = source.split("            - |\n", 1)[1].split("            - cleanup\n", 1)[0]
        assert "{{" not in script
        assert 'cleanup_all "$@"' in script
        assert 'delete "$resource" --ignore-not-found' in script
        assert "| quote" in source.split("            - cleanup\n", 1)[1]
        assert "cleanup_failed_identity" in script
        assert "delete_resource serviceaccount" in script
        assert "delete_resource rolebinding" in script

    post_cleanup = CSI_POST_CLEANUP.read_text()
    assert "for secret_name do" in post_cleanup
    assert 'delete_resource secret "$secret_name"' in post_cleanup
    hook_provider = CSI_HOOK_SECRET_PROVIDER.read_text()
    assert "app.kubernetes.io/component: csi-hook-secret" in hook_provider
    assert "app.kubernetes.io/instance:" in hook_provider


def test_csi_cleanup_attempts_all_compensation_after_api_failure() -> None:
    source = CSI_POST_CLEANUP.read_text()
    raw_script = source.split("            - |\n", 1)[1].split("            - cleanup\n", 1)[0]
    script = textwrap.dedent(raw_script)
    args = [
        "cleanup",
        "sync-job",
        "hook-spc",
        "runtime-hook-spc",
        "sync-rbac",
        "hook-sa",
        "cleanup-self",
        "database-admin-secret",
        "bootstrap-secret",
    ]

    with tempfile.TemporaryDirectory() as directory:
        directory_path = Path(directory)
        log = directory_path / "kubectl.log"
        kubectl = directory_path / "kubectl"
        kubectl.write_text(
            "#!/bin/sh\n"
            'printf "%s\\n" "$*" >> "$KUBECTL_LOG"\n'
            'case "$*" in *"secretproviderclass"*"hook-spc"*) exit 9 ;; esac\n'
            "exit 0\n"
        )
        kubectl.chmod(0o755)
        result = subprocess.run(
            ["sh", "-ec", script, *args],
            env={
                **os.environ,
                "PATH": f"{directory}:{os.environ['PATH']}",
                "POD_NAMESPACE": "apex",
                "KUBECTL_LOG": str(log),
            },
            check=False,
        )

        calls = log.read_text()
        assert result.returncode != 0
        assert "delete job" in calls
        assert "delete secret --ignore-not-found" in calls
        assert "database-admin-secret" in calls
        assert "bootstrap-secret" in calls
        # The EXIT trap revokes the self authorization after all attempts.
        assert calls.rstrip().endswith(
            "delete rolebinding --ignore-not-found --wait=true --timeout=5s -- cleanup-self"
        )


@pytest.mark.parametrize(
    ("template", "args"),
    [
        (
            CSI_PRE_CLEANUP,
            [
                "cleanup",
                "sync-job",
                "hook-spc",
                "runtime-hook-spc",
                "sync-rbac",
                "hook-sa",
                "post-cleanup",
                "stale-cleanup",
                "stale-prior-binding",
                "ledger-role",
                "pre-cleanup",
                "prior-binding",
                "runtime-secret",
                "hook-secret",
            ],
        ),
        (
            CSI_POST_CLEANUP,
            [
                "cleanup",
                "sync-job",
                "hook-spc",
                "runtime-hook-spc",
                "sync-rbac",
                "hook-sa",
                "post-cleanup",
                "hook-secret",
            ],
        ),
    ],
)
@pytest.mark.parametrize("failure_mode", ["job", "pod_api", "pod_orphan"])
def test_hook_cleanup_retains_secret_material_when_quiescence_fails(
    tmp_path: Path,
    template: Path,
    args: list[str],
    failure_mode: str,
) -> None:
    source = template.read_text()
    raw_script = source.split("            - |\n", 1)[1].split("            - cleanup\n", 1)[0]
    script = textwrap.dedent(raw_script)
    log = tmp_path / "kubectl.log"
    kubectl = tmp_path / "kubectl"
    kubectl.write_text(
        "#!/bin/sh\n"
        'printf "%s\\n" "$*" >> "$KUBECTL_LOG"\n'
        'case "$FAILURE_MODE:$*" in\n'
        '  job:*"delete job --cascade=foreground"*"-- sync-job") exit 9 ;;\n'
        '  pod_api:*"get pods --selector=batch.kubernetes.io/job-name=sync-job"*) exit 9 ;;\n'
        '  pod_orphan:*"get pods --selector=batch.kubernetes.io/job-name=sync-job"*) '
        'printf "%s\\n" "pod/sync-orphan" ;;\n'
        "esac\n"
        "exit 0\n"
    )
    kubectl.chmod(0o755)

    result = subprocess.run(
        ["sh", "-ec", script, *args],
        env={
            **os.environ,
            "PATH": f"{tmp_path}:{os.environ['PATH']}",
            "POD_NAMESPACE": "apex",
            "KUBECTL_LOG": str(log),
            "FAILURE_MODE": failure_mode,
        },
        check=False,
    )

    calls = log.read_text().splitlines()
    assert result.returncode != 0
    assert any(
        "delete job --cascade=foreground" in call and call.endswith("-- sync-job") for call in calls
    )
    assert any(
        "get pods --selector=batch.kubernetes.io/job-name=sync-job" in call for call in calls
    )
    assert not any("delete secretproviderclass" in call for call in calls)
    assert not any(" delete secret " in f" {call} " for call in calls)
    # Workload identity/RBAC cleanup remains exhaustive after fail-closed
    # retention, and the hook still revokes its own authorization last.
    assert any("delete rolebinding" in call and call.endswith("-- sync-rbac") for call in calls)
    assert any("delete serviceaccount" in call and call.endswith("-- hook-sa") for call in calls)
    assert "delete rolebinding" in calls[-1] or "delete serviceaccount" in calls[-1]


@pytest.mark.parametrize(
    "stranded_job",
    ["db-roles", "migrate", "db-grants", "bootstrap", "db-role-cleanup"],
)
def test_pre_cleanup_fails_closed_for_each_stranded_secret_consumer_job(
    tmp_path: Path,
    stranded_job: str,
) -> None:
    source = CSI_PRE_CLEANUP.read_text()
    raw_script = source.split("            - |\n", 1)[1].split("            - cleanup\n", 1)[0]
    script = textwrap.dedent(raw_script)
    secret_consumer_jobs = ("db-roles", "migrate", "db-grants", "bootstrap", "db-role-cleanup")
    args = [
        "cleanup",
        "sync-job",
        "hook-spc",
        "runtime-hook-spc",
        "sync-rbac",
        "hook-sa",
        "post-cleanup",
        "stale-cleanup",
        "stale-prior-binding",
        "ledger-role",
        "pre-cleanup",
        "prior-binding",
        "runtime-secret",
        "hook-secret",
    ]
    log = tmp_path / "kubectl.log"
    kubectl = tmp_path / "kubectl"
    kubectl.write_text(
        "#!/bin/sh\n"
        'printf "%s\\n" "$*" >> "$KUBECTL_LOG"\n'
        'case "$*" in\n'
        '  *"get pods --selector=batch.kubernetes.io/job-name=$STRANDED_JOB"*) '
        'printf "%s\\n" "pod/stranded-secret-consumer" ;;\n'
        "esac\n"
        "exit 0\n"
    )
    kubectl.chmod(0o755)

    result = subprocess.run(
        ["sh", "-ec", script, *args],
        env={
            **os.environ,
            "PATH": f"{tmp_path}:{os.environ['PATH']}",
            "POD_NAMESPACE": "apex",
            "KUBECTL_LOG": str(log),
            "SECRET_CONSUMER_JOB_NAMES": " ".join(secret_consumer_jobs),
            "STRANDED_JOB": stranded_job,
        },
        check=False,
    )

    calls = log.read_text().splitlines()
    assert result.returncode != 0
    for job_name in secret_consumer_jobs:
        assert any(
            "delete job --cascade=foreground" in call and call.endswith(f"-- {job_name}")
            for call in calls
        )
        assert any(
            f"get pods --selector=batch.kubernetes.io/job-name={job_name}" in call for call in calls
        )
    assert not any("delete secretproviderclass" in call for call in calls)
    assert not any(" delete secret " in f" {call} " for call in calls)


@pytest.mark.parametrize(
    ("template", "args", "expected_prior", "expected_self"),
    [
        (
            CSI_PRE_CLEANUP,
            [
                "cleanup",
                "sync-job",
                "hook-spc",
                "runtime-hook-spc",
                "sync-rbac",
                "hook-sa",
                "post-cleanup",
                "legacy-cleanup stale-cleanup",
                "stale-prior-binding",
                "ledger-role",
                "pre-cleanup",
                "prior-binding",
                "runtime-secret",
                "hook-secret",
            ],
            "prior-binding",
            "pre-cleanup",
        ),
        (
            CSI_POST_CLEANUP,
            [
                "cleanup",
                "sync-job",
                "hook-spc",
                "runtime-hook-spc",
                "sync-rbac",
                "hook-sa",
                "post-cleanup",
                "hook-secret",
            ],
            None,
            "post-cleanup",
        ),
    ],
)
def test_successful_csi_cleanup_revokes_its_own_identity_last(
    tmp_path: Path,
    template: Path,
    args: list[str],
    expected_prior: str | None,
    expected_self: str,
) -> None:
    source = template.read_text()
    raw_script = source.split("            - |\n", 1)[1].split("            - cleanup\n", 1)[0]
    script = textwrap.dedent(raw_script)
    log = tmp_path / "kubectl.log"
    kubectl = tmp_path / "kubectl"
    kubectl.write_text('#!/bin/sh\nprintf "%s\\n" "$*" >> "$KUBECTL_LOG"\nexit 0\n')
    kubectl.chmod(0o755)

    result = subprocess.run(
        ["sh", "-ec", script, *args],
        env={
            **os.environ,
            "PATH": f"{tmp_path}:{os.environ['PATH']}",
            "POD_NAMESPACE": "apex",
            "KUBECTL_LOG": str(log),
        },
        check=False,
    )

    assert result.returncode == 0
    calls = log.read_text().splitlines()
    job_index = next(
        index
        for index, call in enumerate(calls)
        if "delete job --cascade=foreground" in call and call.endswith("-- sync-job")
    )
    first_sync_revoke = next(
        index
        for index, call in enumerate(calls)
        if "delete rolebinding" in call and call.endswith("-- sync-rbac")
    )
    pod_index = next(
        index
        for index, call in enumerate(calls)
        if "get pods --selector=batch.kubernetes.io/job-name=sync-job" in call
    )
    spc_index = next(
        index for index, call in enumerate(calls) if "delete secretproviderclass" in call
    )
    assert first_sync_revoke < job_index < pod_index < spc_index
    assert calls[-1].endswith(
        f"delete rolebinding --ignore-not-found --wait=true --timeout=5s -- {expected_self}"
    )
    if expected_prior is not None:
        prior = next(
            index for index, call in enumerate(calls) if call.endswith(f"-- {expected_prior}")
        )
        assert prior == len(calls) - 2


def test_pre_cleanup_collects_multiple_stranded_revision_identities_first(
    tmp_path: Path,
) -> None:
    source = CSI_PRE_CLEANUP.read_text()
    raw_script = source.split("            - |\n", 1)[1].split("            - cleanup\n", 1)[0]
    script = textwrap.dedent(raw_script)
    stale_cleanup_names = ("cleanup-r8", "cleanup-r7", "legacy-cleanup")
    stale_prior_bindings = ("prior-binding-r8", "prior-binding-r7")
    args = [
        "cleanup",
        "sync-job",
        "hook-spc",
        "runtime-hook-spc",
        "sync-rbac",
        "hook-sa",
        "post-cleanup-r9 post-cleanup-r8 post-cleanup-r7",
        " ".join(stale_cleanup_names),
        " ".join(stale_prior_bindings),
        "ledger-role",
        "cleanup-r9",
        "prior-binding-r9",
        "runtime-secret",
        "hook-secret",
    ]
    log = tmp_path / "kubectl.log"
    kubectl = tmp_path / "kubectl"
    kubectl.write_text('#!/bin/sh\nprintf "%s\\n" "$*" >> "$KUBECTL_LOG"\nexit 0\n')
    kubectl.chmod(0o755)

    result = subprocess.run(
        ["sh", "-ec", script, *args],
        env={
            **os.environ,
            "PATH": f"{tmp_path}:{os.environ['PATH']}",
            "POD_NAMESPACE": "apex",
            "KUBECTL_LOG": str(log),
        },
        check=False,
    )

    assert result.returncode == 0
    calls = log.read_text().splitlines()
    sync_job_index = next(
        index
        for index, call in enumerate(calls)
        if "delete job" in call and call.endswith("-- sync-job")
    )
    for name in stale_prior_bindings:
        binding_index = next(
            index for index, call in enumerate(calls) if call.endswith(f"-- {name}")
        )
        assert binding_index < sync_job_index
    for name in stale_cleanup_names:
        matching = [
            (index, call) for index, call in enumerate(calls) if call.endswith(f"-- {name}")
        ]
        assert {call.split(" delete ", 1)[1].split(" ", 1)[0] for _, call in matching} == {
            "job",
            "role",
            "rolebinding",
            "serviceaccount",
        }
        assert all(index < sync_job_index for index, _ in matching)
    assert calls[-2].endswith("-- prior-binding-r9")
    assert calls[-1].endswith("-- cleanup-r9")


def test_failed_binding_revocation_cannot_reactivate_on_the_next_release(
    tmp_path: Path,
) -> None:
    source = CSI_POST_CLEANUP.read_text()
    raw_script = source.split("            - |\n", 1)[1].split("            - cleanup\n", 1)[0]
    script = textwrap.dedent(raw_script)
    old_identity = "post-cleanup-r7"
    next_identity = "post-cleanup-r8"
    args = [
        "cleanup",
        "sync-job",
        "hook-spc",
        "runtime-hook-spc",
        "sync-rbac",
        "hook-sa",
        old_identity,
        "hook-secret",
    ]
    log = tmp_path / "kubectl.log"
    kubectl = tmp_path / "kubectl"
    kubectl.write_text(
        "#!/bin/sh\n"
        'printf "%s\\n" "$*" >> "$KUBECTL_LOG"\n'
        f'case "$*" in *"delete rolebinding"*"-- {old_identity}") exit 9 ;; esac\n'
        "exit 0\n"
    )
    kubectl.chmod(0o755)

    result = subprocess.run(
        ["sh", "-ec", script, *args],
        env={
            **os.environ,
            "PATH": f"{tmp_path}:{os.environ['PATH']}",
            "POD_NAMESPACE": "apex",
            "KUBECTL_LOG": str(log),
        },
        check=False,
    )

    assert result.returncode == 0
    calls = log.read_text().splitlines()
    assert calls[-2].endswith(
        f"delete rolebinding --ignore-not-found --wait=true --timeout=5s -- {old_identity}"
    )
    assert calls[-1].endswith(
        f"delete serviceaccount --ignore-not-found --wait=true --timeout=5s -- {old_identity}"
    )
    assert old_identity != next_identity
    # The chart derives all cleanup subjects and bindings from the monotonic
    # Helm release revision, so the r7 binding cannot target the r8 subject.
    assert 'printf "csi-post-cleanup-r%d" (int .Release.Revision)' in source
    pre_cleanup = CSI_PRE_CLEANUP.read_text()
    assert 'printf "csi-cleanup-r%d" $revision' in pre_cleanup
    assert 'printf "csi-prior-secret-cleanup-r%d" $revision' in pre_cleanup


def test_csi_cleanup_timeout_paths_prioritize_bounded_identity_revocation() -> None:
    pre_cleanup = CSI_PRE_CLEANUP.read_text()
    post_cleanup = CSI_POST_CLEANUP.read_text()

    for source in (pre_cleanup, post_cleanup):
        assert "activeDeadlineSeconds: 300" in source
        assert "terminationGracePeriodSeconds: 60" in source
        assert "kubectl --request-timeout=5s" in source
        assert "--timeout=5s" in source
        signal_handler = source.split("              handle_signal() {", 1)[1].split(
            "              trap cleanup_failed_identity EXIT", 1
        )[0]
        assert "cleanup_all" not in signal_handler
        assert "revoke_sync_identity" in signal_handler
        assert "revoke_identity" in signal_handler
        cleanup_body = source.split("              cleanup_all() {", 1)[1].split(
            "              handle_signal() {", 1
        )[0]
        assert "trap '' HUP INT TERM" not in cleanup_body

    # Uninstall cleanup is a post-delete hook and uses the same self-revoking
    # pre-cleanup authorization as upgrade/rollback compensation.
    assert '"helm.sh/hook": pre-upgrade,post-delete,post-rollback' in pre_cleanup
    assert (
        'delete rolebinding --ignore-not-found --wait=true --timeout=5s -- "$self_name"'
        in pre_cleanup
    )


@pytest.mark.parametrize(
    ("template", "args", "self_name", "prior_binding"),
    [
        (
            CSI_PRE_CLEANUP,
            [
                "cleanup",
                "sync-job",
                "hook-spc",
                "runtime-hook-spc",
                "sync-rbac",
                "hook-sa",
                "",
                "",
                "",
                "ledger-role",
                "pre-cleanup",
                "prior-binding",
                "runtime-secret",
                "hook-secret",
            ],
            "pre-cleanup",
            "prior-binding",
        ),
        (
            CSI_POST_CLEANUP,
            [
                "cleanup",
                "sync-job",
                "hook-spc",
                "runtime-hook-spc",
                "sync-rbac",
                "hook-sa",
                "post-cleanup",
                "hook-secret",
            ],
            "post-cleanup",
            None,
        ),
    ],
)
def test_csi_cleanup_signal_revokes_sync_and_cleanup_identities_during_blocked_work(
    tmp_path: Path,
    template: Path,
    args: list[str],
    self_name: str,
    prior_binding: str | None,
) -> None:
    source = template.read_text()
    raw_script = source.split("            - |\n", 1)[1].split("            - cleanup\n", 1)[0]
    script = textwrap.dedent(raw_script)
    log = tmp_path / "kubectl.log"
    started = tmp_path / "blocked.started"
    kubectl = tmp_path / "kubectl"
    kubectl.write_text(
        "#!/bin/sh\n"
        'printf "%s\\n" "$*" >> "$KUBECTL_LOG"\n'
        'case "$*" in\n'
        '  *"delete job --cascade=foreground"*"-- sync-job")\n'
        '    : > "$BLOCK_STARTED"\n'
        "    sleep 1\n"
        "    ;;\n"
        "esac\n"
        "exit 0\n"
    )
    kubectl.chmod(0o755)
    process = subprocess.Popen(
        ["sh", "-ec", script, *args],
        env={
            **os.environ,
            "PATH": f"{tmp_path}:{os.environ['PATH']}",
            "POD_NAMESPACE": "apex",
            "KUBECTL_LOG": str(log),
            "BLOCK_STARTED": str(started),
        },
    )
    deadline = time.monotonic() + 3
    while not started.exists() and process.poll() is None and time.monotonic() < deadline:
        time.sleep(0.01)
    assert started.exists(), "cleanup did not reach the blocking foreground Job deletion"

    process.send_signal(signal.SIGTERM)
    process.wait(timeout=5)

    assert process.returncode == 143
    calls = log.read_text().splitlines()
    assert any("delete rolebinding" in call and call.endswith("-- sync-rbac") for call in calls)
    assert any("delete role " in f"{call} " and call.endswith("-- sync-rbac") for call in calls)
    assert any("delete serviceaccount" in call and call.endswith("-- hook-sa") for call in calls)
    assert any("delete rolebinding" in call and call.endswith(f"-- {self_name}") for call in calls)
    if prior_binding is not None:
        assert any(call.endswith(f"-- {prior_binding}") for call in calls)
    assert not any("delete secretproviderclass" in call for call in calls)
    assert not any(" delete secret " in f" {call} " for call in calls)


def test_database_role_cleanup_revalidates_credential_bound_claims() -> None:
    source = DATABASE_ROLE_CLEANUP_JOB.read_text()

    assert "hmac.new(" in source
    assert "apex-role-claim-v2:" in source
    assert "role_claim_key.encode()" in source
    assert "APEX_DATABASE_ROLE_CLAIM_KEY" in source
    assert "secretKeyRef:" in source
    assert "print(role_claim_key)" not in source
    assert "logger" not in source
    assert 'f"{role_kind}-generation"' in source
    assert "or (login and children)" in source
    assert "parents != expected_parents" in source
    assert 'expected_schema_marker = ownership_marker("schema", "apex")' in source
    assert "schema is not claimed by this release" in source
    verify_generation = source.index(
        "if not hmac.compare_digest(",
        source.index("expected_marker = ownership_marker("),
    )
    terminate_stale = source.index('"SELECT pg_terminate_backend(pid)', verify_generation)
    drop_stale = source.index('await conn.execute(f"DROP ROLE {role}")', terminate_stale)
    assert verify_generation < terminate_stale < drop_stale


def test_database_role_hooks_use_quoted_sql_and_exact_direct_membership() -> None:
    provision = DATABASE_ROLE_JOB.read_text()
    cleanup = DATABASE_ROLE_CLEANUP_JOB.read_text()

    for source in (provision, cleanup):
        assert 'return await conn.fetchval("SELECT quote_ident($1)", value)' in source
        assert "FROM pg_auth_members AS membership" in source
        assert "membership.admin_option AS admin_option" in source
        assert "administrable" in source
        assert "parent membership" in source
        assert "WITH RECURSIVE" not in source
    assert 'return await conn.fetchval("SELECT quote_literal($1)", value)' in provision
    assert "COMMENT ON ROLE {owner} IS {marker_literal}" in provision
    assert "COMMENT ON ROLE {role} IS {marker_literal}" in provision
    assert "parents != {expected_parent}" in provision
    assert "current generation role is not a direct member" in cleanup


def test_runtime_grants_and_schema_metadata_denials_are_one_transaction() -> None:
    def embedded_script(path: Path) -> ast.Module:
        source = path.read_text()
        raw = source.split("          args:\n            - |\n", 1)[1].split("          env:\n", 1)[
            0
        ]
        return ast.parse(textwrap.dedent(raw))

    broad_grant = "GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA apex"
    metadata_deny = "REVOKE INSERT, UPDATE, DELETE ON TABLE"
    for path in (DATABASE_ROLE_JOB, DATABASE_GRANTS_JOB):
        tree = embedded_script(path)
        transaction_blocks = [
            ast.unparse(node) for node in ast.walk(tree) if isinstance(node, ast.AsyncWith)
        ]
        assert any(
            broad_grant in block
            and metadata_deny in block
            and "apex.alembic_version" in block
            and "apex.alembic_revision_lineage" in block
            for block in transaction_blocks
        )


def test_secret_backend_mode_is_a_fail_closed_enum() -> None:
    validation = HELM_VALIDATION.read_text()
    workflow = CI_WORKFLOW.read_text()

    assert 'list "existingSecret" "secretsStoreCSI" "externalSecrets"' in validation
    assert (
        "secretBackend.mode must be one of existingSecret, secretsStoreCSI, or externalSecrets"
        in validation
    )
    assert "--set-string secretBackend.mode=unsupported" in workflow


def test_supported_aks_deploy_is_atomic_and_has_out_of_band_failure_cleanup() -> None:
    workflow = DEPLOY_WORKFLOW.read_text()
    deploy_script = (REPO_ROOT / "scripts/deploy.py").read_text()
    cleanup_script = (REPO_ROOT / "scripts/cleanup_csi_hook_material.py").read_text()

    for source in (workflow, deploy_script):
        assert "--atomic" in source
        assert "--cleanup-on-fail" in source
        assert "cleanup_csi_hook_material" in source
    assert "failure() || cancelled()" in workflow
    assert "trap cleanup_failed_csi_hooks EXIT" in workflow
    assert "forward_cleanup_signal" in workflow
    assert "cleanup_timeout=8s" in workflow
    assert "exec python3 scripts/cleanup_csi_hook_material.py --timeout 120s" in workflow
    assert "trap '' HUP INT TERM" not in workflow
    for hook_secret in (
        "apex-database-admin",
        "apex-database-role-claim",
        "apex-database-bootstrap",
        "apex-database-migration",
        "apex-admin",
        "apex-hook-auth",
    ):
        assert hook_secret in workflow
        assert hook_secret in deploy_script
    assert "Attempt every cleanup operation" in cleanup_script


def test_existing_application_schema_is_never_adopted_by_name_alone() -> None:
    source = DATABASE_ROLE_JOB.read_text()

    verify_owner = source.index('if state["owner_name"] != migration_owner_name:')
    mark_legacy = source.index('if state["marker"] is None:', verify_owner)
    reject_foreign_marker = source.index("elif not hmac.compare_digest(", mark_legacy)
    persist_check = source.index("persisted = await conn.fetchrow(", reject_foreign_marker)
    transfer_apex = source.index(
        'await transfer_relations("apex", migration_owner_name, migration_owner)',
        persist_check,
    )

    assert verify_owner < mark_legacy < reject_foreign_marker < persist_check < transfer_apex


def test_cleanup_verifies_every_generation_claim_before_any_stale_role_action() -> None:
    source = DATABASE_ROLE_CLEANUP_JOB.read_text()

    enumerate_managed = source.index('managed = {row["rolname"] for row in role_names}')
    verify_schema = source.index('expected_schema_marker = ownership_marker("schema", "apex")')
    validate_all = source.index("for raw_role in sorted(managed):", enumerate_managed)
    verify_claim = source.index("if not hmac.compare_digest(", validate_all)
    select_stale = source.index("stale = sorted(", verify_claim)
    terminate = source.index('"SELECT pg_terminate_backend(pid)', select_stale)

    assert (
        verify_schema < enumerate_managed < validate_all < verify_claim < select_stale < terminate
    )


def test_every_privileged_database_hook_revalidates_claims_before_mutation() -> None:
    migration_job = MIGRATION_JOB.read_text()
    grants_job = DATABASE_GRANTS_JOB.read_text()
    migration_runner = MIGRATION_RUNNER.read_text()
    migration_env = MIGRATION_ENV.read_text()

    for source in (migration_job, grants_job):
        assert "APEX_DATABASE_ROLE_CLAIM_KEY" in source
        assert "secretKeyRef:" in source
        assert "APEX_DATABASE_ROLE_OWNER_ID" in source
        assert "APEX_RUNTIME_OWNER_ROLE" in source
        assert "APEX_MIGRATION_OWNER_ROLE" in source
    migration_lock = migration_runner.index('"SELECT pg_advisory_lock($1::bigint)"')
    migration_verify = migration_runner.index(
        "await verify_database_role_claims(driver_connection, context)",
        migration_lock,
    )
    migration_ddl = migration_runner.index(
        "await connection.run_sync(_upgrade_to_packaged_head)",
        migration_verify,
    )
    migration_final_check = migration_runner.index(
        "finally_compatible = await _schema_is_compatible_async(context.migration_uri)",
        migration_ddl,
    )
    assert migration_lock < migration_verify < migration_ddl < migration_final_check
    assert 'config.attributes["connection"] = connection' in migration_runner
    assert 'supplied_connection = config.attributes.get("connection")' in migration_env
    assert "run_migrations_with_supplied_connection(supplied_connection)" in migration_env

    grants_lock = grants_job.index('"SELECT pg_advisory_lock($1::bigint)"')
    grants_verify = grants_job.index("await verify_database_role_claims(", grants_lock)
    grants_mutation = grants_job.index(
        'f"GRANT USAGE ON SCHEMA apex TO {runtime}"',
        grants_verify,
    )
    assert grants_lock < grants_verify < grants_mutation


def test_every_production_readiness_consumer_uses_the_dynamic_probe() -> None:
    values = HELM_VALUES.read_text()
    compose = COMPOSE.read_text()
    compose_ha = COMPOSE_HA.read_text()
    workflow = DEPLOY_WORKFLOW.read_text()
    dashboard_entrypoint = DASHBOARD_ENTRYPOINT.read_text()
    dashboard_vite = DASHBOARD_VITE.read_text()

    assert "readinessProbe:\n  httpGet:\n    path: /ready" in values
    assert "livenessProbe:\n  httpGet:\n    path: /ok" in values
    assert "startupProbe:\n  httpGet:\n    path: /ok" in values
    assert "http://localhost:8000/ready" in compose
    assert "http://localhost:8000/ready" in compose_ha
    assert "http://localhost:8123/ready" in compose_ha
    assert "/ready" in HELM_TEST.read_text()
    assert '"https://$APEX_HOSTNAME/ready"' in workflow
    assert "|ok|ready)(/|" in dashboard_entrypoint
    assert "'/ready'" in dashboard_vite


def _run_dashboard_entrypoint(
    tmp_path: Path,
    *,
    apex_origin: str = "",
    langgraph_origin: str = "",
    backend_upstream: str = "",
) -> tuple[subprocess.CompletedProcess[str], Path, Path]:
    config_path = tmp_path / "config.json"
    proxy_path = tmp_path / "apex-proxy.conf"
    security_path = tmp_path / "apex-security-headers.conf"
    script_path = tmp_path / "dashboard-entrypoint.sh"
    source = DASHBOARD_ENTRYPOINT.read_text()
    source = source.replace("/tmp/config.json", str(config_path))
    source = source.replace("/tmp/apex-proxy.conf", str(proxy_path))
    source = source.replace("/tmp/apex-security-headers.conf", str(security_path))
    script_path.write_text(source)
    environment = {
        **os.environ,
        "APEX_ORIGIN": apex_origin,
        "LANGGRAPH_ORIGIN": langgraph_origin,
        "BACKEND_UPSTREAM": backend_upstream,
    }
    completed = subprocess.run(
        ["sh", str(script_path), "true"],
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )
    return completed, config_path, proxy_path


def test_dashboard_entrypoint_generates_only_canonical_origins(tmp_path: Path) -> None:
    completed, config_path, proxy_path = _run_dashboard_entrypoint(
        tmp_path,
        apex_origin="https://api.example.test/",
        langgraph_origin="https://langgraph.example.test:8443",
        backend_upstream="http://apex.default.svc:80",
    )

    assert completed.returncode == 0, completed.stderr
    assert json.loads(config_path.read_text()) == {
        "apexOrigin": "https://api.example.test",
        "langgraphOrigin": "https://langgraph.example.test:8443",
    }
    proxy = proxy_path.read_text()
    assert "proxy_pass http://apex.default.svc:80;" in proxy
    security = (tmp_path / "apex-security-headers.conf").read_text()
    assert (
        "connect-src 'self' https://api.example.test wss://api.example.test "
        "https://langgraph.example.test:8443 wss://langgraph.example.test:8443;" in security
    )
    assert "connect-src 'self' http: https: ws: wss:" not in security


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("apex_origin", 'https://api.example.test/";location /injected {}'),
        ("langgraph_origin", "https://user:runtime-origin-secret-canary@example.test"),
        ("backend_upstream", "http://apex:80;location /injected {}"),
        ("backend_upstream", "http://apex:65536"),
    ],
)
def test_dashboard_entrypoint_rejects_json_and_nginx_syntax_injection(
    tmp_path: Path,
    field: str,
    value: str,
) -> None:
    values = {
        "apex_origin": "",
        "langgraph_origin": "",
        "backend_upstream": "",
    }
    values[field] = value
    completed, config_path, proxy_path = _run_dashboard_entrypoint(
        tmp_path,
        apex_origin=values["apex_origin"],
        langgraph_origin=values["langgraph_origin"],
        backend_upstream=values["backend_upstream"],
    )

    assert completed.returncode != 0
    assert "runtime-origin-secret-canary" not in completed.stderr
    assert "location /injected" not in completed.stderr
    assert not config_path.exists()
    assert not proxy_path.exists()
    assert not (tmp_path / "apex-security-headers.conf").exists()


def test_dashboard_csp_does_not_allow_arbitrary_api_key_exfiltration() -> None:
    entrypoint = DASHBOARD_ENTRYPOINT.read_text()
    nginx = DASHBOARD_NGINX.read_text()
    static_headers = DASHBOARD_STATIC_HEADERS.read_text()

    assert "connect_sources=\"'self'\"" in entrypoint
    assert "include /tmp/apex-security-headers.conf;" in nginx
    assert nginx.count("include /tmp/apex-security-headers.conf;") == 2
    for source in (nginx, static_headers):
        assert "connect-src 'self' http: https: ws: wss:" not in source
    assert "connect-src 'self';" in static_headers


def test_dashboard_chart_requires_safe_functional_backend_wiring() -> None:
    validation = HELM_VALIDATION.read_text()

    assert "dashboard.config.apexOrigin must be empty or a complete HTTP(S) origin" in validation
    assert (
        "dashboard.config.langgraphOrigin must be empty or a complete HTTP(S) origin" in validation
    )
    assert "dashboard.backendUpstream must be empty or a complete HTTP(S) origin" in validation
    assert "dashboard.config.apexOrigin must use https:// in locked environments" in validation
    assert "dashboard.config.langgraphOrigin must use https:// in locked environments" in validation
    assert "contains a TCP port outside 1..65535" in validation
    assert "dashboard requires backendUpstream or both explicit API origins" in validation


def test_deploy_workflow_limits_oidc_to_jobs_that_login_to_azure() -> None:
    workflow = yaml.safe_load(DEPLOY_WORKFLOW.read_text())
    jobs = workflow["jobs"]
    azure_jobs: set[str] = set()

    assert workflow["permissions"] == {"contents": "read"}
    for job_name, job in jobs.items():
        login_steps = [
            step
            for step in job.get("steps", [])
            if str(step.get("uses", "")).startswith("azure/login@")
        ]
        if login_steps:
            azure_jobs.add(job_name)
            # A duplicate or partially configured login fails before the real
            # deployment work and unnecessarily requests another OIDC token.
            assert len(login_steps) == 1
            assert set(login_steps[0].get("with", {})) == {
                "client-id",
                "tenant-id",
                "subscription-id",
            }
            assert job.get("permissions") == {
                "contents": "read",
                "id-token": "write",
            }
        else:
            assert "id-token" not in job.get("permissions", {})

    assert azure_jobs == {
        "plan-infrastructure",
        "provision",
        "plan-backup",
        "provision-backup",
        "build-push",
        "deploy",
    }


def test_deploy_workflow_never_splices_context_values_into_shell_source() -> None:
    workflow = yaml.safe_load(DEPLOY_WORKFLOW.read_text())

    for job_name, job in workflow["jobs"].items():
        for step in job.get("steps", []):
            run = step.get("run")
            if isinstance(run, str):
                assert "${{" not in run, (
                    f"{job_name}/{step.get('name', '<unnamed>')} must pass GitHub context "
                    "values through env before the shell parses them"
                )


def test_deploy_workflow_never_publishes_plaintext_terraform_plans() -> None:
    workflow = DEPLOY_WORKFLOW.read_text()

    assert "tfplan.txt" not in workflow
    assert "terraform show -no-color" not in workflow
    assert "tfplan.enc" in workflow
    assert workflow.count("seal_terraform_plan.py") == 2
    assert "terraform show -json tfplan" not in workflow
    assert "-in tfplan -out tfplan.enc" not in workflow
    assert "Terraform output $name is empty, oversized, or multiline" in workflow


def test_deploy_workflow_removes_stale_plan_files_before_every_plan() -> None:
    workflow = yaml.safe_load(DEPLOY_WORKFLOW.read_text())

    for job_name in ("plan-infrastructure", "plan-backup"):
        plan_steps = [
            step["run"]
            for step in workflow["jobs"][job_name]["steps"]
            if isinstance(step.get("run"), str) and "terraform plan" in step["run"]
        ]
        assert len(plan_steps) == 1
        script = plan_steps[0]
        assert script.index("rm -f tfplan tfplan.enc tfplan.sha256") < script.index(
            "terraform plan"
        )


def test_deploy_workflow_applies_anonymous_kernel_sealed_approved_plans() -> None:
    workflow = DEPLOY_WORKFLOW.read_text()

    assert workflow.count("apply_terraform_plan.py") == 2
    assert "exec {plan_fd}<tfplan" not in workflow
    assert "openssl enc -d" not in workflow
    assert "-out tfplan" not in workflow
    assert "terraform apply -input=false tfplan" not in workflow


def test_deploy_workflow_scopes_plan_passphrase_to_seal_and_apply_helpers() -> None:
    workflow = yaml.safe_load(DEPLOY_WORKFLOW.read_text())
    jobs = workflow["jobs"]
    protected_jobs = ("plan-infrastructure", "provision", "plan-backup", "provision-backup")

    for job_name in protected_jobs:
        job = jobs[job_name]
        assert 0 < int(job["timeout-minutes"]) <= 90
        step = next(
            candidate
            for candidate in job["steps"]
            if candidate.get("env", {}).get("TFPLAN_PASSPHRASE") is not None
        )
        script = step["run"]
        scrub = script.index("unset TFPLAN_PASSPHRASE")
        terraform = script.index("terraform ")
        helper = (
            "seal_terraform_plan.py" if job_name.startswith("plan-") else "apply_terraform_plan.py"
        )

        assert script.index('plan_passphrase="$TFPLAN_PASSPHRASE"') < scrub < terraform
        assert script.count('TFPLAN_PASSPHRASE="$plan_passphrase"') == 1
        assert script.index('TFPLAN_PASSPHRASE="$plan_passphrase"') < script.index(helper)
        assert script.index("unset plan_passphrase") > script.index(helper)


def test_ci_workflow_declares_a_read_only_repository_token() -> None:
    workflow = yaml.safe_load(CI_WORKFLOW.read_text())

    assert workflow["permissions"] == {"contents": "read"}


def test_every_javascript_delivery_gate_lints_the_shared_event_contract() -> None:
    workflows = (
        (CI_WORKFLOW, "dashboard"),
        (DEPLOY_WORKFLOW, "verify-dashboard"),
        (RELEASE_WORKFLOW, "dashboard-gates"),
    )

    for path, job_name in workflows:
        workflow = yaml.load(path.read_text(), Loader=yaml.BaseLoader)
        runs = [step.get("run", "") for step in workflow["jobs"][job_name]["steps"]]
        assert "npm run -w @apex/pipeline-events lint" in runs

    root_scripts = json.loads((REPO_ROOT / "package.json").read_text())["scripts"]
    contract_gate = root_scripts["check:contracts"]
    assert "npm run -w @apex/api-client typecheck" in contract_gate
    for command in ("typecheck", "lint", "test"):
        assert f"npm run -w @apex/pipeline-events {command}" in contract_gate
    assert "npm run check:contracts" in root_scripts["check"]


def test_manual_aks_deploy_requires_full_frozen_dashboard_gates() -> None:
    workflow = yaml.load(DEPLOY_WORKFLOW.read_text(), Loader=yaml.BaseLoader)
    jobs = workflow["jobs"]
    dashboard_gates = jobs["verify-dashboard"]

    runs = [step.get("run", "") for step in dashboard_gates["steps"]]
    expected_commands = {
        "npm ci",
        "npm audit --audit-level=high",
        "npm run -w @apex/pipeline-events typecheck",
        "npm run -w @apex/pipeline-events lint",
        "npm run -w @apex/pipeline-events test",
        "npm run -w @apex/api-client typecheck",
        "npm run -w @apex/dashboard typecheck",
        "npm run -w @apex/dashboard lint",
        "npm run -w @apex/dashboard test",
        "npm run -w @apex/dashboard test:coverage",
        "npm run -w @apex/dashboard build",
    }

    assert expected_commands <= set(runs)
    assert any(
        "npm run generate:sdks" in run
        and "git diff --exit-code packages/api-client/src/schema.d.ts" in run
        for run in runs
    )
    assert set(jobs["plan-infrastructure"]["needs"]) == {
        "verify",
        "verify-dashboard",
    }
    assert set(jobs["build-push"]["needs"]) == {
        "provision",
        "verify",
        "verify-dashboard",
    }


def test_manual_aks_deploy_requires_full_server_release_gates() -> None:
    workflow = yaml.load(DEPLOY_WORKFLOW.read_text(), Loader=yaml.BaseLoader)
    steps = workflow["jobs"]["verify"]["steps"]
    runs = [step.get("run", "") for step in steps]

    assert "uv run pytest --cov=src/apex --cov-report=term-missing --cov-fail-under=88" in runs
    assert "uv run pip-audit" in runs
    assert "uv run pytest tests/unit -q" not in runs
    assert any(
        "uv run python scripts/export_openapi.py" in run and "git diff --exit-code docs/api/" in run
        for run in runs
    )


def test_manual_aks_deploy_scans_both_images_before_push() -> None:
    workflow = yaml.load(DEPLOY_WORKFLOW.read_text(), Loader=yaml.BaseLoader)
    steps = workflow["jobs"]["build-push"]["steps"]
    build_index = next(
        index for index, step in enumerate(steps) if step.get("name") == "Build images"
    )
    push_index, push = next(
        (index, step)
        for index, step in enumerate(steps)
        if step.get("name") == "Push scanned images"
    )
    scans = [
        (index, step)
        for index, step in enumerate(steps)
        if str(step.get("uses", "")).startswith("aquasecurity/trivy-action@")
    ]

    assert len(scans) == 2
    assert all(build_index < index < push_index for index, _ in scans)
    assert "docker push" not in steps[build_index]["run"]
    assert "docker build" not in push["run"]
    assert "langgraph build" not in push["run"]
    assert push["id"] == "images"
    assert {scan["with"]["image-ref"] for _, scan in scans} == {
        "${{ needs.provision.outputs.acr }}/apex-orchestration-engine:sha-${{ github.sha }}",
        "${{ needs.provision.outputs.acr }}/apex-dashboard:sha-${{ github.sha }}",
    }
    for _, scan in scans:
        assert scan["uses"] == TRIVY_ACTION
        assert {key: value for key, value in scan["with"].items() if key != "image-ref"} == {
            "scan-type": "image",
            "version": "v0.70.0",
            "scanners": "vuln",
            "vuln-type": "os,library",
            "severity": "HIGH,CRITICAL",
            "ignore-unfixed": "false",
            "exit-code": "1",
            "format": "table",
        }


def test_bootstrap_secret_guard_covers_the_entire_pre_storage_document() -> None:
    helpers = HELM_HELPERS.read_text()
    validation = HELM_VALIDATION.read_text()
    workflow = CI_WORKFLOW.read_text()

    assert 'fail "bootstrap.document must be a map"' in validation
    assert '"value" .Values.bootstrap.document "label" "bootstrap.document"' in validation
    assert 'eq $normalizedKey "secretref"' in helpers
    assert "^env:[A-Za-z_][A-Za-z0-9_]{0,254}$" in helpers
    for secret_marker in (
        "privatekey",
        "sshkey",
        "signingkey",
        "encryptionkey",
        "bearer",
        "sharedkey",
        "clientcertificate",
        "privatepem",
        "signature",
        "cookie",
        "jwt",
        "pfx",
        "pkcs12",
        "connectionstring",
        "databaseuri",
        "postgresqlurl",
        "redisurl",
        "brokeruri",
        "amqpurl",
        "mongodburl",
        "dsn",
        "sas",
        "authentication",
    ):
        assert secret_marker in helpers

    assert "bootstrap-nested-secret-invalid.yaml" in workflow
    assert "bootstrap-unknown-secret-invalid.yaml" in workflow
    assert "bootstrap-scalar-invalid.yaml" in workflow
    assert "bootstrap-secret-ref-bool-invalid.yaml" in workflow
    assert "bootstrap-secret-ref-null-invalid.yaml" in workflow
    assert "bootstrap-secret-ref-map-invalid.yaml" in workflow
    assert "bootstrap-access-key-identifiers-valid.yaml" in workflow
    assert "bootstrap-noncredential-fields-valid.yaml" in workflow
    assert "bootstrap-terminal-credential-key-invalid.yaml" in workflow
    assert "bootstrap-credential-text-invalid.yaml" in workflow
    assert "bootstrap-benign-url-valid.yaml" in workflow
    assert "bootstrap-standalone-credential-signature-invalid.yaml" in workflow
    assert "bootstrap-private-key-block-invalid.yaml" in workflow
    assert "bootstrap-credential-signature-controls-valid.yaml" in workflow
    assert "$compactJwt := regexMatch" in helpers
    assert "$privateKeyBlock := regexMatch" in helpers
    assert "$providerToken := regexMatch" in helpers
    assert "$nonCredentialNames := list" in helpers
    assert "$separatedCredential := regexMatch" in helpers
    assert "$terminalCredential := regexMatch" in helpers
    assert "$wrappedCredential := false" in helpers
    assert "providerToken providerSignature providerTokenValue providerSignatureValue" in workflow
    for noncredential_field in (
        "authorship",
        "auth_mode",
        "authenticationType",
        "authenticationModeValue",
        "authenticationModeData",
        "metadata",
        "tokenCount",
        "tokenCountValue",
        "signatureAlgorithm",
        "signatureAlgorithmValue",
        "publicKey",
        "secretKeyRef",
        "secretKeyIdentifier",
        "access_key_id",
        "aws_access_key_id",
        "project_key",
        "monkey",
    ):
        assert f'"{noncredential_field}"' in workflow
    for signature_marker in (
        "eyJ[A-Za-z0-9_-]{8,}",
        "RSA|EC|DSA|OPENSSH|ENCRYPTED",
        "gh[pousr]_",
        "github_pat_",
        "glpat-",
        "xox[baprs]-",
        "xapp-",
        "[sr]k_(live|test)_",
        "sk-(proj-)?",
        "npm_",
        "pypi-",
        "hf_",
        "AIza",
        "ya29[.]",
        "SG[.]",
        "dckr_(pat|oat)_",
        "atlasv1",
        "AZDO",
    ):
        assert signature_marker in helpers


def test_langgraph_image_context_is_a_minimal_fail_closed_allowlist() -> None:
    patterns = [
        line.strip()
        for line in DOCKER_IGNORE.read_text().splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]

    assert patterns == [
        "**",
        "!pyproject.toml",
        "!README.md",
        "!uv.lock",
        "!langgraph.json",
        "!src/",
        "!src/**",
    ]

    # These are local/generated trees the LangGraph CLI will otherwise turn
    # into explicit ADD instructions whenever they happen to exist locally.
    for path in (".claude", ".langgraph_api", "output", ".env", ".git"):
        assert f"!{path}" not in patterns


def test_dashboard_image_has_an_independent_fail_closed_context() -> None:
    patterns = [
        line.strip()
        for line in DASHBOARD_DOCKER_IGNORE.read_text().splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]

    assert patterns[0] == "**"
    for required in (
        "!package.json",
        "!package-lock.json",
        "!apps/dashboard/package.json",
        "!apps/dashboard/src/**",
        "!packages/api-client/src/**",
        "!packages/pipeline-events/src/**",
    ):
        assert required in patterns
    for local_or_generated in (
        "!apps/dashboard/.env.development.local",
        "!apps/dashboard/dist/**",
        "!apps/dashboard/coverage/**",
        "!.env",
        "!.git/**",
    ):
        assert local_or_generated not in patterns


def test_release_workflow_limits_signing_permissions_to_artifact_jobs() -> None:
    workflow = yaml.safe_load(RELEASE_WORKFLOW.read_text())
    jobs = workflow["jobs"]
    signing_jobs: set[str] = set()

    assert workflow["permissions"] == {"contents": "read"}
    for job_name, job in jobs.items():
        steps = job.get("steps", [])
        requires_signing = any(
            str(step.get("uses", "")).startswith("actions/attest-build-provenance@")
            or "cosign " in str(step.get("run", ""))
            for step in steps
        )
        if requires_signing:
            signing_jobs.add(job_name)
            assert job.get("permissions") == {
                "attestations": "write",
                "contents": "read",
                "id-token": "write",
            }
        else:
            assert "id-token" not in job.get("permissions", {})
            assert "attestations" not in job.get("permissions", {})

    assert signing_jobs == {"server-image", "dashboard-image", "sdk", "helm-chart"}


def test_release_artifacts_wait_for_both_backend_and_dashboard_contract_gates() -> None:
    jobs = yaml.safe_load(RELEASE_WORKFLOW.read_text())["jobs"]

    for job_name in ("server-image", "dashboard-image", "sdk", "helm-chart"):
        needs = jobs[job_name]["needs"]
        assert set(needs) == {"gates", "dashboard-gates", "version"}, (
            f"{job_name} can publish before every release contract gate passes"
        )


def test_locked_database_aliases_reach_bootstrap_and_schema_readiness() -> None:
    helpers = HELM_HELPERS.read_text()
    bootstrap = HELM_BOOTSTRAP_JOB.read_text()
    deployment = HELM_DEPLOYMENT.read_text()
    validation = HELM_VALIDATION.read_text()
    values = HELM_VALUES.read_text()

    bootstrap_db = helpers.split('{{- define "apex.bootstrapDbEnv" -}}', 1)[1].split(
        "Non-secret settings for hook processes", 1
    )[0]
    assert "- name: DATABASE_URI" in bootstrap_db
    assert "- name: APEX_DATABASE__URI" in bootstrap_db
    assert ".Values.bootstrap.langgraphDatabaseSecret" in bootstrap_db
    assert ".Values.bootstrap.langgraphDatabaseKey" in bootstrap_db
    assert '{{- include "apex.hookSettingsEnv" . | nindent 12 }}' in bootstrap
    assert '{{- include "apex.bootstrapDbEnv" . | nindent 12 }}' in bootstrap
    assert '{{- include "apex.bootstrapRedisEnv" . | nindent 12 }}' in bootstrap
    assert '{{- include "apex.bootstrapAuthEnv" . | nindent 12 }}' in bootstrap

    schema_readiness = deployment.split("- name: schema-readiness", 1)[1].split("containers:", 1)[0]
    assert "- name: DATABASE_URI" in schema_readiness
    assert "- name: APEX_DATABASE__URI" in schema_readiness
    assert 'required "database.uriKey is required"' in schema_readiness

    assert "database.existingSecret is required in locked environments" in validation
    assert "database.uriKey is required for DATABASE_URI in locked environments" in validation
    assert (
        "database.apexUriKey is required for APEX_DATABASE__URI in locked environments"
        in validation
    )
    assert "redis.uriKey is required for REDIS_URI in locked environments" in validation
    assert (
        "objectName: database-uri, secretName: apex-database-bootstrap, key: DATABASE_URI" in values
    )


def test_infrastructure_tool_versions_are_strict_github_outputs() -> None:
    checksum = "95f14e87aa28c09d5941f11bd024c1d02fdc0303ccaa23f61cef67bc92619d73"
    assert TOOL_VERSIONS == {
        "terraform": "1.15.6",
        "helm": "4.2.1",
        "kubeconform": "0.6.7",
    }
    assert TOOL_CHECKSUMS == {"kubeconform_linux_amd64_sha256": checksum}
    assert github_outputs() == (
        "terraform=1.15.6\nhelm=4.2.1\nkubeconform=0.6.7\n"
        f"kubeconform_linux_amd64_sha256={checksum}"
    )

    invalid_versions = (
        {
            "terraform": "1.15.6\ncompromised=true",
            "helm": "4.2.1",
            "kubeconform": "0.6.7",
        },
        {"terraform": "latest", "helm": "4.2.1", "kubeconform": "0.6.7"},
        {"terraform": "1.15.6"},
        {
            "terraform": "1.15.6",
            "helm": "4.2.1",
            "kubeconform": "0.6.7",
            "other": "1.0.0",
        },
    )
    for versions in invalid_versions:
        try:
            github_outputs(versions)
        except ValueError:
            pass
        else:
            raise AssertionError(f"unsafe infrastructure tool versions accepted: {versions!r}")

    for checksums in (
        {},
        {"kubeconform_linux_amd64_sha256": "latest"},
        {"kubeconform_linux_amd64_sha256": f"{checksum}\ncompromised=true"},
        {"kubeconform_linux_amd64_sha256": checksum, "other": "a" * 64},
    ):
        with pytest.raises(ValueError):
            github_outputs(checksums=checksums)


def test_ci_and_release_verify_kubeconform_from_the_canonical_pin() -> None:
    workflows = (
        (yaml.load(CI_WORKFLOW.read_text(), Loader=yaml.BaseLoader), "helm-lint"),
        (yaml.load(RELEASE_WORKFLOW.read_text(), Loader=yaml.BaseLoader), "helm-chart"),
    )
    for workflow, job_name in workflows:
        steps = workflow["jobs"][job_name]["steps"]
        install = next(step for step in steps if step.get("name") == "Install kubeconform")
        assert install["env"] == {
            "KUBECONFORM_VERSION": "${{ steps.tool_versions.outputs.kubeconform }}",
            "KUBECONFORM_SHA256": (
                "${{ steps.tool_versions.outputs.kubeconform_linux_amd64_sha256 }}"
            ),
        }
        assert "sha256sum --check --strict" in install["run"]
        assert "v${KUBECONFORM_VERSION}" in install["run"]

    release_steps = workflows[1][0]["jobs"]["helm-chart"]["steps"]
    validation = next(
        step
        for step in release_steps
        if step.get("name") == "Helm lint, render, and schema-validate release variants"
    )["run"]
    assert '> "$out/default.yaml"' in validation
    assert '> "$out/azure.yaml"' in validation
    assert 'kubeconform -strict -ignore-missing-schemas -summary "$out"/*.yaml' in validation


def test_every_infrastructure_setup_action_uses_the_canonical_version_source() -> None:
    setup_actions = {
        "hashicorp/setup-terraform@": (
            "terraform_version",
            "${{ steps.tool_versions.outputs.terraform }}",
        ),
        "azure/setup-helm@": (
            "version",
            "${{ steps.tool_versions.outputs.helm }}",
        ),
    }
    counts = {prefix: 0 for prefix in setup_actions}

    for workflow_path in (CI_WORKFLOW, DEPLOY_WORKFLOW, RELEASE_WORKFLOW):
        workflow = yaml.safe_load(workflow_path.read_text())
        for job_name, job in workflow["jobs"].items():
            steps = job.get("steps", [])
            for index, step in enumerate(steps):
                uses = str(step.get("uses", ""))
                matches = [prefix for prefix in setup_actions if uses.startswith(prefix)]
                if not matches:
                    continue

                prefix = matches[0]
                input_name, expected_value = setup_actions[prefix]
                counts[prefix] += 1
                assert step.get("with", {}).get(input_name) == expected_value, (
                    f"{workflow_path.name}/{job_name} does not pin {prefix}"
                )

                loaders = [prior for prior in steps[:index] if prior.get("id") == "tool_versions"]
                assert len(loaders) == 1, (
                    f"{workflow_path.name}/{job_name} must load the canonical versions once"
                )
                assert (
                    loaders[0].get("run") == 'python3 scripts/tool_versions.py >> "$GITHUB_OUTPUT"'
                )

    assert counts == {
        "hashicorp/setup-terraform@": 5,
        "azure/setup-helm@": 3,
    }

"""Regressions for authenticated TLS in generated managed-service credentials."""

import hashlib
import hmac
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
KEYVAULT_TERRAFORM = REPO_ROOT / "deploy/terraform/keyvault.tf"
DATABASE_ROLE_JOB = (
    REPO_ROOT / "deploy/helm/apex-orchestration-engine/templates/database-role-job.yaml"
)
DATABASE_ROLE_CLEANUP_JOB = (
    REPO_ROOT / "deploy/helm/apex-orchestration-engine/templates/database-role-cleanup-job.yaml"
)
HELM_VALUES = REPO_ROOT / "deploy/helm/apex-orchestration-engine/values.yaml"
HELM_VALIDATION = REPO_ROOT / "deploy/helm/apex-orchestration-engine/templates/validate.yaml"
AZURE_VALUES = REPO_ROOT / "deploy/azure/helm/values-azure.yaml"
DEPLOYMENT_RUNBOOK = REPO_ROOT / "docs/runbooks/deployment.md"
TERRAFORM_README = REPO_ROOT / "deploy/terraform/README.md"
HELM_TEST = REPO_ROOT / "deploy/helm/apex-orchestration-engine/templates/tests/test-connection.yaml"
COMPOSE = REPO_ROOT / "docker-compose.yaml"
COMPOSE_HA = REPO_ROOT / "deploy/compose-ha/docker-compose.ha.yaml"
DEPLOY_WORKFLOW = REPO_ROOT / ".github/workflows/deploy-aks.yaml"
RELEASE_WORKFLOW = REPO_ROOT / ".github/workflows/release.yaml"
DASHBOARD_ENTRYPOINT = REPO_ROOT / "apps/dashboard/docker-entrypoint.sh"
DASHBOARD_VITE = REPO_ROOT / "apps/dashboard/vite.config.ts"
MIGRATION_JOB = REPO_ROOT / "deploy/helm/apex-orchestration-engine/templates/migration-job.yaml"
DATABASE_GRANTS_JOB = (
    REPO_ROOT / "deploy/helm/apex-orchestration-engine/templates/database-grants-job.yaml"
)
MIGRATION_RUNNER = REPO_ROOT / "src/apex/persistence/migrate.py"
MIGRATION_ENV = REPO_ROOT / "src/apex/persistence/migrations/env.py"


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
        assert "WITH RECURSIVE" not in source
    assert 'return await conn.fetchval("SELECT quote_literal($1)", value)' in provision
    assert "COMMENT ON ROLE {owner} IS {marker_literal}" in provision
    assert "COMMENT ON ROLE {role} IS {marker_literal}" in provision
    assert "parents != {expected_parent}" in provision
    assert "current generation role is not a direct member" in cleanup


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
        "finally_compatible = await _schema_is_compatible_async()",
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

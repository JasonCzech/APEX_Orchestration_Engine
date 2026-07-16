"""Regression checks for migration ordering and self-contained revisions."""

import hashlib
import importlib
import inspect
import subprocess
import sys
from pathlib import Path

from apex.persistence.audit_lock import AUDIT_CHAIN_LOCK_KEY
from apex.persistence.database_role_claims import DATABASE_ROLE_LOCK_KEY

REPO_ROOT = Path(__file__).resolve().parents[2]
PUBLISHED_MIGRATION_DIGESTS = {
    "0013_scope_and_replay_integrity.py": (
        "251532e873d7b866f7d99595af13c62c327f452b108ca868f64e9373f803400c"
    ),
    "0014_audit_chain_seq_default.py": (
        "d07cbae63420dff5b6b7acbd9e8719494644ce4308efee8004e6578da411824f"
    ),
    "0015_artifact_references.py": (
        "444ad464a1925a01ff68390dbdcacb1b019db81cbf4f7e83b05de8175ef91f5b"
    ),
}
migration_0016 = importlib.import_module(
    "apex.persistence.migrations.versions.0016_durable_reference_hardening"
)
migration_0022 = importlib.import_module(
    "apex.persistence.migrations.versions.0022_legacy_audit_writer_compatibility"
)
migration_0017 = importlib.import_module(
    "apex.persistence.migrations.versions.0017_independent_consumer_key_expiry"
)
migration_0018 = importlib.import_module(
    "apex.persistence.migrations.versions.0018_document_deletion_tombstones"
)
migration_0023 = importlib.import_module(
    "apex.persistence.migrations.versions.0023_work_item_mutations"
)
migration_0024 = importlib.import_module(
    "apex.persistence.migrations.versions.0024_document_cleanup_retry"
)
migration_0025 = importlib.import_module(
    "apex.persistence.migrations.versions.0025_artifact_content_identity"
)
migration_0026 = importlib.import_module(
    "apex.persistence.migrations.versions.0026_connection_runtime_version"
)
migration_0027 = importlib.import_module(
    "apex.persistence.migrations.versions.0027_engine_completion_witness"
)
migration_0028 = importlib.import_module(
    "apex.persistence.migrations.versions.0028_artifact_ownership_provenance"
)


def test_offline_upgrade_locks_audit_chain_before_revision_table_work() -> None:
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head", "--sql"],
        cwd=REPO_ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    sql = result.stdout
    lock_sql = f"SELECT pg_advisory_xact_lock({AUDIT_CHAIN_LOCK_KEY})"

    assert sql.index(lock_sql) < sql.index("CREATE TABLE apex.api_consumers")


def test_offline_upgrade_serializes_before_schema_bootstrap() -> None:
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head", "--sql"],
        cwd=REPO_ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    sql = result.stdout
    lock_sql = f"SELECT pg_advisory_xact_lock({DATABASE_ROLE_LOCK_KEY})"

    assert sql.index("BEGIN;") < sql.index(lock_sql)
    assert sql.index(lock_sql) < sql.index("IF to_regnamespace('apex') IS NULL THEN")


def test_offline_upgrade_checks_schema_before_executing_create() -> None:
    """An existing least-privilege schema owner must not need database CREATE."""

    result = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head", "--sql"],
        cwd=REPO_ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    sql = result.stdout

    assert "IF to_regnamespace('apex') IS NULL THEN" in sql
    assert "EXECUTE 'CREATE SCHEMA apex'" in sql
    assert "CREATE SCHEMA IF NOT EXISTS apex" not in sql


def test_offline_upgrade_registers_trusted_revision_lineage_before_migrations() -> None:
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head", "--sql"],
        cwd=REPO_ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    sql = result.stdout

    assert "CREATE TABLE IF NOT EXISTS apex.alembic_revision_lineage" in sql
    assert "VALUES ('0025', '0024')" in sql
    assert "VALUES ('0026', '0025')" in sql
    assert "VALUES ('0027', '0026')" in sql
    assert "VALUES ('0028', '0027')" in sql
    assert sql.index("alembic_revision_lineage") < sql.index("CREATE TABLE apex.api_consumers")


def test_published_0016_lock_literal_matches_runtime_constant() -> None:
    # Revisions stay self-contained while this parity check prevents the lock
    # identity from drifting away from runtime appenders and env.py.
    assert str(AUDIT_CHAIN_LOCK_KEY) in inspect.getsource(migration_0016.upgrade)


def test_0016_downgrade_preserves_the_schema_and_guards_owned_by_0015() -> None:
    """A database stamped back to 0015 must still match published 0015 exactly."""

    source = inspect.getsource(migration_0016.downgrade)

    assert "op.execute" not in source
    assert "op.alter_column" not in source
    assert "op.drop_constraint" not in source


def test_published_migrations_remain_byte_for_byte_immutable() -> None:
    versions = REPO_ROOT / "src/apex/persistence/migrations/versions"
    for filename, expected in PUBLISHED_MIGRATION_DIGESTS.items():
        assert hashlib.sha256((versions / filename).read_bytes()).hexdigest() == expected


def test_0022_keeps_legacy_audit_writers_compatible_with_nonnull_sequence() -> None:
    upgrade_source = inspect.getsource(migration_0022.upgrade)
    downgrade_source = inspect.getsource(migration_0022.downgrade)

    assert "IF NEW.chain_seq IS NULL" in upgrade_source
    assert migration_0022._AUDIT_CHAIN_LOCK_KEY == AUDIT_CHAIN_LOCK_KEY
    assert "DROP TRIGGER IF EXISTS trg_assign_audit_chain_seq" in downgrade_source
    assert "DROP FUNCTION IF EXISTS apex.assign_audit_chain_seq()" in downgrade_source


def test_0017_preserves_possibly_explicit_rotated_key_deadlines() -> None:
    source = inspect.getsource(migration_0017.upgrade)

    assert "COALESCE(consumer.rotation_count, 0) = 0" in source


def test_0018_downgrade_refuses_to_resurrect_pending_document_deletions() -> None:
    source = inspect.getsource(migration_0018.downgrade)

    assert "deletion_pending_at IS NOT NULL" in source
    assert "cannot downgrade with pending document deletions" in source


def test_0023_pins_live_mutations_and_preserves_compact_retired_key_claims() -> None:
    upgrade_source = inspect.getsource(migration_0023.upgrade)
    downgrade_source = inspect.getsource(migration_0023.downgrade)

    assert migration_0023.down_revision == "0022"
    assert 'sa.Column("tenant_scope", sa.String(length=64)' in upgrade_source
    assert 'sa.Column("connection_version", sa.DateTime(timezone=True)' in upgrade_source
    assert '"payload",' in upgrade_source and "postgresql.JSONB" in upgrade_source
    assert 'ondelete="RESTRICT"' in upgrade_source
    assert '"work_item_mutation_tombstones"' in upgrade_source
    assert "work_item_mutation_tombstones" in downgrade_source
    assert "cannot downgrade with work-item idempotency records present" in downgrade_source


def test_0024_adds_bounded_document_cleanup_retry_state() -> None:
    upgrade_source = inspect.getsource(migration_0024.upgrade)
    downgrade_source = inspect.getsource(migration_0024.downgrade)

    assert migration_0024.down_revision == "0023"
    assert '"cleanup_retry_at"' in upgrade_source
    assert '"cleanup_attempt_count"' in upgrade_source
    assert '"cleanup_last_error"' in upgrade_source
    assert '"ix_documents_cleanup_retry"' in upgrade_source
    assert "ix_documents_cleanup_retry" in downgrade_source


def test_0025_adds_exact_artifact_content_identity_without_guessing_legacy_bytes() -> None:
    upgrade_source = inspect.getsource(migration_0025.upgrade)
    downgrade_source = inspect.getsource(migration_0025.downgrade)

    assert migration_0025.down_revision == "0024"
    assert '"content_sha256"' in upgrade_source
    assert '"size_bytes"' in upgrade_source
    assert '"content_type"' in upgrade_source
    assert upgrade_source.count("nullable=True") == 3
    assert "content_sha256" in downgrade_source


def test_0026_separates_runtime_generation_and_backfills_exactly() -> None:
    upgrade_source = inspect.getsource(migration_0026.upgrade)
    downgrade_source = inspect.getsource(migration_0026.downgrade)

    assert migration_0026.down_revision == "0025"
    assert '"runtime_version"' in upgrade_source
    assert "SET runtime_version = updated_at" in upgrade_source
    assert "nullable=False" in upgrade_source
    assert "SET updated_at = runtime_version" in downgrade_source


def test_0027_adds_post_effect_engine_completion_witness() -> None:
    upgrade_source = inspect.getsource(migration_0027.upgrade)
    downgrade_source = inspect.getsource(migration_0027.downgrade)

    assert migration_0027.down_revision == "0026"
    assert '"execution_connection_version"' in upgrade_source
    assert '"artifact_connection_version"' in upgrade_source
    assert '"completion_kind"' in upgrade_source
    assert "execution_connection_version" in downgrade_source


def test_0028_quarantines_ambiguous_run_and_artifact_ownership() -> None:
    upgrade_source = inspect.getsource(migration_0028.upgrade)
    helper_source = inspect.getsource(migration_0028._add_provenance_column)
    downgrade_source = inspect.getsource(migration_0028.downgrade)

    assert migration_0028.down_revision == "0027"
    assert "UPDATE apex.engine_runs" in upgrade_source
    assert "SET ownership_known = false" in upgrade_source
    assert "WHERE app_id IS NULL" in upgrade_source
    assert '_add_provenance_column("artifact_references")' in upgrade_source
    assert '_add_provenance_column("artifact_upload_intents")' in upgrade_source
    assert 'server_default=sa.text("false")' in helper_source
    assert "project_id IS NOT NULL" in helper_source
    assert "app_id IS NOT NULL" in helper_source
    assert 'server_default=sa.text("true")' not in helper_source
    assert '"scope_ownership_known"' in upgrade_source
    assert 'server_default=sa.text("false")' in upgrade_source
    assert 'drop_column("engine_runs", "scope_ownership_known"' in downgrade_source
    assert "SET ownership_known = true" not in downgrade_source


def test_0028_orm_provenance_columns_are_nonnullable_and_default_quarantined() -> None:
    from apex.persistence.models import ArtifactReference, ArtifactUploadIntent, EngineRun

    for model in (ArtifactReference, ArtifactUploadIntent):
        column = model.__table__.c.ownership_known
        assert column.nullable is False
        assert str(column.server_default.arg) == "false"
    engine_column = EngineRun.__table__.c.scope_ownership_known
    assert engine_column.nullable is False
    assert str(engine_column.server_default.arg) == "false"

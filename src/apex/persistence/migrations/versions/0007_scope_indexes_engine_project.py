"""Add engine-run project ownership and hot-path indexes.

Revision ID: 0007
Revises: 0006
Create Date: 2026-06-24
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0007"
down_revision: str | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _is_postgres() -> bool:
    return op.get_bind().dialect.name == "postgresql"


def _create_index(
    name: str,
    table_name: str,
    columns: list[str],
    *,
    unique: bool = False,
    postgresql_where: sa.TextClause | None = None,
) -> None:
    if postgresql_where is not None and not _is_postgres():
        return
    # Repairs invalid indexes left by the old concurrent/autocommit form if an
    # interrupted revision is retried.
    op.drop_index(name, table_name=table_name, schema="apex", if_exists=True)
    op.create_index(
        name,
        table_name,
        columns,
        schema="apex",
        unique=unique,
        postgresql_where=postgresql_where,
    )


def _drop_index(name: str, table_name: str, *, postgresql_where: bool = False) -> None:
    if postgresql_where and not _is_postgres():
        return
    op.drop_index(
        name,
        table_name=table_name,
        schema="apex",
        if_exists=True,
    )


def upgrade() -> None:
    op.add_column(
        "engine_runs",
        sa.Column("project_id", sa.String(length=255)),
        schema="apex",
        if_not_exists=True,
    )

    _create_index("ix_consumer_scopes_consumer_id", "consumer_scopes", ["consumer_id"])
    _create_index("ix_consumer_scopes_project_app", "consumer_scopes", ["project_id", "app_id"])
    _create_index(
        "uq_consumer_scopes_consumer_project_no_app",
        "consumer_scopes",
        ["consumer_id", "project_id"],
        unique=True,
        postgresql_where=sa.text("app_id IS NULL"),
    )
    _create_index("ix_prompts_active_version_id", "prompts", ["active_version_id"])
    _create_index("ix_prompt_versions_prompt_id", "prompt_versions", ["prompt_id"])
    _create_index("ix_applications_project_id", "applications", ["project_id"])
    _create_index("ix_environments_application_id", "environments", ["application_id"])
    _create_index("ix_environment_hosts_environment_id", "environment_hosts", ["environment_id"])
    _create_index(
        "ix_environment_snapshots_environment_scanned",
        "environment_snapshots",
        ["environment_id", "scanned_at"],
    )
    _create_index("ix_connections_project_id", "connections", ["project_id"])
    _create_index(
        "ix_connections_kind_project_enabled",
        "connections",
        ["kind", "project_id", "enabled"],
    )
    _create_index("ix_host_mappings_connection_id", "host_mappings", ["connection_id"])
    _create_index("ix_documents_artifact_key", "documents", ["artifact_key"])
    _create_index("ix_documents_project_created", "documents", ["project_id", "created_at"])
    _create_index("ix_documents_created_at", "documents", ["created_at"])
    _create_index("ix_saved_queries_project_id", "saved_queries", ["project_id"])
    _create_index("ix_engine_runs_project_started", "engine_runs", ["project_id", "started_at"])
    _create_index("ix_engine_runs_status_started", "engine_runs", ["status", "started_at"])
    _create_index("ix_engine_runs_engine_started", "engine_runs", ["engine", "started_at"])
    _create_index("ix_engine_runs_external_run_id", "engine_runs", ["external_run_id"])
    _create_index("ix_drafts_project_id", "drafts", ["project_id"])


def downgrade() -> None:
    _drop_index("ix_drafts_project_id", "drafts")
    _drop_index("ix_engine_runs_external_run_id", "engine_runs")
    _drop_index("ix_engine_runs_engine_started", "engine_runs")
    _drop_index("ix_engine_runs_status_started", "engine_runs")
    _drop_index("ix_engine_runs_project_started", "engine_runs")
    _drop_index("ix_saved_queries_project_id", "saved_queries")
    _drop_index("ix_documents_created_at", "documents")
    _drop_index("ix_documents_project_created", "documents")
    _drop_index("ix_documents_artifact_key", "documents")
    _drop_index("ix_host_mappings_connection_id", "host_mappings")
    _drop_index("ix_connections_kind_project_enabled", "connections")
    _drop_index("ix_connections_project_id", "connections")
    _drop_index("ix_environment_snapshots_environment_scanned", "environment_snapshots")
    _drop_index("ix_environment_hosts_environment_id", "environment_hosts")
    _drop_index("ix_environments_application_id", "environments")
    _drop_index("ix_applications_project_id", "applications")
    _drop_index("ix_prompt_versions_prompt_id", "prompt_versions")
    _drop_index("ix_prompts_active_version_id", "prompts")
    _drop_index(
        "uq_consumer_scopes_consumer_project_no_app",
        "consumer_scopes",
        postgresql_where=True,
    )
    _drop_index("ix_consumer_scopes_project_app", "consumer_scopes")
    _drop_index("ix_consumer_scopes_consumer_id", "consumer_scopes")

    op.drop_column("engine_runs", "project_id", schema="apex")

"""app-aware run ownership and replay integrity

Revision ID: 0013
Revises: 0012
Create Date: 2026-07-14
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0013"
down_revision: str | None = "0012"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _is_postgres() -> bool:
    return op.get_bind().dialect.name == "postgresql"


def upgrade() -> None:
    op.add_column(
        "environments",
        sa.Column(
            "target_approved",
            sa.Boolean(),
            server_default=sa.text("false"),
            nullable=False,
        ),
        schema="apex",
    )
    op.add_column(
        "environments",
        sa.Column(
            "target_version",
            sa.Integer(),
            server_default="0",
            nullable=False,
        ),
        schema="apex",
    )
    op.add_column(
        "audit_log",
        sa.Column("chain_seq", sa.BigInteger(), nullable=True),
        schema="apex",
    )
    # Preserve the ordering used by audit verification before this migration.
    # Hashes are intentionally not rewritten: the sequence is an ordering and
    # continuity guard layered around the existing tamper-evident hash chain.
    op.execute(
        sa.text(
            """
            WITH numbered AS (
                SELECT id, row_number() OVER (ORDER BY at, id) AS chain_seq
                FROM apex.audit_log
            )
            UPDATE apex.audit_log AS audit
            SET chain_seq = numbered.chain_seq
            FROM numbered
            WHERE audit.id = numbered.id
            """
        )
    )
    # Keep this column nullable through the rolling writer window. Older pods
    # do not know about chain_seq and must be able to insert rows while the new
    # application version is being rolled out. New writers populate it and the
    # uniqueness constraint remains effective for non-null values; a later
    # migration can enforce NOT NULL once old writers are gone.
    op.create_unique_constraint(
        "uq_audit_log_chain_seq",
        "audit_log",
        ["chain_seq"],
        schema="apex",
    )
    op.add_column(
        "engine_runs",
        sa.Column("app_id", sa.String(length=255), nullable=True),
        schema="apex",
    )
    op.add_column(
        "engine_runs",
        sa.Column(
            "ownership_known",
            sa.Boolean(),
            server_default=sa.text("false"),
            nullable=False,
        ),
        schema="apex",
    )
    # Existing rows retain false; new raw inserts default to known ownership.
    op.alter_column(
        "engine_runs",
        "ownership_known",
        server_default=sa.text("true"),
        schema="apex",
    )
    op.add_column(
        "engine_runs",
        sa.Column("artifact_namespace", sa.String(length=512), nullable=True),
        schema="apex",
    )
    op.add_column(
        "engine_runs",
        sa.Column("artifact_connection_id", sa.String(length=255), nullable=True),
        schema="apex",
    )
    op.add_column(
        "documents",
        sa.Column("artifact_connection_id", sa.String(length=255), nullable=True),
        schema="apex",
    )
    op.add_column(
        "drafts",
        sa.Column("created_by_consumer_id", sa.String(length=32), nullable=True),
        schema="apex",
    )
    op.add_column(
        "usage_events",
        sa.Column("event_key", sa.String(length=512), nullable=True),
        schema="apex",
    )
    op.add_column(
        "usage_events",
        sa.Column("app_id", sa.String(length=255), nullable=True),
        schema="apex",
    )
    op.add_column(
        "agent_events",
        sa.Column("event_key", sa.String(length=512), nullable=True),
        schema="apex",
    )
    op.add_column(
        "agent_events",
        sa.Column("app_id", sa.String(length=255), nullable=True),
        schema="apex",
    )

    if _is_postgres():
        # app_id, artifact-store connection ownership, and the opaque artifact
        # namespace cannot be reconstructed safely for legacy rows.
        # PostgreSQL's ordinary UNIQUE(project_id, name) treats every NULL as
        # distinct. Refuse to silently discard/rename existing operator queries.
        op.execute(
            """
            DO $$
            BEGIN
              IF EXISTS (
                SELECT 1
                FROM apex.saved_queries
                WHERE project_id IS NULL
                GROUP BY name
                HAVING count(*) > 1
              ) THEN
                RAISE EXCEPTION
                  'duplicate global saved-query names must be resolved before migration 0013';
              END IF;
            END
            $$
            """
        )

    op.create_unique_constraint(
        "uq_engine_runs_artifact_namespace",
        "engine_runs",
        ["artifact_namespace"],
        schema="apex",
    )
    op.create_unique_constraint(
        "uq_usage_events_event_key",
        "usage_events",
        ["event_key"],
        schema="apex",
    )
    op.create_unique_constraint(
        "uq_agent_events_event_key",
        "agent_events",
        ["event_key"],
        schema="apex",
    )
    op.create_index(
        "ix_engine_runs_project_app_started",
        "engine_runs",
        ["project_id", "app_id", "started_at"],
        schema="apex",
    )
    op.create_index(
        "ix_usage_events_project_app_at",
        "usage_events",
        ["project_id", "app_id", "at"],
        schema="apex",
    )
    op.create_index(
        "ix_agent_events_project_app_at",
        "agent_events",
        ["project_id", "app_id", "at"],
        schema="apex",
    )
    op.create_index(
        "ix_drafts_created_by_consumer_id",
        "drafts",
        ["created_by_consumer_id"],
        schema="apex",
    )
    op.create_index(
        "uq_saved_queries_global_name",
        "saved_queries",
        ["name"],
        schema="apex",
        unique=True,
        postgresql_where=sa.text("project_id IS NULL"),
        sqlite_where=sa.text("project_id IS NULL"),
    )


def downgrade() -> None:
    op.drop_index("uq_saved_queries_global_name", table_name="saved_queries", schema="apex")
    op.drop_index("ix_drafts_created_by_consumer_id", table_name="drafts", schema="apex")
    op.drop_index("ix_engine_runs_project_app_started", table_name="engine_runs", schema="apex")
    op.drop_index("ix_agent_events_project_app_at", table_name="agent_events", schema="apex")
    op.drop_index("ix_usage_events_project_app_at", table_name="usage_events", schema="apex")
    op.drop_constraint("uq_agent_events_event_key", "agent_events", schema="apex", type_="unique")
    op.drop_constraint("uq_usage_events_event_key", "usage_events", schema="apex", type_="unique")
    op.drop_constraint(
        "uq_engine_runs_artifact_namespace",
        "engine_runs",
        schema="apex",
        type_="unique",
    )
    op.drop_column("agent_events", "app_id", schema="apex")
    op.drop_column("agent_events", "event_key", schema="apex")
    op.drop_column("usage_events", "app_id", schema="apex")
    op.drop_column("usage_events", "event_key", schema="apex")
    op.drop_column("drafts", "created_by_consumer_id", schema="apex")
    op.drop_column("documents", "artifact_connection_id", schema="apex")
    op.drop_column("engine_runs", "artifact_connection_id", schema="apex")
    op.drop_column("engine_runs", "artifact_namespace", schema="apex")
    op.drop_column("engine_runs", "ownership_known", schema="apex")
    op.drop_column("engine_runs", "app_id", schema="apex")
    op.drop_constraint("uq_audit_log_chain_seq", "audit_log", schema="apex", type_="unique")
    op.drop_column("audit_log", "chain_seq", schema="apex")
    op.drop_column("environments", "target_version", schema="apex")
    op.drop_column("environments", "target_approved", schema="apex")

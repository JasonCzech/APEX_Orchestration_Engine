"""Index durable artifact references and retire the audit sequence default."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0015"
down_revision: str | None = "0014"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "engine_runs",
        sa.Column("connection_id", sa.String(length=32), nullable=True),
        schema="apex",
    )
    if op.get_bind().dialect.name == "postgresql":
        op.create_foreign_key(
            "fk_engine_runs_connections_connection_id",
            "engine_runs",
            "connections",
            ["connection_id"],
            ["id"],
            source_schema="apex",
            referent_schema="apex",
            ondelete="RESTRICT",
        )
        op.execute(
            """
            UPDATE apex.engine_runs AS run
            SET connection_id = connection.id
            FROM apex.connections AS connection
            WHERE run.status NOT IN ('completed', 'failed', 'aborted')
              AND run.handle->>'connection_id' = connection.id
            """
        )
    op.create_index("ix_engine_runs_connection_id", "engine_runs", ["connection_id"], schema="apex")
    op.create_table(
        "artifact_references",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("artifact_key", sa.String(length=1024), nullable=False),
        sa.Column("connection_id", sa.String(length=32), nullable=False),
        sa.Column("kind", sa.String(length=64), nullable=False),
        sa.Column("thread_id", sa.String(length=255), nullable=False),
        sa.Column("project_id", sa.String(length=255), nullable=True),
        sa.Column("app_id", sa.String(length=255), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(["connection_id"], ["apex.connections.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("artifact_key"),
        schema="apex",
    )
    op.create_index(
        "ix_artifact_references_connection_id",
        "artifact_references",
        ["connection_id"],
        schema="apex",
    )
    op.create_index(
        "ix_artifact_references_thread_id",
        "artifact_references",
        ["thread_id"],
        schema="apex",
    )
    if op.get_bind().dialect.name == "postgresql":
        # Enforce new artifact ownership writes while allowing an upgrade to
        # retain legacy rows whose store identity could not be reconstructed.
        op.execute(
            """
            ALTER TABLE apex.documents
            ADD CONSTRAINT fk_documents_connections_artifact_connection_id
            FOREIGN KEY (artifact_connection_id) REFERENCES apex.connections(id)
            ON DELETE RESTRICT NOT VALID
            """
        )
        op.execute(
            """
            ALTER TABLE apex.engine_runs
            ADD CONSTRAINT fk_engine_runs_connections_artifact_connection_id
            FOREIGN KEY (artifact_connection_id) REFERENCES apex.connections(id)
            ON DELETE RESTRICT NOT VALID
            """
        )
        # Checkpoints created before the durable artifact index cannot be
        # enumerated safely. Conservatively protect every pre-existing store
        # until an operator reconciles it and removes this sentinel reference.
        op.execute(
            """
            INSERT INTO apex.artifact_references
                (id, artifact_key, connection_id, kind, thread_id)
            SELECT md5('legacy-artifact-store:' || id),
                   'legacy-connection-protection/' || id,
                   id,
                   'legacy_connection_guard',
                   'legacy'
            FROM apex.connections
            WHERE kind = 'artifact_store'
            ON CONFLICT (artifact_key) DO NOTHING
            """
        )
        # Serialize the chain repair with AuditService writers for the entire
        # migration transaction ("APEXAUDI" signed 64-bit advisory key).
        op.execute("SELECT pg_advisory_xact_lock(4706337855957713993)")
        # Some deployments may already have run the earlier form of 0014,
        # which reordered chain_seq by wall-clock time. Recover the original
        # order from the tamper-evident previous_hash links, but only when the
        # remaining rows form exactly one complete chain segment.
        op.execute(
            """
            DO $$
            DECLARE
                total_rows bigint;
                linked_rows bigint;
                distinct_depths bigint;
                base_seq bigint;
                shift_by bigint;
            BEGIN
                SELECT count(*), COALESCE(min(chain_seq), 1),
                       COALESCE(max(chain_seq), 0) + count(*) + 1
                INTO total_rows, base_seq, shift_by
                FROM apex.audit_log;

                WITH RECURSIVE linked AS (
                    SELECT row.id, row.event_hash, 1::bigint AS depth
                    FROM apex.audit_log AS row
                    WHERE NOT EXISTS (
                        SELECT 1 FROM apex.audit_log AS predecessor
                        WHERE predecessor.event_hash = row.previous_hash
                    )
                    UNION ALL
                    SELECT successor.id, successor.event_hash, linked.depth + 1
                    FROM apex.audit_log AS successor
                    JOIN linked ON successor.previous_hash = linked.event_hash
                )
                SELECT count(*), count(DISTINCT depth)
                INTO linked_rows, distinct_depths
                FROM linked;

                IF total_rows > 0
                   AND linked_rows = total_rows
                   AND distinct_depths = total_rows THEN
                    UPDATE apex.audit_log SET chain_seq = chain_seq + shift_by;
                    WITH RECURSIVE linked AS (
                        SELECT row.id, row.event_hash, 1::bigint AS depth
                        FROM apex.audit_log AS row
                        WHERE NOT EXISTS (
                            SELECT 1 FROM apex.audit_log AS predecessor
                            WHERE predecessor.event_hash = row.previous_hash
                        )
                        UNION ALL
                        SELECT successor.id, successor.event_hash, linked.depth + 1
                        FROM apex.audit_log AS successor
                        JOIN linked ON successor.previous_hash = linked.event_hash
                    )
                    UPDATE apex.audit_log AS row
                    SET chain_seq = base_seq + linked.depth - 1
                    FROM linked
                    WHERE row.id = linked.id;
                END IF;
            END $$;
            """
        )
        op.alter_column("audit_log", "chain_seq", server_default=None, schema="apex")
        op.execute("DROP SEQUENCE IF EXISTS apex.audit_chain_seq_seq")


def downgrade() -> None:
    if op.get_bind().dialect.name == "postgresql":
        op.drop_constraint(
            "fk_engine_runs_connections_artifact_connection_id",
            "engine_runs",
            schema="apex",
            type_="foreignkey",
        )
        op.drop_constraint(
            "fk_documents_connections_artifact_connection_id",
            "documents",
            schema="apex",
            type_="foreignkey",
        )
    op.drop_index(
        "ix_artifact_references_thread_id", table_name="artifact_references", schema="apex"
    )
    op.drop_index(
        "ix_artifact_references_connection_id",
        table_name="artifact_references",
        schema="apex",
    )
    op.drop_table("artifact_references", schema="apex")
    op.drop_index("ix_engine_runs_connection_id", table_name="engine_runs", schema="apex")
    if op.get_bind().dialect.name == "postgresql":
        op.drop_constraint(
            "fk_engine_runs_connections_connection_id",
            "engine_runs",
            schema="apex",
            type_="foreignkey",
        )
    op.drop_column("engine_runs", "connection_id", schema="apex")

"""Harden durable connection references after the published 0015 revision."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0016"
down_revision: str | None = "0015"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return

    # Defense in depth for callers that invoke this revision outside the normal
    # Alembic env.  Keep the published revision self-contained: this literal is
    # "APEXAUDI" as a signed int64 and a parity test pins it to the runtime
    # constant.  The env acquires it before *any* revision work so 0015 cannot
    # invert its table-lock order with a runtime audit appender.
    op.execute("SELECT pg_advisory_xact_lock(4706337855957713993)")

    # Published 0015 created this column as nullable. Backfill defensively before
    # aligning it with the non-nullable ORM model.
    op.execute("UPDATE apex.artifact_references SET created_at = now() WHERE created_at IS NULL")
    op.alter_column(
        "artifact_references",
        "created_at",
        existing_type=sa.DateTime(timezone=True),
        existing_server_default=sa.func.now(),
        nullable=False,
        schema="apex",
    )

    # NOT VALID retains legacy rows whose connection identity cannot be repaired,
    # while PostgreSQL still enforces each new insert/update and parent deletion.
    # The guards make this revision safe if an image containing the briefly edited
    # form of 0015 reached a database before this follow-up revision was added.
    op.execute(
        """
        DO $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1
                FROM pg_constraint
                WHERE conname = 'fk_documents_connections_artifact_connection_id'
                  AND conrelid = 'apex.documents'::regclass
            ) THEN
                ALTER TABLE apex.documents
                ADD CONSTRAINT fk_documents_connections_artifact_connection_id
                FOREIGN KEY (artifact_connection_id) REFERENCES apex.connections(id)
                ON DELETE RESTRICT NOT VALID;
            END IF;
        END $$
        """
    )
    op.execute(
        """
        DO $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1
                FROM pg_constraint
                WHERE conname = 'fk_engine_runs_connections_artifact_connection_id'
                  AND conrelid = 'apex.engine_runs'::regclass
            ) THEN
                ALTER TABLE apex.engine_runs
                ADD CONSTRAINT fk_engine_runs_connections_artifact_connection_id
                FOREIGN KEY (artifact_connection_id) REFERENCES apex.connections(id)
                ON DELETE RESTRICT NOT VALID;
            END IF;
        END $$
        """
    )

    # Pre-index checkpoints cannot be enumerated safely. Protect every store that
    # existed during the upgrade until an operator explicitly reconciles it.
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

    # Re-run the published 0015 repair under the audit writer lock. This is safe
    # both when 0015 already repaired the chain and when its unlocked repair raced
    # an append.
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
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return

    # Every durable object repaired above is already owned by the published
    # 0015 revision: both affinity foreign keys, the non-null created_at
    # contract, and the conservative legacy store guards.  Downgrading to 0015
    # must therefore preserve them byte-for-byte.  Removing them here leaves a
    # database stamped at 0015 with a schema that 0015 never produced, drops
    # active deletion guards, and makes the subsequent 0015 downgrade fail when
    # it tries to remove constraints that no longer exist.
    return

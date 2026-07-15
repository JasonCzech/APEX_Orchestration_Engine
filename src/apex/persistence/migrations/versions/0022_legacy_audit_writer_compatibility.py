"""Assign audit chain sequences for writers from before revision 0013.

Revision ID: 0022
Revises: 0021
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0022"
down_revision: str | None = "0021"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Keep the revision self-contained while matching legacy and current writers.
_AUDIT_CHAIN_LOCK_KEY = 0x4150455841554449


def upgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    # Alembic exposes the full upgrade transaction atomically, so a deployment
    # coming from before 0013 publishes this trigger together with chain_seq's
    # NOT NULL contract. Legacy pods omit the column but already acquire this
    # same advisory lock before calculating previous_hash. The trigger remains
    # harmless for current writers because they supply an explicit sequence.
    op.execute(
        f"""
        CREATE FUNCTION apex.assign_audit_chain_seq()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $audit_chain_seq$
        BEGIN
            IF NEW.chain_seq IS NULL THEN
                PERFORM pg_advisory_xact_lock({_AUDIT_CHAIN_LOCK_KEY});
                SELECT COALESCE(MAX(chain_seq), 0) + 1
                INTO NEW.chain_seq
                FROM apex.audit_log;
            END IF;
            RETURN NEW;
        END
        $audit_chain_seq$
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_assign_audit_chain_seq
        BEFORE INSERT ON apex.audit_log
        FOR EACH ROW
        EXECUTE FUNCTION apex.assign_audit_chain_seq()
        """
    )


def downgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    op.execute("DROP TRIGGER IF EXISTS trg_assign_audit_chain_seq ON apex.audit_log")
    op.execute("DROP FUNCTION IF EXISTS apex.assign_audit_chain_seq()")

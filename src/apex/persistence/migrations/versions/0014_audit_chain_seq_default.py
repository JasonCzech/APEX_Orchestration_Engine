"""Retire the unused audit sequence without rewriting the hash-chain order."""

from collections.abc import Sequence

from alembic import op

revision: str = "0014"
down_revision: str | None = "0013"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    # 0013 already assigned and constrained chain_seq. Re-numbering here would
    # invalidate previous_hash traversal for events whose clocks moved backwards.
    op.alter_column("audit_log", "chain_seq", nullable=False, schema="apex")
    op.alter_column("audit_log", "chain_seq", server_default=None, schema="apex")
    op.execute("DROP SEQUENCE IF EXISTS apex.audit_chain_seq_seq")


def downgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    # The service allocates from the advisory-locked chain head. A database
    # sequence is intentionally not recreated because it cannot remain gapless
    # across transaction rollbacks.

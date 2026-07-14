"""Provide a database fallback for audit chain sequence writes."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0014"
down_revision: str | None = "0013"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    op.execute("CREATE SEQUENCE IF NOT EXISTS apex.audit_chain_seq_seq")
    op.execute(
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
    op.alter_column("audit_log", "chain_seq", nullable=False, schema="apex")
    op.execute(
        """SELECT setval(
            'apex.audit_chain_seq_seq',
            COALESCE((SELECT MAX(chain_seq) FROM apex.audit_log), 0) + 1,
            false
        )"""
    )
    op.alter_column(
        "audit_log",
        "chain_seq",
        server_default=sa.text("nextval('apex.audit_chain_seq_seq')"),
        schema="apex",
    )


def downgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    op.alter_column("audit_log", "chain_seq", server_default=None, schema="apex")
    op.execute("DROP SEQUENCE IF EXISTS apex.audit_chain_seq_seq")

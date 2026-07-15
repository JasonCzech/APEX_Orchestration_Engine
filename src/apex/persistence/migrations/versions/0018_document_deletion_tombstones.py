"""Make document/object deletion recoverable across failures.

Revision ID: 0018
Revises: 0017
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0018"
down_revision: str | None = "0017"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    op.add_column(
        "documents",
        sa.Column("deletion_pending_at", sa.DateTime(timezone=True), nullable=True),
        schema="apex",
    )
    op.create_index(
        "ix_documents_deletion_pending",
        "documents",
        ["deletion_pending_at"],
        unique=False,
        schema="apex",
    )


def downgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (
                SELECT 1 FROM apex.documents
                WHERE deletion_pending_at IS NOT NULL
            ) THEN
                RAISE EXCEPTION
                    'cannot downgrade with pending document deletions; drain tombstones first';
            END IF;
        END $$
        """
    )
    op.drop_index("ix_documents_deletion_pending", table_name="documents", schema="apex")
    op.drop_column("documents", "deletion_pending_at", schema="apex")

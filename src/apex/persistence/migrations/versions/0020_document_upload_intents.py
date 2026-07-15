"""Persist hidden upload intents before object-store writes.

Revision ID: 0020
Revises: 0019
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0020"
down_revision: str | None = "0019"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    op.add_column(
        "documents",
        sa.Column("upload_pending_at", sa.DateTime(timezone=True), nullable=True),
        schema="apex",
    )
    op.create_index(
        "ix_documents_upload_pending",
        "documents",
        ["upload_pending_at"],
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
                WHERE upload_pending_at IS NOT NULL
            ) THEN
                RAISE EXCEPTION
                    'cannot downgrade with pending document uploads; reconcile intents first';
            END IF;
        END $$
        """
    )
    op.drop_index("ix_documents_upload_pending", table_name="documents", schema="apex")
    op.drop_column("documents", "upload_pending_at", schema="apex")

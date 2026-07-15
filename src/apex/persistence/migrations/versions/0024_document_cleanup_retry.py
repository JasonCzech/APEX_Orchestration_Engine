"""Add fair retry scheduling for durable document cleanup intents.

Revision ID: 0024
Revises: 0023
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0024"
down_revision: str | None = "0023"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "documents",
        sa.Column("cleanup_retry_at", sa.DateTime(timezone=True), nullable=True),
        schema="apex",
    )
    op.add_column(
        "documents",
        sa.Column(
            "cleanup_attempt_count",
            sa.Integer(),
            server_default="0",
            nullable=False,
        ),
        schema="apex",
    )
    op.add_column(
        "documents",
        sa.Column("cleanup_last_error", sa.Text(), nullable=True),
        schema="apex",
    )
    op.create_index(
        "ix_documents_cleanup_retry",
        "documents",
        ["cleanup_retry_at"],
        schema="apex",
    )


def downgrade() -> None:
    op.drop_index(
        "ix_documents_cleanup_retry",
        table_name="documents",
        schema="apex",
    )
    op.drop_column("documents", "cleanup_last_error", schema="apex")
    op.drop_column("documents", "cleanup_attempt_count", schema="apex")
    op.drop_column("documents", "cleanup_retry_at", schema="apex")

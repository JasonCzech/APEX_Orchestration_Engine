"""add document extracted text

Revision ID: 0011
Revises: 0010
Create Date: 2026-06-26
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0011"
down_revision: str | None = "0010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "documents",
        sa.Column("extracted_text", sa.Text(), nullable=True),
        schema="apex",
    )
    op.add_column(
        "documents",
        sa.Column("extracted_chars", sa.Integer(), nullable=True),
        schema="apex",
    )
    op.add_column(
        "documents",
        sa.Column("parse_status", sa.String(length=32), nullable=True),
        schema="apex",
    )
    op.add_column(
        "documents",
        sa.Column("parse_error", sa.Text(), nullable=True),
        schema="apex",
    )


def downgrade() -> None:
    op.drop_column("documents", "parse_error", schema="apex")
    op.drop_column("documents", "parse_status", schema="apex")
    op.drop_column("documents", "extracted_chars", schema="apex")
    op.drop_column("documents", "extracted_text", schema="apex")

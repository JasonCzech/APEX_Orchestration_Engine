"""Bind finalized durable artifact keys to exact payload identity.

Revision ID: 0025
Revises: 0024
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0025"
down_revision: str | None = "0024"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Existing ownership-only rows cannot be backfilled safely without reading
    # the external object. Keep them explicitly unverifiable; every upload
    # finalized by the new application writes all three fields atomically.
    op.add_column(
        "artifact_references",
        sa.Column("content_sha256", sa.String(length=64), nullable=True),
        schema="apex",
    )
    op.add_column(
        "artifact_references",
        sa.Column("size_bytes", sa.BigInteger(), nullable=True),
        schema="apex",
    )
    op.add_column(
        "artifact_references",
        sa.Column("content_type", sa.String(length=255), nullable=True),
        schema="apex",
    )


def downgrade() -> None:
    op.drop_column("artifact_references", "content_type", schema="apex")
    op.drop_column("artifact_references", "size_bytes", schema="apex")
    op.drop_column("artifact_references", "content_sha256", schema="apex")

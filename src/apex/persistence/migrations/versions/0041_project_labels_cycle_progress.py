"""Authoritative project labels and bounded cycle list progress."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0041"
down_revision = "0040"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "project_labels",
        sa.Column("project_id", sa.String(255), primary_key=True),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        schema="apex",
    )
    op.add_column(
        "test_cycles", sa.Column("progress", postgresql.JSONB(), nullable=True), schema="apex"
    )
    # Schema-only, including offline SQL deployments. The list service fills
    # missing projections in bounded, revision-qualified batches on first read.


def downgrade() -> None:
    op.drop_column("test_cycles", "progress", schema="apex")
    op.drop_table("project_labels", schema="apex")

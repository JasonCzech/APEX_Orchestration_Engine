"""Archive execution preparations without deleting their validation history."""

import sqlalchemy as sa
from alembic import op

revision = "0039"
down_revision = "0038"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "cycle_execution_preparations",
        sa.Column("archived_at", sa.DateTime(timezone=True), nullable=True),
        schema="apex",
    )
    op.add_column(
        "cycle_execution_preparations",
        sa.Column("archived_by", sa.String(255), nullable=True),
        schema="apex",
    )


def downgrade() -> None:
    op.drop_column("cycle_execution_preparations", "archived_by", schema="apex")
    op.drop_column("cycle_execution_preparations", "archived_at", schema="apex")

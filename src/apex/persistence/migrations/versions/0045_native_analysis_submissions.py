"""Native analysis write-ahead reservations and acknowledged remote identities."""

import sqlalchemy as sa
from alembic import op

revision = "0045"
down_revision = "0044"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "native_analysis_submissions",
        sa.Column("idempotency_key", sa.String(64), primary_key=True),
        sa.Column("scope_digest", sa.String(64), nullable=False),
        sa.Column("thread_id", sa.String(255), nullable=False),
        sa.Column("connection_id", sa.String(256), nullable=False),
        sa.Column("native_run_id", sa.String(256), nullable=False),
        sa.Column("job_id", sa.String(256), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=True),
        schema="apex",
    )


def downgrade() -> None:
    op.drop_table("native_analysis_submissions", schema="apex")

"""Index of the prompt versions each launched test pinned ("Used by" in the catalog)."""

import sqlalchemy as sa
from alembic import op

revision = "0046"
down_revision = "0045"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "prompt_usages",
        sa.Column("thread_id", sa.String(64), primary_key=True),
        sa.Column("prompt_id", sa.String(32), primary_key=True),
        sa.Column("slot", sa.String(64), primary_key=True),
        sa.Column("version_id", sa.String(32), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("project_id", sa.String(255), nullable=True),
        sa.Column("app_id", sa.String(255), nullable=True),
        sa.Column("title", sa.String(500), nullable=True),
        sa.Column(
            "recorded_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        schema="apex",
    )
    op.create_index(
        "ix_prompt_usages_prompt_recorded",
        "prompt_usages",
        ["prompt_id", "recorded_at"],
        schema="apex",
    )


def downgrade() -> None:
    op.drop_index("ix_prompt_usages_prompt_recorded", table_name="prompt_usages", schema="apex")
    op.drop_table("prompt_usages", schema="apex")

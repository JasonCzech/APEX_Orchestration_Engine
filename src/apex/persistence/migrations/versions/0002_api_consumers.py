"""api consumers + scopes

Revision ID: 0002
Revises: 0001
Create Date: 2026-06-11

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "api_consumers",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("key_hash", sa.String(length=64), nullable=False),
        sa.Column("consumer_type", sa.String(length=32), nullable=False),
        sa.Column("role", sa.String(length=32), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_api_consumers")),
        sa.UniqueConstraint("name", name=op.f("uq_api_consumers_name")),
        sa.UniqueConstraint("key_hash", name=op.f("uq_api_consumers_key_hash")),
        schema="apex",
    )
    op.create_table(
        "consumer_scopes",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("consumer_id", sa.String(length=32), nullable=False),
        sa.Column("project_id", sa.String(length=255), nullable=False),
        sa.Column("app_id", sa.String(length=255), nullable=True),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_consumer_scopes")),
        sa.ForeignKeyConstraint(
            ["consumer_id"],
            ["apex.api_consumers.id"],
            name=op.f("fk_consumer_scopes_api_consumers_consumer_id"),
            ondelete="CASCADE",
        ),
        sa.UniqueConstraint(
            "consumer_id",
            "project_id",
            "app_id",
            name=op.f("uq_consumer_scopes_consumer_id"),
        ),
        schema="apex",
    )


def downgrade() -> None:
    op.drop_table("consumer_scopes", schema="apex")
    op.drop_table("api_consumers", schema="apex")

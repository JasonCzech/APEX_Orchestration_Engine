"""Persist artifact bytes before provider upload and ownership indexing.

Revision ID: 0021
Revises: 0020
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0021"
down_revision: str | None = "0020"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    op.create_table(
        "artifact_upload_intents",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("artifact_key", sa.String(length=1024), nullable=False),
        sa.Column("connection_id", sa.String(length=32), nullable=False),
        sa.Column("kind", sa.String(length=64), nullable=False),
        sa.Column("thread_id", sa.String(length=255), nullable=False),
        sa.Column("project_id", sa.String(length=255), nullable=True),
        sa.Column("app_id", sa.String(length=255), nullable=True),
        sa.Column("payload", sa.LargeBinary(), nullable=False),
        sa.Column("content_type", sa.String(length=255), nullable=False),
        sa.Column("claim_token", sa.String(length=32), nullable=False),
        sa.Column("claimed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("attempt_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["connection_id"], ["apex.connections.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("artifact_key"),
        schema="apex",
    )
    op.create_index(
        "ix_artifact_upload_intents_connection_id",
        "artifact_upload_intents",
        ["connection_id"],
        schema="apex",
    )
    op.create_index(
        "ix_artifact_upload_intents_updated_at",
        "artifact_upload_intents",
        ["updated_at"],
        schema="apex",
    )


def downgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM apex.artifact_upload_intents) THEN
                RAISE EXCEPTION
                    'cannot downgrade with pending artifact uploads; drain the outbox first';
            END IF;
        END $$
        """
    )
    op.drop_index(
        "ix_artifact_upload_intents_updated_at",
        table_name="artifact_upload_intents",
        schema="apex",
    )
    op.drop_index(
        "ix_artifact_upload_intents_connection_id",
        table_name="artifact_upload_intents",
        schema="apex",
    )
    op.drop_table("artifact_upload_intents", schema="apex")

"""Add durable, scoped idempotency records for work-item mutations.

Revision ID: 0023
Revises: 0022
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0023"
down_revision: str | None = "0022"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    op.create_table(
        "work_item_mutations",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("tenant_scope", sa.String(length=64), nullable=False),
        sa.Column("consumer_id", sa.String(length=255), nullable=False),
        sa.Column("project_id", sa.String(length=255), nullable=True),
        sa.Column("connection_id", sa.String(length=32), nullable=False),
        sa.Column("connection_version", sa.DateTime(timezone=True), nullable=False),
        sa.Column("operation", sa.String(length=32), nullable=False),
        sa.Column("idempotency_key", sa.String(length=255), nullable=False),
        sa.Column("payload_hash", sa.String(length=64), nullable=False),
        sa.Column(
            "payload",
            sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql"),
            nullable=False,
        ),
        sa.Column("target_key", sa.String(length=255), nullable=True),
        sa.Column("provider_marker", sa.String(length=64), nullable=False),
        sa.Column("provider_attempted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("comment_attempted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("status", sa.String(length=32), server_default="pending", nullable=False),
        sa.Column("fields_status", sa.String(length=32), server_default="skipped", nullable=False),
        sa.Column("comment_status", sa.String(length=32), server_default="skipped", nullable=False),
        sa.Column(
            "result",
            sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql"),
            nullable=True,
        ),
        sa.Column("claim_token", sa.String(length=32), nullable=True),
        sa.Column("claimed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("attempt_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("terminal_error", sa.String(length=32), nullable=True),
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
        sa.UniqueConstraint("provider_marker"),
        sa.UniqueConstraint(
            "tenant_scope",
            "consumer_id",
            "connection_id",
            "operation",
            "idempotency_key",
            name="uq_work_item_mutation_scope_key",
        ),
        schema="apex",
    )
    op.create_index(
        "ix_work_item_mutations_connection_id",
        "work_item_mutations",
        ["connection_id"],
        schema="apex",
    )
    op.create_index(
        "ix_work_item_mutations_reconcile",
        "work_item_mutations",
        ["status", "next_attempt_at"],
        schema="apex",
    )
    op.create_index(
        "ix_work_item_mutations_terminal_retirement",
        "work_item_mutations",
        ["status", "updated_at"],
        schema="apex",
    )
    op.create_table(
        "work_item_mutation_tombstones",
        sa.Column("scope_hash", sa.String(length=64), nullable=False),
        sa.Column("payload_hash", sa.String(length=64), nullable=False),
        sa.Column("outcome", sa.String(length=32), nullable=False),
        sa.Column(
            "retired_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("scope_hash"),
        schema="apex",
    )


def downgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM apex.work_item_mutations)
               OR EXISTS (SELECT 1 FROM apex.work_item_mutation_tombstones) THEN
                RAISE EXCEPTION
                    'cannot downgrade with work-item idempotency records present; '
                    'retain or explicitly archive them first';
            END IF;
        END $$;
        """
    )
    op.drop_table("work_item_mutation_tombstones", schema="apex")
    op.drop_index(
        "ix_work_item_mutations_terminal_retirement",
        table_name="work_item_mutations",
        schema="apex",
    )
    op.drop_index(
        "ix_work_item_mutations_reconcile",
        table_name="work_item_mutations",
        schema="apex",
    )
    op.drop_index(
        "ix_work_item_mutations_connection_id",
        table_name="work_item_mutations",
        schema="apex",
    )
    op.drop_table("work_item_mutations", schema="apex")

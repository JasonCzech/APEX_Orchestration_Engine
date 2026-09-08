"""Durable immutable script storage intents and verified acknowledgements."""

import sqlalchemy as sa
from alembic import op

revision = "0035"
down_revision = "0034"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "cycle_script_artifacts",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column(
            "cycle_id",
            sa.String(32),
            sa.ForeignKey("apex.test_cycles.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("version_id", sa.String(32), nullable=False),
        sa.Column("version_checksum", sa.String(64), nullable=False),
        sa.Column("source_checksum", sa.String(64), nullable=False),
        sa.Column("size_bytes", sa.Integer(), nullable=False),
        sa.Column("artifact_key", sa.String(255), nullable=False),
        sa.Column(
            "connection_id",
            sa.String(32),
            sa.ForeignKey("apex.connections.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("connection_version", sa.DateTime(timezone=True), nullable=False),
        sa.Column("stored_at", sa.DateTime(timezone=True)),
        sa.Column("created_by", sa.String(255), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.UniqueConstraint("cycle_id", "version_id"),
        schema="apex",
    )
    op.create_index(
        "ix_apex_cycle_script_artifacts_cycle_id",
        "cycle_script_artifacts",
        ["cycle_id"],
        schema="apex",
    )


def downgrade() -> None:
    op.drop_table("cycle_script_artifacts", schema="apex")

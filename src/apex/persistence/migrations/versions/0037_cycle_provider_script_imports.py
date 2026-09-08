"""Preserve provider export provenance for imported script drafts."""

import sqlalchemy as sa
from alembic import op

revision = "0037"
down_revision = "0036"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "cycle_provider_script_imports",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column(
            "cycle_id",
            sa.String(32),
            sa.ForeignKey("apex.test_cycles.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("version_id", sa.String(32), nullable=False),
        sa.Column("request_key", sa.String(32), nullable=False),
        sa.Column("request_fingerprint", sa.String(64), nullable=False),
        sa.Column(
            "connection_id",
            sa.String(32),
            sa.ForeignKey("apex.connections.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("connection_version", sa.DateTime(timezone=True), nullable=False),
        sa.Column("remote_script_id", sa.String(255), nullable=False),
        sa.Column("remote_name", sa.String(255), nullable=False),
        sa.Column("remote_project_id", sa.String(255), nullable=False),
        sa.Column("remote_version", sa.String(255), nullable=False),
        sa.Column("source_checksum", sa.String(64), nullable=False),
        sa.Column("imported_by", sa.String(255), nullable=False),
        sa.Column(
            "imported_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.UniqueConstraint("cycle_id", "request_key"),
        schema="apex",
    )
    op.create_index(
        "ix_apex_cycle_provider_script_imports_cycle_id",
        "cycle_provider_script_imports",
        ["cycle_id"],
        schema="apex",
    )


def downgrade() -> None:
    op.drop_table("cycle_provider_script_imports", schema="apex")

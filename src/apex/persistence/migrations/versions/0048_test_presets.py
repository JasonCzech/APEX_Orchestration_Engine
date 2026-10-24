"""Team and personal test presets, their versions, and run → preset provenance."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0048"
down_revision = "0047"
branch_labels = None
depends_on = None


def _json() -> sa.types.TypeEngine:
    return sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql")


def upgrade() -> None:
    op.create_table(
        "test_presets",
        sa.Column("id", sa.String(32), nullable=False),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("project_id", sa.String(255), nullable=False),
        sa.Column("application_id", sa.String(32), nullable=True),
        sa.Column("visibility", sa.String(16), nullable=False),
        sa.Column("payload", _json(), nullable=False),
        sa.Column("version", sa.Integer(), server_default="1", nullable=False),
        sa.Column("created_by", sa.String(255), nullable=True),
        sa.Column("created_by_consumer_id", sa.String(32), nullable=True),
        sa.Column("updated_by", sa.String(255), nullable=True),
        sa.Column("archived_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_test_presets")),
        schema="apex",
    )
    op.create_index("ix_test_presets_project_id", "test_presets", ["project_id"], schema="apex")
    op.create_index(
        "ix_test_presets_application_id", "test_presets", ["application_id"], schema="apex"
    )
    op.create_table(
        "test_preset_versions",
        sa.Column("preset_id", sa.String(32), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("payload", _json(), nullable=False),
        sa.Column("note", sa.String(500), nullable=True),
        sa.Column("created_by", sa.String(255), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["preset_id"],
            ["apex.test_presets.id"],
            name=op.f("fk_test_preset_versions_test_presets_preset_id"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("preset_id", "version", name=op.f("pk_test_preset_versions")),
        schema="apex",
    )
    op.create_table(
        "run_preset_links",
        sa.Column("thread_id", sa.String(64), nullable=False),
        sa.Column("preset_kind", sa.String(16), nullable=False),
        sa.Column("preset_id", sa.String(256), nullable=False),
        sa.Column("preset_version", sa.Integer(), nullable=True),
        sa.Column("preset_name", sa.String(255), nullable=True),
        sa.Column("modified", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("project_id", sa.String(255), nullable=True),
        sa.Column("app_id", sa.String(255), nullable=True),
        sa.Column(
            "recorded_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("thread_id", name=op.f("pk_run_preset_links")),
        schema="apex",
    )
    op.create_index(
        "ix_run_preset_links_preset",
        "run_preset_links",
        ["preset_kind", "preset_id"],
        schema="apex",
    )


def downgrade() -> None:
    op.drop_index("ix_run_preset_links_preset", table_name="run_preset_links", schema="apex")
    op.drop_table("run_preset_links", schema="apex")
    op.drop_table("test_preset_versions", schema="apex")
    op.drop_index("ix_test_presets_application_id", table_name="test_presets", schema="apex")
    op.drop_index("ix_test_presets_project_id", table_name="test_presets", schema="apex")
    op.drop_table("test_presets", schema="apex")

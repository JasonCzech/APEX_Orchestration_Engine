"""AIFx Context Services: onboarded organizations and application repo mappings."""

import sqlalchemy as sa
from alembic import op

revision = "0049"
down_revision = "0048"
branch_labels = None
depends_on = None


def _timestamps() -> list[sa.Column]:
    return [
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
    ]


def upgrade() -> None:
    op.create_table(
        "aifx_organizations",
        sa.Column("id", sa.String(32), nullable=False),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("connection_id", sa.String(32), nullable=False),
        sa.Column("ghe_owner", sa.String(100), nullable=False),
        sa.Column("global_rules_repo", sa.String(100), nullable=True),
        sa.Column("team_documents_repo_pattern", sa.String(120), nullable=True),
        sa.Column("acb_repo_pattern", sa.String(120), nullable=True),
        sa.Column("default_ref", sa.String(255), server_default="main", nullable=False),
        sa.Column("enabled", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        *_timestamps(),
        sa.ForeignKeyConstraint(
            ["connection_id"],
            ["apex.connections.id"],
            name=op.f("fk_aifx_organizations_connections_connection_id"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_aifx_organizations")),
        sa.UniqueConstraint("name", name=op.f("uq_aifx_organizations_name")),
        schema="apex",
    )
    op.create_table(
        "aifx_application_sources",
        sa.Column("application_id", sa.String(32), nullable=False),
        sa.Column("organization_id", sa.String(32), nullable=False),
        sa.Column("apm_id", sa.String(64), nullable=False),
        sa.Column("team_documents_repo", sa.String(100), nullable=True),
        sa.Column("acb_repo", sa.String(100), nullable=True),
        sa.Column("ref", sa.String(255), nullable=True),
        sa.Column("enabled", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        *_timestamps(),
        sa.ForeignKeyConstraint(
            ["application_id"],
            ["apex.applications.id"],
            name=op.f("fk_aifx_application_sources_applications_application_id"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["apex.aifx_organizations.id"],
            name=op.f("fk_aifx_application_sources_aifx_organizations_organization_id"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("application_id", name=op.f("pk_aifx_application_sources")),
        sa.UniqueConstraint("apm_id", name=op.f("uq_aifx_application_sources_apm_id")),
        schema="apex",
    )
    op.create_index(
        "ix_aifx_application_sources_organization_id",
        "aifx_application_sources",
        ["organization_id"],
        schema="apex",
    )


def downgrade() -> None:
    op.drop_index(
        "ix_aifx_application_sources_organization_id",
        table_name="aifx_application_sources",
        schema="apex",
    )
    op.drop_table("aifx_application_sources", schema="apex")
    op.drop_table("aifx_organizations", schema="apex")

"""Tracked postmortem actions and idempotent cycle lineage."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0038"
down_revision = "0037"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "test_cycles",
        sa.Column(
            "followups", postgresql.JSONB(), nullable=False, server_default=sa.text("'[]'::jsonb")
        ),
        schema="apex",
    )
    op.add_column(
        "test_cycles", sa.Column("origin_cycle_id", sa.String(32), nullable=True), schema="apex"
    )
    op.add_column(
        "test_cycles", sa.Column("origin_request_key", sa.String(32), nullable=True), schema="apex"
    )
    op.add_column(
        "test_cycles", sa.Column("origin_fingerprint", sa.String(64), nullable=True), schema="apex"
    )
    op.create_foreign_key(
        "fk_test_cycles_origin_cycle_id_test_cycles",
        "test_cycles",
        "test_cycles",
        ["origin_cycle_id"],
        ["id"],
        source_schema="apex",
        referent_schema="apex",
        ondelete="RESTRICT",
    )
    op.create_unique_constraint(
        "uq_test_cycles_origin_cycle_id",
        "test_cycles",
        ["origin_cycle_id", "origin_request_key"],
        schema="apex",
    )


def downgrade():
    op.drop_constraint(
        "uq_test_cycles_origin_cycle_id", "test_cycles", schema="apex", type_="unique"
    )
    op.drop_constraint(
        "fk_test_cycles_origin_cycle_id_test_cycles",
        "test_cycles",
        schema="apex",
        type_="foreignkey",
    )
    for name in ("origin_fingerprint", "origin_request_key", "origin_cycle_id", "followups"):
        op.drop_column("test_cycles", name, schema="apex")

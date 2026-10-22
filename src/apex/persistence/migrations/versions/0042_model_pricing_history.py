"""Retained model pricing, corrections and private usage attribution.

Frozen PostgreSQL DDL; no imports of mutable runtime model metadata.
"""

import sqlalchemy as sa
from alembic import op

revision = "0042"
down_revision = "0041"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.alter_column(
        "agent_events",
        "cost_usd",
        type_=sa.Numeric(24, 6),
        existing_type=sa.Numeric(12, 6),
        schema="apex",
    )
    op.execute(
        """
        CREATE TABLE apex.model_run_admissions (
            thread_id VARCHAR(64) NOT NULL,
            models_hash VARCHAR(64) NOT NULL,
            models JSONB NOT NULL,
            actor_id VARCHAR(255) NOT NULL,
            admitted_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
            CONSTRAINT pk_model_run_admissions PRIMARY KEY (thread_id, models_hash)
        )
        """
    )
    op.execute(
        """
        CREATE TABLE apex.model_catalog (
            id VARCHAR(32) NOT NULL,
            provider VARCHAR(64) NOT NULL,
            model_key VARCHAR(255) NOT NULL,
            display_name VARCHAR(255) NOT NULL,
            archived_at TIMESTAMP WITH TIME ZONE,
            archived_by VARCHAR(255),
            archive_reason VARCHAR(1000),
            version INTEGER NOT NULL,
            active_pricing_generation_id VARCHAR(32),
            created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
            updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
            CONSTRAINT pk_model_catalog PRIMARY KEY (id),
            CONSTRAINT uq_model_catalog_provider UNIQUE (provider, model_key)
        )
        """
    )
    op.execute(
        """
        CREATE TABLE apex.model_pricing_generations (
            id VARCHAR(32) NOT NULL,
            model_id VARCHAR(32) NOT NULL,
            number INTEGER NOT NULL,
            created_by VARCHAR(255),
            creator_name VARCHAR(255),
            reason VARCHAR(1000) NOT NULL,
            source VARCHAR(32) NOT NULL,
            job_id VARCHAR(32),
            published_at TIMESTAMP WITH TIME ZONE,
            created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
            CONSTRAINT pk_model_pricing_generations PRIMARY KEY (id),
            CONSTRAINT uq_model_pricing_generations_model_id UNIQUE (model_id, number),
            CONSTRAINT fk_model_pricing_generations_model_catalog_model_id
                FOREIGN KEY(model_id)
                REFERENCES apex.model_catalog (id)
                ON DELETE RESTRICT
        )
        """
    )
    op.execute(
        """
        CREATE TABLE apex.model_price_intervals (
            id VARCHAR(32) NOT NULL,
            generation_id VARCHAR(32) NOT NULL,
            effective_from TIMESTAMP WITH TIME ZONE NOT NULL,
            effective_to TIMESTAMP WITH TIME ZONE,
            input NUMERIC(18, 9) NOT NULL,
            output NUMERIC(18, 9) NOT NULL,
            cache_read NUMERIC(18, 9) NOT NULL,
            cache_creation NUMERIC(18, 9) NOT NULL,
            currency VARCHAR(3) NOT NULL,
            unit VARCHAR(32) NOT NULL,
            CONSTRAINT pk_model_price_intervals PRIMARY KEY (id),
            CONSTRAINT fk_model_price_intervals_model_pricing_generations_gene_3aeb
                FOREIGN KEY(generation_id)
                REFERENCES apex.model_pricing_generations (id)
                ON DELETE RESTRICT
        )
        """
    )
    op.execute(
        "CREATE INDEX ix_apex_model_price_intervals_generation_id "
        "ON apex.model_price_intervals (generation_id)"
    )
    op.execute(
        """
        CREATE TABLE apex.pricing_correction_jobs (
            id VARCHAR(32) NOT NULL,
            model_id VARCHAR(32) NOT NULL,
            generation_id VARCHAR(32) NOT NULL,
            base_generation_id VARCHAR(32),
            expected_version INTEGER NOT NULL,
            effective_from TIMESTAMP WITH TIME ZONE NOT NULL,
            effective_to TIMESTAMP WITH TIME ZONE NOT NULL,
            actor_id VARCHAR(255) NOT NULL,
            reason VARCHAR(1000) NOT NULL,
            idempotency_key VARCHAR(128) NOT NULL,
            payload_hash VARCHAR(64) NOT NULL,
            state VARCHAR(16) NOT NULL,
            lease_owner VARCHAR(32),
            lease_expires_at TIMESTAMP WITH TIME ZONE,
            retry_count INTEGER NOT NULL,
            progress_cursor VARCHAR(32),
            processed_count INTEGER NOT NULL,
            preview_totals JSONB NOT NULL,
            final_totals JSONB NOT NULL,
            failure_code VARCHAR(64),
            created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
            updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
            CONSTRAINT pk_pricing_correction_jobs PRIMARY KEY (id),
            CONSTRAINT uq_pricing_correction_jobs_model_id UNIQUE (model_id, idempotency_key),
            CONSTRAINT fk_pricing_correction_jobs_model_catalog_model_id
                FOREIGN KEY(model_id)
                REFERENCES apex.model_catalog (id)
                ON DELETE RESTRICT
        )
        """
    )
    op.execute(
        "CREATE INDEX ix_apex_pricing_correction_jobs_state ON apex.pricing_correction_jobs (state)"
    )
    op.execute(
        """
        CREATE TABLE apex.usage_cost_adjustments (
            id VARCHAR(32) NOT NULL,
            event_id VARCHAR(32) NOT NULL,
            generation_id VARCHAR(32) NOT NULL,
            interval_id VARCHAR(32) NOT NULL,
            cost_usd NUMERIC(24, 6) NOT NULL,
            job_id VARCHAR(32) NOT NULL,
            created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
            CONSTRAINT pk_usage_cost_adjustments PRIMARY KEY (id),
            CONSTRAINT uq_usage_cost_adjustments_event_id UNIQUE (event_id, generation_id),
            CONSTRAINT fk_usage_cost_adjustments_agent_events_event_id
                FOREIGN KEY(event_id)
                REFERENCES apex.agent_events (id)
                ON DELETE RESTRICT,
            CONSTRAINT fk_usage_cost_adjustments_model_pricing_generations_gen_951e
                FOREIGN KEY(generation_id)
                REFERENCES apex.model_pricing_generations (id)
                ON DELETE RESTRICT,
            CONSTRAINT fk_usage_cost_adjustments_model_price_intervals_interval_id
                FOREIGN KEY(interval_id)
                REFERENCES apex.model_price_intervals (id)
                ON DELETE RESTRICT,
            CONSTRAINT fk_usage_cost_adjustments_pricing_correction_jobs_job_id
                FOREIGN KEY(job_id)
                REFERENCES apex.pricing_correction_jobs (id)
                ON DELETE RESTRICT
        )
        """
    )
    op.execute(
        "CREATE INDEX ix_apex_usage_cost_adjustments_event_id "
        "ON apex.usage_cost_adjustments (event_id)"
    )
    op.execute(
        """
        CREATE TABLE apex.model_admin_actions (
            id VARCHAR(32) NOT NULL,
            model_id VARCHAR(32) NOT NULL,
            actor_id VARCHAR(255) NOT NULL,
            action VARCHAR(32) NOT NULL,
            reason VARCHAR(1000) NOT NULL,
            idempotency_key VARCHAR(128) NOT NULL,
            payload_hash VARCHAR(64) NOT NULL,
            result JSONB NOT NULL,
            at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
            CONSTRAINT pk_model_admin_actions PRIMARY KEY (id),
            CONSTRAINT uq_model_admin_actions_model_id UNIQUE (model_id, idempotency_key),
            CONSTRAINT fk_model_admin_actions_model_catalog_model_id
                FOREIGN KEY(model_id)
                REFERENCES apex.model_catalog (id)
                ON DELETE RESTRICT
        )
        """
    )
    op.add_column(
        "usage_events", sa.Column("consumer_id", sa.String(255), nullable=True), schema="apex"
    )
    op.add_column(
        "usage_events",
        sa.Column("recorded_at", sa.DateTime(timezone=True), nullable=True),
        schema="apex",
    )
    op.alter_column("usage_events", "recorded_at", server_default=sa.func.now(), schema="apex")
    op.execute("CREATE INDEX ix_apex_usage_events_consumer_id ON apex.usage_events (consumer_id)")
    op.execute("CREATE INDEX ix_usageevent_consumer_at ON apex.usage_events (consumer_id, at)")
    op.execute("CREATE INDEX ix_usageevent_at_id ON apex.usage_events (at, id)")
    op.execute(
        "CREATE INDEX ix_usageevent_scope_consumer_at "
        "ON apex.usage_events (project_id, app_id, consumer_id, at)"
    )
    op.add_column(
        "agent_events", sa.Column("consumer_id", sa.String(255), nullable=True), schema="apex"
    )
    op.add_column(
        "agent_events",
        sa.Column("recorded_at", sa.DateTime(timezone=True), nullable=True),
        schema="apex",
    )
    op.alter_column("agent_events", "recorded_at", server_default=sa.func.now(), schema="apex")
    op.add_column(
        "agent_events", sa.Column("model_catalog_id", sa.String(32), nullable=True), schema="apex"
    )
    op.add_column(
        "agent_events",
        sa.Column("pricing_interval_id", sa.String(32), nullable=True),
        schema="apex",
    )
    op.add_column(
        "agent_events",
        sa.Column("consumer_name_snapshot", sa.String(255), nullable=True),
        schema="apex",
    )
    op.create_foreign_key(
        "fk_agent_events_model_catalog_id",
        "agent_events",
        "model_catalog",
        ["model_catalog_id"],
        ["id"],
        source_schema="apex",
        referent_schema="apex",
        ondelete="RESTRICT",
    )
    op.create_foreign_key(
        "fk_agent_events_pricing_interval_id",
        "agent_events",
        "model_price_intervals",
        ["pricing_interval_id"],
        ["id"],
        source_schema="apex",
        referent_schema="apex",
        ondelete="RESTRICT",
    )
    op.execute(
        "CREATE INDEX ix_apex_agent_events_model_catalog_id ON apex.agent_events (model_catalog_id)"
    )
    op.execute("CREATE INDEX ix_agentevent_at_id ON apex.agent_events (at, id)")
    op.execute(
        "CREATE INDEX ix_agentevent_model_catalog_at "
        "ON apex.agent_events (model_catalog_id, at)"
    )
    op.execute("CREATE INDEX ix_agentevent_consumer_at ON apex.agent_events (consumer_id, at)")
    op.execute(
        "CREATE INDEX ix_agentevent_scope_consumer_at "
        "ON apex.agent_events (project_id, app_id, consumer_id, at)"
    )
    op.execute("CREATE INDEX ix_apex_agent_events_consumer_id ON apex.agent_events (consumer_id)")

    op.create_check_constraint(
        "ck_model_price_intervals_nonnegative_rates",
        "model_price_intervals",
        "input >= 0 AND output >= 0 AND cache_read >= 0 AND cache_creation >= 0",
        schema="apex",
    )
    op.create_check_constraint(
        "ck_model_price_intervals_positive_interval",
        "model_price_intervals",
        "effective_to IS NULL OR effective_to > effective_from",
        schema="apex",
    )


def downgrade() -> None:
    raise RuntimeError(
        "Retained pricing history requires an explicit archival migration before downgrade"
    )

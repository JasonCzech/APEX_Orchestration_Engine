"""Explicit prompt applicability and default selections; preserve legacy content."""

import sqlalchemy as sa
from alembic import op

revision = "0043"
down_revision = "0042"
branch_labels = None
depends_on = None


def upgrade() -> None:
    for name, type_ in (
        ("phase", sa.String(64)),
        ("application_id", sa.String(32)),
        ("environment_id", sa.String(32)),
    ):
        op.add_column("prompts", sa.Column(name, type_, nullable=True), schema="apex")
    for name in ("is_shared", "is_default", "selection_enabled"):
        op.add_column(
            "prompts",
            sa.Column(name, sa.Boolean(), nullable=False, server_default=sa.text("false")),
            schema="apex",
        )
    op.create_foreign_key(
        "fk_prompts_application",
        "prompts",
        "applications",
        ["application_id"],
        ["id"],
        source_schema="apex",
        referent_schema="apex",
    )
    op.create_foreign_key(
        "fk_prompts_environment",
        "prompts",
        "environments",
        ["environment_id"],
        ["id"],
        source_schema="apex",
        referent_schema="apex",
    )
    migrate_catalog(op.execute)
    op.create_index(
        "uq_prompts_default_system",
        "prompts",
        ["namespace"],
        unique=True,
        schema="apex",
        postgresql_where=sa.text("is_default AND namespace='system'"),
    )
    op.create_index(
        "uq_prompts_default_phase",
        "prompts",
        ["phase"],
        unique=True,
        schema="apex",
        postgresql_where=sa.text("is_default AND namespace='phase'"),
    )
    op.create_check_constraint(
        "prompt_default_eligible",
        "prompts",
        "NOT is_default OR (selection_enabled AND archived_at IS NULL "
        "AND active_version_id IS NOT NULL AND namespace IN ('system', 'phase'))",
        schema="apex",
    )


# Frozen revision data: future catalog edits must not change this migration.
SYSTEM_CONTENT = """Guide this performance-testing pipeline using the selected phase instructions,
application and environment context, and supplied evidence. Maintain continuity
between phases, identify missing requirements explicitly, and produce only the
current phase's requested deliverable within the engine's policy and output contract.
"""
PHASE_CONTENT = {
    "story_analysis": """Analyze supplied requirements into performance-testable scenarios.
Required inputs: scoped stories/requirements and available application context.
Assign stable scenario IDs; map each to source requirement IDs and evidence.
For each scenario identify transaction boundaries, ordered systems, business
invariants, data needs, risk, protocol constraints, and expected acceptance criteria.
Consider happy, negative, bulk, concurrent, asynchronous and data-intensive paths
only where relevant; explain exclusions and avoid duplicate scenarios.
Distinguish stated targets from assumptions. Finish with a coverage map showing
covered, excluded, and blocked requirements. Missing stories block analysis.""",
    "test_planning": """
Build a plan from accepted scenario analysis and supplied traffic/baselines.
Map every included scenario to a workload transaction. Distinguish open arrival-rate
from closed concurrency models; define transaction boundaries, pacing, think time,
units, ramp, steady-state duration, and workload shares totaling 100 percent.
Justify load, stress, endurance and spike tests individually. Define per-transaction
latency percentile/window, error-rate denominator, throughput and asynchronous
recovery thresholds. Compare only compatible baselines. Do not convert concurrency
to TPS without an evidenced response/pacing model. Missing traffic may support a
provisional proposal, never an invented approved target.
Include environment sizing, synthetic data, observability, entry/exit and hard-stop
criteria, dependencies, risks and approval needs. Required unevidenced targets
block an execution-ready plan. Workload entries must use stable scenario IDs.""",
    "env_triage": """
Assess readiness from supplied approved inventory/reference and observations.
Compare expected and observed values with source revisions and observation times.
For every mismatch state impact, severity, blocker status, supported remediation,
proposed owner role and verification. Compare targets against approved inventory;
hostname labels alone do not establish correctness. Unreachable, stale or partial
inspections are unknown, not healthy. State ready, warning, blocked or not_checked
with reasons; do not declare ready while required checks are missing. Recommend
changes only; do not claim to inspect or remediate without tool evidence.""",
    "script_scenario": """
Prepare script source or an immutable import reference from the accepted plan.
Use only the supplied execution platform, supported format and provider version;
if missing, report a blocker rather than selecting a platform. Include named
transactions, dynamic-value correlation before use, parameterized synthetic data,
assertions, pacing/think time, timeouts, safe logging, cleanup and dependencies.
Trace scripts to scenario IDs. State validation as not_run, passed or failed for
syntax/compilation, correlation, assertions, portability and provider preflight;
passed requires actual validation evidence. Source text is a draft until validated.
Execution uses approved run configuration and provider-owned workload selection.
Generated text does not deploy a script or select a remote workload. Describe the
required import/validation/approval handoff and any disagreement with the run's
execution settings. Never imply that prose changes execution parameters.""",
    "execution": """This phase is engine-controlled; its catalog text is for operator review.
No monitoring LLM consumes this prompt in the current execution spine. Review the
approved workload, immutable target, run limits, provider preflight and evidence
collection requirements. Engine configuration and gates govern start/stop actions.
Do not infer health or execution from this prompt. A future monitoring advisory
must receive timestamped telemetry, windows, baselines and explicit thresholds,
distinguish advice from actions, and report missing telemetry as unknown. Sustained
anomaly rules must never suppress an immediate configured hard-stop condition.""",
    "reporting": """
Analyze supplied execution evidence against the accepted plan and thresholds.
For each transaction show measured value, unit, percentile/window, target, evidence
reference and pass/fail/unknown. Preserve provider failure and partial-run limits;
absent evidence is not a pass. Compare baselines only when workload, environment,
versions and measurement windows are compatible. Separate executive impact from
technical findings; label bottleneck hypotheses and confidence with their evidence.
List severity, affected scope, metrics, proposed remediation and verification.
Provide defect drafts only for supported actionable findings; use verified source
IDs, leave unknown project/assignee fields unresolved, and never claim publication.
End with exclusions, accepted risks only when documented, and a qualified recommendation.""",
    "postmortem": """
Learn from the accepted report, execution evidence and supplied operator decisions.
Build a timestamped evidence timeline; distinguish triggers, contributing factors,
confirmed causes and hypotheses. Preserve uncertainty when causality is unresolved.
Assess detection, escalation and decisions without inferring unavailable dialogue.
Describe what worked and what should change. Each follow-up needs a stable action
ID, linked finding, proposed owner role, proposed due date or UNKNOWN, status,
measurable verification and supporting evidence. Do not invent assignments,
agreement, ticket IDs or completed actions. Missing decision records limit the
decision review; they do not justify fabricated history.""",
}


def migrate_catalog(execute) -> None:
    """Idempotent data statements also supported by Alembic offline SQL output."""
    metadata = sa.MetaData()
    prompts = sa.Table(
        "prompts",
        metadata,
        *[
            sa.Column(name, sa.String())
            for name in (
                "id",
                "namespace",
                "key",
                "phase",
                "application_id",
                "active_version_id",
                "description",
            )
        ],
        *[
            sa.Column(name, sa.Boolean())
            for name in ("selection_enabled", "is_default", "is_shared")
        ],
        sa.Column("archived_at", sa.DateTime(timezone=True)),
        schema="apex",
    )
    versions = sa.Table(
        "prompt_versions",
        metadata,
        *[sa.Column(name, sa.String()) for name in ("id", "prompt_id", "content", "created_by")],
        sa.Column("version", sa.Integer()),
        schema="apex",
    )
    applications = sa.Table("applications", metadata, sa.Column("id", sa.String()), schema="apex")
    other = prompts.alias("existing_default")
    for phase in PHASE_CONTENT:
        has_default = sa.exists(
            sa.select(other.c.id).where(
                other.c.namespace == "phase",
                other.c.phase == phase,
                other.c.is_default.is_(True),
            )
        )
        execute(
            prompts.update()
            .where(
                prompts.c.namespace == "phase",
                prompts.c.key == f"{phase}/system",
                prompts.c.phase.is_(None),
                prompts.c.selection_enabled.is_(False),
            )
            .values(
                phase=phase,
                selection_enabled=True,
                is_default=sa.and_(
                    ~has_default,
                    prompts.c.archived_at.is_(None),
                    prompts.c.active_version_id.is_not(None),
                ),
            )
        )
    execute(
        prompts.update()
        .where(
            prompts.c.namespace == "application",
            prompts.c.application_id.is_(None),
            prompts.c.is_shared.is_(False),
            prompts.c.key.in_(sa.select(applications.c.id)),
        )
        .values(application_id=prompts.c.key, selection_enabled=True)
    )

    def seed(namespace, phase, content):
        family = [other.c.namespace == namespace, other.c.is_default.is_(True)]
        if phase:
            family.append(other.c.phase == phase)
        has_default = sa.exists(sa.select(other.c.id).where(*family))
        prompt_id = f"apex-phase-{phase}" if phase else "apex-shared-system-default"
        key = f"{phase}/default" if phase else "default"
        key_taken = sa.exists(
            sa.select(other.c.id).where(
                other.c.namespace == namespace,
                other.c.key == key,
            )
        )
        execute(
            prompts.insert().from_select(
                [
                    "id",
                    "namespace",
                    "key",
                    "phase",
                    "description",
                    "selection_enabled",
                    "is_default",
                ],
                sa.select(
                    sa.literal(prompt_id),
                    sa.literal(namespace),
                    sa.case((key_taken, sa.literal(key + "-0043")), else_=sa.literal(key)),
                    sa.literal(phase, sa.String()),
                    sa.literal(
                        "Default phase context" if phase else "Shared pipeline system prompt"
                    ),
                    sa.true(),
                    sa.false(),
                ).where(
                    ~has_default, ~sa.exists(sa.select(other.c.id).where(other.c.id == prompt_id))
                ),
            )
        )
        version_id = f"{prompt_id}-v1"
        execute(
            versions.insert().from_select(
                ["id", "prompt_id", "version", "content", "created_by"],
                sa.select(
                    sa.literal(version_id),
                    prompts.c.id,
                    sa.literal(1),
                    sa.literal(content),
                    sa.literal("migration:0043"),
                ).where(
                    prompts.c.id == prompt_id,
                    ~sa.exists(sa.select(versions.c.id).where(versions.c.prompt_id == prompt_id)),
                ),
            )
        )
        execute(
            prompts.update()
            .where(
                prompts.c.id == prompt_id,
                prompts.c.active_version_id.is_(None),
                prompts.c.archived_at.is_(None),
                prompts.c.selection_enabled.is_(True),
                ~has_default,
            )
            .values(active_version_id=version_id, is_default=True)
        )

    seed("system", None, SYSTEM_CONTENT)
    for phase, content in PHASE_CONTENT.items():
        seed("phase", phase, content)


def downgrade() -> None:
    op.drop_constraint("prompt_default_eligible", "prompts", schema="apex", type_="check")
    op.drop_index("uq_prompts_default_phase", table_name="prompts", schema="apex")
    op.drop_index("uq_prompts_default_system", table_name="prompts", schema="apex")
    op.drop_constraint("fk_prompts_environment", "prompts", schema="apex", type_="foreignkey")
    op.drop_constraint("fk_prompts_application", "prompts", schema="apex", type_="foreignkey")
    for name in (
        "selection_enabled",
        "is_default",
        "is_shared",
        "environment_id",
        "application_id",
        "phase",
    ):
        op.drop_column("prompts", name, schema="apex")

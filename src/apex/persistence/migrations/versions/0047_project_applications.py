"""Applications serve many projects: project_applications link table, global names.

Before this revision an application belonged to exactly one project
(``applications.project_id``), so a system used by several projects had to be
registered once per project, each copy with its own environments. This
revision:

1. creates ``project_applications`` and links every application to its project;
2. merges applications with the same name into one linked to every project the
   copies served. The kept copy is an active one if any is, else the oldest.
   Environments move to the kept application. Two environments with the same
   name merge only when their target (kind, base URL, approval, options) is
   identical; otherwise the moved one is renamed "<name> (<old project>)", so a
   project's tests never silently switch to another project's target;
3. repoints live references to merged ids: prompts, environment snapshots,
   documents and cycles. Consumers keep their old app scopes and gain the same
   scope on the kept application. Run history (run participants and their scope
   snapshots, engine runs, usage/agent events, artifacts, prompt usages) keeps
   the ids it was recorded with; ``catalog_merges`` maps each old id to the kept
   one, and catalog lookups resolve through it;
4. drops ``applications.project_id`` and makes application names unique.

The merge refuses to run while a cycle on a copy being merged has an active
dispatch. The downgrade restores one project per application (the first linked
project) but cannot split merged rows apart.
"""

import sqlalchemy as sa
from alembic import op

revision = "0047"
down_revision = "0046"
branch_labels = None
depends_on = None

# Live (configuration) references to an application id, repointed on merge.
_APPLICATION_REFS = (
    ("prompts", "application_id"),
    ("documents", "app_id"),
    ("test_cycles", "app_id"),
)
# Live references to an environment id, repointed when two environments merge.
_ENVIRONMENT_REFS = (
    ("prompts", "environment_id"),
    ("environment_snapshots", "environment_id"),
    ("test_cycles", "environment_id"),
)


def _repoint_sql(refs, old: str, new: str) -> str:
    return "\n".join(
        f"UPDATE apex.{table} SET {column} = {new} WHERE {column} = {old};"
        for table, column in refs
    )


# Pure SQL (PL/pgSQL) so the merge also runs in offline mode (`alembic upgrade --sql`).
_MERGE_DUPLICATES = f"""
DO $$
DECLARE
    dup RECORD;
    old_env RECORD;
    kept text;
    twin text;
    moved_name text;
BEGIN
    FOR dup IN
        SELECT a.id, a.project_id,
               first_value(a.id) OVER (
                   PARTITION BY a.name
                   ORDER BY (a.archived_at IS NOT NULL), a.created_at, a.id
               ) AS kept_id
        FROM apex.applications a
        WHERE a.name IN (SELECT name FROM apex.applications GROUP BY name HAVING count(*) > 1)
        ORDER BY a.name, (a.archived_at IS NOT NULL), a.created_at, a.id
    LOOP
        CONTINUE WHEN dup.id = dup.kept_id;
        kept := dup.kept_id;
        -- In-flight cycle work carries the old ids in its frozen configuration.
        IF EXISTS (
            SELECT 1 FROM apex.cycle_execution_dispatches d
            JOIN apex.test_cycles c ON c.id = d.cycle_id
            WHERE d.active_environment_id IS NOT NULL AND c.app_id = dup.id
        ) THEN
            RAISE EXCEPTION USING MESSAGE = 'application ' || dup.id || ' has an active cycle '
                || 'dispatch; let it finish, then rerun the migration';
        END IF;
        INSERT INTO apex.project_applications (project_id, application_id)
        VALUES (dup.project_id, kept) ON CONFLICT DO NOTHING;
        FOR old_env IN
            SELECT id, name, kind, base_url, target_approved, options
            FROM apex.environments WHERE application_id = dup.id
        LOOP
            twin := NULL;
            SELECT id INTO twin FROM apex.environments
            WHERE application_id = kept AND name = old_env.name;
            IF twin IS NOT NULL AND NOT EXISTS (
                SELECT 1 FROM apex.environments t
                WHERE t.id = twin
                  AND t.kind IS NOT DISTINCT FROM old_env.kind
                  AND t.base_url IS NOT DISTINCT FROM old_env.base_url
                  AND t.target_approved = old_env.target_approved
                  AND t.options::jsonb = old_env.options::jsonb
            ) THEN
                -- Same name, different target: keep both, named apart.
                moved_name := old_env.name || ' (' || dup.project_id || ')';
                IF EXISTS (
                    SELECT 1 FROM apex.environments
                    WHERE application_id = kept AND name = moved_name
                ) THEN
                    moved_name := moved_name || ' ' || left(old_env.id, 8);
                END IF;
                UPDATE apex.environments SET name = left(moved_name, 255), application_id = kept
                WHERE id = old_env.id;
            ELSIF twin IS NULL THEN
                UPDATE apex.environments SET application_id = kept WHERE id = old_env.id;
            ELSE
                {_repoint_sql(_ENVIRONMENT_REFS, "old_env.id", "twin")}
                DELETE FROM apex.environments WHERE id = old_env.id;
                INSERT INTO apex.catalog_merges (old_id, kind, new_id, old_project_id)
                VALUES (old_env.id, 'environment', twin, dup.project_id);
            END IF;
        END LOOP;
        -- Old app scopes stay (run history still names the old id); add the same scope
        -- on the kept application.
        INSERT INTO apex.consumer_scopes (id, consumer_id, project_id, app_id)
        SELECT md5(s.id || kept), s.consumer_id, s.project_id, kept
        FROM apex.consumer_scopes s
        WHERE s.app_id = dup.id AND NOT EXISTS (
            SELECT 1 FROM apex.consumer_scopes o
            WHERE o.consumer_id = s.consumer_id AND o.project_id = s.project_id
              AND o.app_id = kept
        );
        {_repoint_sql(_APPLICATION_REFS, "dup.id", "kept")}
        DELETE FROM apex.applications WHERE id = dup.id;
        INSERT INTO apex.catalog_merges (old_id, kind, new_id, old_project_id)
        VALUES (dup.id, 'application', kept, dup.project_id);
    END LOOP;
END $$
"""


def upgrade() -> None:
    op.create_table(
        "project_applications",
        sa.Column("project_id", sa.String(255), nullable=False),
        sa.Column("application_id", sa.String(32), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["application_id"],
            ["apex.applications.id"],
            name=op.f("fk_project_applications_applications_application_id"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint(
            "project_id", "application_id", name=op.f("pk_project_applications")
        ),
        schema="apex",
    )
    op.create_index(
        "ix_project_applications_application_id",
        "project_applications",
        ["application_id"],
        schema="apex",
    )
    op.create_table(
        "catalog_merges",
        sa.Column("old_id", sa.String(32), nullable=False),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("new_id", sa.String(32), nullable=False),
        sa.Column("old_project_id", sa.String(255), nullable=True),
        sa.Column(
            "merged_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.PrimaryKeyConstraint("old_id", name=op.f("pk_catalog_merges")),
        schema="apex",
    )
    op.execute(
        "INSERT INTO apex.project_applications (project_id, application_id) "
        "SELECT project_id, id FROM apex.applications"
    )
    op.execute(_MERGE_DUPLICATES)
    op.drop_index("ix_applications_project_id", table_name="applications", schema="apex")
    op.drop_constraint("uq_applications_project_id", "applications", schema="apex", type_="unique")
    op.drop_column("applications", "project_id", schema="apex")
    op.create_unique_constraint(
        op.f("uq_applications_name"), "applications", ["name"], schema="apex"
    )


def downgrade() -> None:
    op.drop_constraint("uq_applications_name", "applications", schema="apex", type_="unique")
    op.add_column(
        "applications", sa.Column("project_id", sa.String(255), nullable=True), schema="apex"
    )
    op.execute(
        "UPDATE apex.applications a SET project_id = ("
        "  SELECT min(project_id) FROM apex.project_applications l WHERE l.application_id = a.id)"
    )
    op.execute("UPDATE apex.applications SET project_id = '' WHERE project_id IS NULL")
    op.alter_column("applications", "project_id", nullable=False, schema="apex")
    op.create_unique_constraint(
        op.f("uq_applications_project_id"), "applications", ["project_id", "name"], schema="apex"
    )
    op.create_index("ix_applications_project_id", "applications", ["project_id"], schema="apex")
    op.drop_table("catalog_merges", schema="apex")
    op.drop_index(
        "ix_project_applications_application_id", table_name="project_applications", schema="apex"
    )
    op.drop_table("project_applications", schema="apex")

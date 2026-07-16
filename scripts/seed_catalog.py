"""Seed the demo catalog + one stub connection per port kind (idempotent by name).

Creates:
- application "Checkout" in project "demo"
- environment "staging-2" under it with 2 hosts (hosts only on first create)
- one global connection per port kind: "stub-<kind>" using the same provider as
  DEV_CONNECTIONS (stub everywhere, "env" for secrets), plus "sim-engine" for
  the execution engine (provider "sim")
- global connection "minio-artifacts" (artifact_store/s3) pointing at the dev
  MinIO from docker-compose.dev.yaml — requires APEX_INTEGRATION_MINIO_SECRET_KEY at runtime

Graceful if the database is unreachable. Run: uv run python scripts/seed_catalog.py
"""

import asyncio
import sys

from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from apex.adapters.registry import PortKind
from apex.domain.diagnostics import safe_type_name
from apex.persistence.db import get_sessionmaker
from apex.persistence.models import Application, Connection, Environment, EnvironmentHost
from apex.services.connections import DEV_CONNECTIONS

DEMO_PROJECT = "demo"
DEMO_APP_NAME = "Checkout"
DEMO_ENV_NAME = "staging-2"
DEMO_HOSTS: tuple[tuple[str, str], ...] = (
    ("checkout-app-01.staging2.local", "app"),
    ("checkout-db-01.staging2.local", "db"),
)

# Dev MinIO from docker-compose.dev.yaml (root user "apex" / "apex-minio").
MINIO_CONNECTION_NAME = "minio-artifacts"
MINIO_OPTIONS: dict[str, object] = {
    "endpoint": "localhost:9000",
    "bucket": "apex-artifacts",
    "secure": False,
    "access_key": "apex",
    "_apex_trusted_private_host": True,
}
MINIO_SECRET_REF = "env:APEX_INTEGRATION_MINIO_SECRET_KEY"


def _seed_connections() -> list[Connection]:
    """One global connection per port kind, named for discoverability."""
    rows: list[Connection] = []
    for kind in PortKind:
        provider = DEV_CONNECTIONS[kind].provider
        name = "sim-engine" if kind is PortKind.EXECUTION_ENGINE else f"stub-{kind.value}"
        rows.append(
            Connection(
                kind=kind.value,
                provider=provider,
                name=name,
                project_id=None,  # global
                options={},
            )
        )
    return rows


async def _seed_application(session: AsyncSession) -> Application:
    app = await session.scalar(
        select(Application).where(
            Application.project_id == DEMO_PROJECT, Application.name == DEMO_APP_NAME
        )
    )
    if app is not None:
        print(f"application {DEMO_APP_NAME!r}: already exists (id={app.id})")
        return app
    app = Application(
        project_id=DEMO_PROJECT,
        name=DEMO_APP_NAME,
        description="Demo checkout service (seeded)",
    )
    session.add(app)
    await session.flush()
    print(f"application {DEMO_APP_NAME!r}: created (id={app.id})")
    return app


async def _seed_environment(session: AsyncSession, app: Application) -> None:
    env = await session.scalar(
        select(Environment).where(
            Environment.application_id == app.id, Environment.name == DEMO_ENV_NAME
        )
    )
    if env is not None:
        print(f"environment {DEMO_ENV_NAME!r}: already exists (id={env.id})")
        return
    env = Environment(
        application_id=app.id,
        name=DEMO_ENV_NAME,
        kind="vm",
        base_url="https://checkout.staging2.local",
        options={},
    )
    env.hosts = [EnvironmentHost(hostname=hostname, role=role) for hostname, role in DEMO_HOSTS]
    session.add(env)
    await session.flush()
    print(f"environment {DEMO_ENV_NAME!r}: created with {len(DEMO_HOSTS)} hosts (id={env.id})")


async def _seed_connection_rows(session: AsyncSession) -> None:
    for row in _seed_connections():
        existing = await session.scalar(select(Connection).where(Connection.name == row.name))
        if existing is not None:
            print(f"connection {row.name!r}: already exists (id={existing.id})")
            continue
        session.add(row)
        await session.flush()
        print(f"connection {row.name!r}: created ({row.kind}/{row.provider}, global)")


async def _seed_minio_connection(session: AsyncSession) -> None:
    existing = await session.scalar(
        select(Connection).where(Connection.name == MINIO_CONNECTION_NAME)
    )
    if existing is not None:
        print(f"connection {MINIO_CONNECTION_NAME!r}: already exists (id={existing.id})")
        return
    row = Connection(
        kind=PortKind.ARTIFACT_STORE.value,
        provider="s3",
        name=MINIO_CONNECTION_NAME,
        project_id=None,  # global
        options=dict(MINIO_OPTIONS),
        secret_ref=MINIO_SECRET_REF,
        enabled=True,
    )
    session.add(row)
    await session.flush()
    print(f"connection {MINIO_CONNECTION_NAME!r}: created (artifact_store/s3, global)")
    print(
        "NOTE: the DB-backed connection resolver prefers DB rows over the static "
        "DEV_CONNECTIONS map, so transcripts/documents/engine artifacts now go to "
        f"MinIO at {MINIO_OPTIONS['endpoint']} (bucket {MINIO_OPTIONS['bucket']!r}) in dev. "
        f"Ensure APEX_INTEGRATION_MINIO_SECRET_KEY is set (see .env.example) and MinIO is up "
        "(docker-compose.dev.yaml)."
    )


async def main() -> int:
    try:
        async with get_sessionmaker()() as session:
            app = await _seed_application(session)
            await _seed_environment(session, app)
            await _seed_connection_rows(session)
            await _seed_minio_connection(session)
            await session.commit()
    except (SQLAlchemyError, OSError) as exc:
        print(f"Database unreachable ({safe_type_name(exc)}).")
        print("Run `make infra-up` + `make migrate` first, then re-run this script.")
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))

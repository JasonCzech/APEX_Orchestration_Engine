"""Apply a validated BootstrapDocument idempotently against the apex schema.

Every step is idempotent by natural key (so re-running the Helm hook is a no-op
once converged), mirroring the dev seed scripts it consolidates. The initial
admin key is read from the environment, hashed, and never logged.
"""

from __future__ import annotations

import secrets as _secrets
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from sqlalchemy import select

from apex.auth.service import hash_api_key
from apex.bootstrap.schema import BootstrapDocument
from apex.persistence.models import (
    ApiConsumer,
    Application,
    Connection,
    ConsumerScope,
    Environment,
    EnvironmentHost,
)
from apex.services.connections import (
    TRUSTED_PRIVATE_HOST_OPTION,
    validate_adapter_base_url,
)

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


@dataclass
class BootstrapReport:
    prompts_created: list[str] = field(default_factory=list)
    applications_created: list[str] = field(default_factory=list)
    environments_created: list[str] = field(default_factory=list)
    connections_created: list[str] = field(default_factory=list)
    admin_created: str | None = None
    admin_existing: str | None = None

    def summary(self) -> str:
        admin = "created" if self.admin_created else "exists" if self.admin_existing else "skipped"
        return (
            f"prompts +{len(self.prompts_created)}, "
            f"applications +{len(self.applications_created)}, "
            f"environments +{len(self.environments_created)}, "
            f"connections +{len(self.connections_created)}, "
            f"admin={admin}"
        )


class BootstrapError(RuntimeError):
    """A bootstrap input is invalid in a way validation cannot catch (e.g. a missing
    referenced application or an unset admin-key env var)."""


async def apply_document(
    doc: BootstrapDocument,
    session: AsyncSession,
    *,
    env: Mapping[str, str],
    log: Callable[[str], None] = print,
) -> BootstrapReport:
    """Apply `doc` to the database. Caller owns the transaction (commit/rollback)."""
    report = BootstrapReport()
    if doc.seed_default_prompts:
        await _seed_default_prompts(session, report, log)
    await _apply_applications(doc, session, report, log)
    await _apply_environments(doc, session, report, log)
    await _apply_connections(doc, session, report, log)
    await _apply_admin(doc, session, env, report, log)
    return report


async def _seed_default_prompts(
    session: AsyncSession, report: BootstrapReport, log: Callable[[str], None]
) -> None:
    # Imported lazily: prompt seeding pulls the phase-prompt catalog, which most
    # production bootstraps leave to the app's own defaults.
    from apex.persistence.repositories.prompts import PromptRepository
    from apex.services.prompts import DEFAULT_PHASE_PROMPTS, PHASE_NAMESPACE, PromptCatalogService

    # Bootstrap's caller owns the transaction. Prompt writes therefore flush into
    # that transaction instead of committing the prompt subset independently.
    repo = PromptRepository(session, commit_on_write=False)
    catalog = PromptCatalogService(repo)
    for key in sorted(DEFAULT_PHASE_PROMPTS):
        if await repo.get_by_key(PHASE_NAMESPACE, key) is not None:
            log(f"prompt {PHASE_NAMESPACE}/{key}: exists; unchanged")
            continue
        phase, _, part = key.partition("/")
        await catalog.create_prompt(
            namespace=PHASE_NAMESPACE,
            key=key,
            content=DEFAULT_PHASE_PROMPTS[key],
            description=f"Built-in {part} prompt for the {phase} phase",
            note="seeded by apex.bootstrap",
            created_by="apex.bootstrap",
        )
        report.prompts_created.append(f"{PHASE_NAMESPACE}/{key}")
        log(f"prompt {PHASE_NAMESPACE}/{key}: created")


async def _apply_applications(
    doc: BootstrapDocument,
    session: AsyncSession,
    report: BootstrapReport,
    log: Callable[[str], None],
) -> None:
    for spec in doc.applications:
        existing = await session.scalar(
            select(Application).where(
                Application.project_id == spec.project_id, Application.name == spec.name
            )
        )
        if existing is not None:
            log(f"application {spec.project_id}/{spec.name}: exists (id={existing.id})")
            continue
        session.add(
            Application(project_id=spec.project_id, name=spec.name, description=spec.description)
        )
        await session.flush()
        report.applications_created.append(f"{spec.project_id}/{spec.name}")
        log(f"application {spec.project_id}/{spec.name}: created")


async def _apply_environments(
    doc: BootstrapDocument,
    session: AsyncSession,
    report: BootstrapReport,
    log: Callable[[str], None],
) -> None:
    for spec in doc.environments:
        app = await session.scalar(
            select(Application).where(
                Application.project_id == spec.project_id, Application.name == spec.application
            )
        )
        if app is None:
            raise BootstrapError(
                f"environment {spec.name!r} references application "
                f"{spec.project_id}/{spec.application!r}, which does not exist; declare it "
                "under `applications` (or in a prior run)"
            )
        if spec.base_url:
            try:
                validate_adapter_base_url(
                    spec.base_url,
                    allow_private_hosts=spec.options.get(TRUSTED_PRIVATE_HOST_OPTION) is True
                    or None,
                )
            except ValueError as exc:
                raise BootstrapError(
                    f"environment {spec.application}/{spec.name} has an invalid target: {exc}"
                ) from exc
        existing = await session.scalar(
            select(Environment).where(
                Environment.application_id == app.id, Environment.name == spec.name
            )
        )
        if existing is not None:
            # Migration 0013 deliberately revokes legacy executable targets.
            # A trusted bootstrap document may re-approve an unchanged target,
            # but never silently rewrites a row that drifted from the document.
            if (
                spec.base_url
                and existing.base_url == spec.base_url
                and dict(existing.options or {}) == dict(spec.options)
                and not existing.target_approved
            ):
                existing.target_approved = True
                existing.target_version = int(existing.target_version or 0) + 1
                await session.flush()
                log(
                    f"environment {spec.application}/{spec.name}: approved existing target "
                    f"(id={existing.id})"
                )
            else:
                log(f"environment {spec.application}/{spec.name}: exists (id={existing.id})")
            continue
        env_row = Environment(
            application_id=app.id,
            name=spec.name,
            kind=spec.kind,
            base_url=spec.base_url,
            options=dict(spec.options),
            target_approved=bool(spec.base_url),
            target_version=1 if spec.base_url else 0,
        )
        env_row.hosts = [EnvironmentHost(hostname=h.hostname, role=h.role) for h in spec.hosts]
        session.add(env_row)
        await session.flush()
        report.environments_created.append(f"{spec.application}/{spec.name}")
        log(f"environment {spec.application}/{spec.name}: created with {len(spec.hosts)} host(s)")


async def _apply_connections(
    doc: BootstrapDocument,
    session: AsyncSession,
    report: BootstrapReport,
    log: Callable[[str], None],
) -> None:
    for spec in doc.connections:
        existing = await session.scalar(select(Connection).where(Connection.name == spec.name))
        if existing is not None:
            expected = {
                "kind": spec.kind.value,
                "provider": spec.provider,
                "project_id": spec.project_id,
                "base_url": spec.base_url,
                "options": dict(spec.options),
                "secret_ref": spec.secret_ref,
                "enabled": spec.enabled,
            }
            drift = sorted(
                field for field, value in expected.items() if getattr(existing, field) != value
            )
            if drift:
                raise BootstrapError(
                    f"connection {spec.name!r} differs from bootstrap configuration "
                    f"({', '.join(drift)}); create a versioned replacement or reconcile it "
                    "explicitly before bootstrap"
                )
            log(f"connection {spec.name}: exists and matches (id={existing.id})")
            continue
        session.add(
            Connection(
                kind=spec.kind.value,
                provider=spec.provider,
                name=spec.name,
                project_id=spec.project_id,
                base_url=spec.base_url,
                options=dict(spec.options),
                secret_ref=spec.secret_ref,
                enabled=spec.enabled,
            )
        )
        await session.flush()
        report.connections_created.append(spec.name)
        log(f"connection {spec.name}: created ({spec.kind.value}/{spec.provider})")


async def _apply_admin(
    doc: BootstrapDocument,
    session: AsyncSession,
    env: Mapping[str, str],
    report: BootstrapReport,
    log: Callable[[str], None],
) -> None:
    spec = doc.admin
    if spec is None:
        return
    existing = await session.scalar(select(ApiConsumer).where(ApiConsumer.name == spec.name))
    if existing is not None:
        report.admin_existing = spec.name
        log(f"admin consumer {spec.name!r}: exists; key unchanged")
        return
    plaintext = (env.get(spec.key_env) or "").strip()
    if not plaintext:
        raise BootstrapError(
            f"admin consumer {spec.name!r} requested but ${spec.key_env} is unset/empty; "
            "supply the initial admin key via that environment variable (K8s Secret / Key Vault)"
        )
    consumer = ApiConsumer(
        name=spec.name,
        key_hash=hash_api_key(plaintext),
        consumer_type=spec.consumer_type.value,
        role=spec.role.value,
        scopes=[ConsumerScope(project_id=s.project_id, app_id=s.app_id) for s in spec.scopes],
    )
    session.add(consumer)
    await session.flush()
    report.admin_created = spec.name
    # NB: plaintext is never logged. The operator already holds it (they supplied it).
    log(
        f"admin consumer {spec.name!r}: created "
        f"(key hashed from ${spec.key_env}; role={spec.role.value})"
    )


def generate_admin_key() -> str:
    """A URL-safe key for operators who want the bootstrap to mint one for them."""
    return _secrets.token_urlsafe(32)

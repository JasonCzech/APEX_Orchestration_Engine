"""Apply a validated BootstrapDocument idempotently against the apex schema.

Every step is idempotent by natural key (so re-running the Helm hook is a no-op
once converged), mirroring the dev seed scripts it consolidates. The initial
admin key is read from the environment, hashed, and never logged.
"""

from __future__ import annotations

import re
import secrets as _secrets
import unicodedata
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import func, select
from sqlalchemy.orm import selectinload

from apex.adapters.registry import ConnectionConfig
from apex.auth.service import _candidate_key_hashes, hash_api_key
from apex.bootstrap.schema import AdminConsumerSpec, BootstrapDocument, EnvironmentSpec
from apex.domain.diagnostics import bounded_diagnostic
from apex.persistence.models import (
    ApiConsumer,
    Application,
    Connection,
    ConsumerKey,
    ConsumerScope,
    Environment,
    EnvironmentHost,
)
from apex.services.connections import (
    TRUSTED_PRIVATE_HOST_OPTION,
    validate_adapter_base_url,
    validate_connection_config,
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


BOOTSTRAP_DIAGNOSTIC_MAX_CHARS = 4_096


def safe_bootstrap_diagnostic(value: Any) -> str:
    """Return a bounded, credential-redacted, single-line CLI diagnostic."""

    # First bound/redact the original, then escape every terminal/log control.
    # A second redaction pass catches credential assignments split by a newline
    # in the source value (the escaped ``\\n`` is now ordinary text).
    rendered = bounded_diagnostic(
        value,
        max_chars=BOOTSTRAP_DIAGNOSTIC_MAX_CHARS * 4,
    )
    single_line = "".join(_safe_bootstrap_character(character) for character in rendered)
    return bounded_diagnostic(
        single_line,
        max_chars=BOOTSTRAP_DIAGNOSTIC_MAX_CHARS,
    )


def _safe_bootstrap_character(character: str) -> str:
    codepoint = ord(character)
    if character == "\n":
        return r"\n"
    if character == "\r":
        return r"\r"
    if character == "\t":
        return r"\t"
    category = unicodedata.category(character)
    if category.startswith("C") or category in {"Zl", "Zp"}:
        return f"\\x{codepoint:02x}" if codepoint <= 0xFF else f"\\u{codepoint:04x}"
    return character


class BootstrapError(RuntimeError):
    """A bootstrap input is invalid in a way validation cannot catch (e.g. a missing
    referenced application or an unset admin-key env var)."""

    def __init__(self, message: Any) -> None:
        super().__init__(safe_bootstrap_diagnostic(message))


_MINIO_SHORT_ENDPOINT = "apex-minio:9000"
_MINIO_CLUSTER_ENDPOINT = re.compile(
    r"apex-minio\.[a-z0-9](?:[-a-z0-9]{0,61}[a-z0-9])?\.svc\.cluster\.local:9000"
)


def _is_minio_endpoint_alias_pair(current: object, expected: object) -> bool:
    """Accept only short-name/FQDN pairs for the chart-managed MinIO Service."""

    if not isinstance(current, str) or not isinstance(expected, str):
        return False
    endpoints = {current, expected}
    if len(endpoints) != 2 or _MINIO_SHORT_ENDPOINT not in endpoints:
        return False
    fqdn = next(endpoint for endpoint in endpoints if endpoint != _MINIO_SHORT_ENDPOINT)
    return _MINIO_CLUSTER_ENDPOINT.fullmatch(fqdn) is not None


def _reconcile_known_connection_alias(
    existing: Connection, expected: dict[str, object], drift: list[str]
) -> bool:
    """Apply narrowly-scoped, equivalent in-cluster endpoint migrations."""

    if existing.name != "minio-artifacts" or drift != ["options"]:
        return False
    current_options = dict(existing.options or {})
    raw_expected_options = expected.get("options")
    if not isinstance(raw_expected_options, Mapping):
        return False
    expected_options = dict(raw_expected_options)
    current_endpoint = current_options.pop("endpoint", None)
    expected_endpoint = expected_options.pop("endpoint", None)
    if current_options != expected_options or not _is_minio_endpoint_alias_pair(
        current_endpoint, expected_endpoint
    ):
        return False
    existing.options = expected_options | {"endpoint": expected_endpoint}
    existing.runtime_version = func.now()
    return True


def _environment_drift_fields(existing: Environment, spec: EnvironmentSpec) -> list[str]:
    """Compare a persisted environment aggregate without exposing field values."""

    drift: list[str] = []
    expected = {
        "kind": spec.kind,
        "base_url": spec.base_url,
        "options": dict(spec.options),
    }
    for field_name, value in expected.items():
        current = getattr(existing, field_name)
        if field_name == "options":
            current = dict(current or {})
        if current != value:
            drift.append(field_name)
    expected_hosts = sorted((host.hostname, host.role) for host in spec.hosts)
    actual_hosts = sorted((host.hostname, host.role) for host in existing.hosts)
    if actual_hosts != expected_hosts:
        drift.append("hosts")
    return drift


async def apply_document(
    doc: BootstrapDocument,
    session: AsyncSession,
    *,
    env: Mapping[str, str],
    log: Callable[[str], None] = print,
) -> BootstrapReport:
    """Apply `doc` to the database. Caller owns the transaction (commit/rollback)."""

    def safe_log(message: str) -> None:
        log(safe_bootstrap_diagnostic(message))

    report = BootstrapReport()
    if doc.seed_default_prompts:
        await _seed_default_prompts(session, report, safe_log)
    await _apply_applications(doc, session, report, safe_log)
    await _apply_environments(doc, session, report, safe_log)
    await _apply_connections(doc, session, report, safe_log)
    await _apply_admin(doc, session, env, report, safe_log)
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
            select(Application)
            .where(Application.project_id == spec.project_id, Application.name == spec.name)
            .with_for_update()
        )
        if existing is not None:
            drift = []
            if existing.description != spec.description:
                drift.append("description")
            if existing.archived_at is not None:
                drift.append("archived_at")
            if drift:
                raise BootstrapError(
                    f"application {spec.project_id!r}/{spec.name!r} differs from bootstrap "
                    f"configuration ({', '.join(drift)}); reconcile it explicitly before "
                    "bootstrap"
                )
            log(
                f"application {spec.project_id!r}/{spec.name!r}: exists and matches "
                f"(id={existing.id})"
            )
            continue
        session.add(
            Application(project_id=spec.project_id, name=spec.name, description=spec.description)
        )
        await session.flush()
        report.applications_created.append(f"{spec.project_id}/{spec.name}")
        log(f"application {spec.project_id!r}/{spec.name!r}: created")


async def _apply_environments(
    doc: BootstrapDocument,
    session: AsyncSession,
    report: BootstrapReport,
    log: Callable[[str], None],
) -> None:
    for spec in doc.environments:
        app = await session.scalar(
            select(Application)
            .where(Application.project_id == spec.project_id, Application.name == spec.application)
            .with_for_update()
        )
        if app is None:
            raise BootstrapError(
                f"environment {spec.name!r} references application "
                f"{spec.project_id!r}/{spec.application!r}, which does not exist; declare it "
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
                    f"environment {spec.application!r}/{spec.name!r} has an invalid target: {exc}"
                ) from exc
        existing = await session.scalar(
            select(Environment)
            .where(Environment.application_id == app.id, Environment.name == spec.name)
            .options(selectinload(Environment.hosts))
            .with_for_update()
        )
        if existing is not None:
            drift = _environment_drift_fields(existing, spec)
            if drift:
                raise BootstrapError(
                    f"environment {spec.application!r}/{spec.name!r} differs from bootstrap "
                    f"configuration ({', '.join(drift)}); reconcile it explicitly before "
                    "bootstrap"
                )
            # Migration 0013 deliberately revokes legacy executable targets.
            # A trusted bootstrap document may re-approve an unchanged target,
            # but never silently rewrites an aggregate that drifted from the document.
            if spec.base_url and not existing.target_approved:
                existing.target_approved = True
                existing.target_version = int(existing.target_version or 0) + 1
                await session.flush()
                log(
                    f"environment {spec.application!r}/{spec.name!r}: approved existing target "
                    f"(id={existing.id})"
                )
            else:
                log(
                    f"environment {spec.application!r}/{spec.name!r}: exists and matches "
                    f"(id={existing.id})"
                )
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
        log(
            f"environment {spec.application!r}/{spec.name!r}: created with "
            f"{len(spec.hosts)} host(s)"
        )


async def _apply_connections(
    doc: BootstrapDocument,
    session: AsyncSession,
    report: BootstrapReport,
    log: Callable[[str], None],
) -> None:
    for spec in doc.connections:
        options = dict(spec.options)
        if spec.base_url:
            options.setdefault("base_url", spec.base_url)
        try:
            # Validate before reading or writing a row so a bad bootstrap
            # document cannot persist a transport that only fails at first use.
            validate_adapter_base_url(
                spec.base_url,
                allow_private_hosts=options.get(TRUSTED_PRIVATE_HOST_OPTION) is True or None,
            )
            validate_connection_config(
                ConnectionConfig(
                    id=f"bootstrap:{spec.name}",
                    kind=spec.kind,
                    provider=spec.provider,
                    name=spec.name,
                    options=options,
                    secret_ref=spec.secret_ref,
                )
            )
        except ValueError as exc:
            raise BootstrapError(
                f"connection {spec.name!r} has an invalid transport: {exc}"
            ) from exc
        existing = await session.scalar(
            select(Connection).where(Connection.name == spec.name).with_for_update()
        )
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
                if _reconcile_known_connection_alias(existing, expected, drift):
                    await session.flush()
                    log(
                        f"connection {spec.name!r}: reconciled equivalent in-cluster endpoint "
                        f"(id={existing.id})"
                    )
                    continue
                raise BootstrapError(
                    f"connection {spec.name!r} differs from bootstrap configuration "
                    f"({', '.join(drift)}); create a versioned replacement or reconcile it "
                    "explicitly before bootstrap"
                )
            log(f"connection {spec.name!r}: exists and matches (id={existing.id})")
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
        log(f"connection {spec.name!r}: created ({spec.kind.value!r}/{spec.provider!r})")


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
    plaintext = (env.get(spec.key_env) or "").strip()
    if not plaintext:
        raise BootstrapError(
            f"admin consumer {spec.name!r} requested but ${spec.key_env} is unset/empty; "
            "supply the initial admin key via that environment variable (K8s Secret / Key Vault)"
        )
    key_hash = hash_api_key(plaintext)
    candidate_key_hashes = _candidate_key_hashes(plaintext)
    existing = await session.scalar(
        select(ApiConsumer)
        .where(ApiConsumer.name == spec.name)
        .options(selectinload(ApiConsumer.scopes), selectinload(ApiConsumer.keys))
        # Consumer rotation/update takes this same aggregate lock. Keep the
        # authority/key proof stable until the bootstrap transaction commits.
        .with_for_update()
    )
    if existing is not None:
        drift = _admin_drift_fields(existing, spec, candidate_key_hashes)
        if drift:
            raise BootstrapError(
                f"admin consumer {spec.name!r} differs from bootstrap configuration "
                f"({', '.join(drift)}); reconcile the consumer and mounted bootstrap "
                "key explicitly before bootstrap"
            )
        report.admin_existing = spec.name
        log(f"admin consumer {spec.name!r}: exists and matches; key verified")
        return
    consumer = ApiConsumer(
        name=spec.name,
        key_hash=key_hash,
        consumer_type=spec.consumer_type.value,
        role=spec.role.value,
        scopes=[ConsumerScope(project_id=s.project_id, app_id=s.app_id) for s in spec.scopes],
        keys=[
            ConsumerKey(
                key_hash=key_hash,
                expiry_source="independent",
                created_by="bootstrap",
            )
        ],
        created_by="bootstrap",
        updated_by="bootstrap",
    )
    session.add(consumer)
    await session.flush()
    report.admin_created = spec.name
    # NB: plaintext is never logged. The operator already holds it (they supplied it).
    log(
        f"admin consumer {spec.name!r}: created "
        f"(key hashed from ${spec.key_env}; role={spec.role.value})"
    )


def _admin_drift_fields(
    existing: ApiConsumer,
    spec: AdminConsumerSpec,
    candidate_key_hashes: tuple[str, ...],
) -> list[str]:
    """Return only safe field labels when an existing bootstrap admin has drifted."""

    # The helper is kept separate so the fail-closed aggregate contract can be
    # unit tested without ever rendering a raw key or digest into diagnostics.
    expected_consumer_type = spec.consumer_type.value
    expected_role = spec.role.value
    expected_scopes = {(scope.project_id, scope.app_id) for scope in spec.scopes}
    actual_scopes = {(scope.project_id, scope.app_id) for scope in existing.scopes}
    drift: list[str] = []
    if existing.consumer_type != expected_consumer_type:
        drift.append("consumer_type")
    if existing.role != expected_role:
        drift.append("role")
    if actual_scopes != expected_scopes:
        drift.append("scopes")
    if not existing.enabled:
        drift.append("enabled")
    if existing.revoked_at is not None:
        drift.append("revoked_at")
    if existing.deleted_at is not None:
        drift.append("deleted_at")
    if existing.expires_at is not None:
        drift.append("expires_at")

    now = datetime.now(UTC)

    def is_active(key: ConsumerKey) -> bool:
        expires_at = key.expires_at
        if expires_at is not None and expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=UTC)
        return key.revoked_at is None and (expires_at is None or expires_at > now)

    all_active_keys = [key for key in existing.keys if is_active(key)]
    matching_active_keys = [
        key
        for key in all_active_keys
        if any(
            _secrets.compare_digest(key.key_hash, candidate_hash)
            for candidate_hash in candidate_key_hashes
        )
    ]
    if len(all_active_keys) != 1:
        drift.append("active_keys")
    if len(matching_active_keys) != 1:
        drift.append("bootstrap_key")
    else:
        bootstrap_key = matching_active_keys[0]
        if bootstrap_key.expires_at is not None or bootstrap_key.expiry_source != "independent":
            drift.append("bootstrap_key_lifecycle")
        if not _secrets.compare_digest(existing.key_hash, bootstrap_key.key_hash):
            drift.append("key_pointer")
    return drift


def generate_admin_key() -> str:
    """A URL-safe key for operators who want the bootstrap to mint one for them."""
    return _secrets.token_urlsafe(32)

"""Resolve catalog environments into execution targets without leaking ownership."""

from dataclasses import dataclass
from typing import Protocol

from apex.persistence.models import Environment
from apex.services.connection_credentials import environment_target_requires_repair
from apex.services.connections import (
    TRUSTED_PRIVATE_HOST_OPTION,
    validate_adapter_base_url,
)


class EnvironmentRepository(Protocol):
    async def get_environment(self, environment_id: str) -> Environment | None: ...


class EnvironmentTargetNotFoundError(LookupError):
    """The environment is missing or outside the selected scope."""


class EnvironmentTargetNotApprovedError(EnvironmentTargetNotFoundError):
    """An accessible environment has no currently approved executable target."""


@dataclass(frozen=True)
class EnvironmentTarget:
    environment_id: str
    project_id: str
    app_id: str
    base_url: str
    version: int


async def resolve_environment_context(
    repository: EnvironmentRepository,
    environment_id: str,
    *,
    project_id: str | None,
    app_id: str | None,
) -> Environment:
    """Resolve catalog context without requiring permission to send traffic to it."""

    env = await repository.get_environment(environment_id)
    application = env.application if env is not None else None
    if (
        env is None
        or application is None
        or getattr(application, "archived_at", None) is not None
        or project_id is None
        or project_id not in application.project_ids
        or (app_id is not None and env.application_id != app_id)
    ):
        raise EnvironmentTargetNotFoundError("environment not found")

    return env


async def resolve_environment_target(
    repository: EnvironmentRepository,
    environment_id: str,
    *,
    project_id: str | None,
    app_id: str | None,
) -> EnvironmentTarget:
    """Return an approved target only when catalog ownership exactly matches the run."""
    env = await resolve_environment_context(
        repository, environment_id, project_id=project_id, app_id=app_id
    )
    assert project_id is not None  # resolve_environment_context rejects a missing project
    # Environment rows predate the secret-free connection contract and can be
    # written outside the API. Treat the URL/options pair atomically: no approval
    # bit may make legacy credentials or malformed metadata executable.
    if environment_target_requires_repair(env.base_url, env.options):
        raise EnvironmentTargetNotApprovedError("environment has no approved HTTP target")

    base_url = str(env.base_url or "").strip()
    if not env.target_approved or not base_url or env.target_version < 1:
        raise EnvironmentTargetNotApprovedError("environment has no approved HTTP target")
    invalid_target = False
    try:
        validate_adapter_base_url(
            base_url,
            allow_private_hosts=(env.options or {}).get(TRUSTED_PRIVATE_HOST_OPTION) is True
            or None,
        )
    except ValueError:
        invalid_target = True
    if invalid_target:
        raise EnvironmentTargetNotApprovedError("environment has no approved HTTP target")
    return EnvironmentTarget(
        environment_id=env.id,
        # The run's project, which resolve_environment_context checked is linked.
        project_id=project_id,
        app_id=env.application_id,
        base_url=base_url,
        version=int(env.target_version),
    )

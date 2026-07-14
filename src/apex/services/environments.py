"""Resolve catalog environments into execution targets without leaking ownership."""

from dataclasses import dataclass
from typing import Protocol

from apex.persistence.models import Environment
from apex.services.connections import (
    TRUSTED_PRIVATE_HOST_OPTION,
    validate_adapter_base_url,
)


class EnvironmentRepository(Protocol):
    async def get_environment(self, environment_id: str) -> Environment | None: ...


class EnvironmentTargetNotFoundError(LookupError):
    """The environment is missing, out of scope, or has no executable target."""


@dataclass(frozen=True)
class EnvironmentTarget:
    environment_id: str
    project_id: str
    app_id: str
    base_url: str
    version: int


async def resolve_environment_target(
    repository: EnvironmentRepository,
    environment_id: str,
    *,
    project_id: str | None,
    app_id: str | None,
) -> EnvironmentTarget:
    """Return an approved target only when catalog ownership exactly matches the run."""

    env = await repository.get_environment(environment_id)
    application = env.application if env is not None else None
    if (
        env is None
        or application is None
        or getattr(application, "archived_at", None) is not None
        or project_id is None
        or application.project_id != project_id
        or (app_id is not None and env.application_id != app_id)
    ):
        raise EnvironmentTargetNotFoundError(f"environment {environment_id!r} not found")

    base_url = str(env.base_url or "").strip()
    if not env.target_approved or not base_url:
        raise EnvironmentTargetNotFoundError(
            f"environment {environment_id!r} has no approved HTTP target"
        )
    try:
        validate_adapter_base_url(
            base_url,
            allow_private_hosts=(env.options or {}).get(TRUSTED_PRIVATE_HOST_OPTION) is True
            or None,
        )
    except ValueError as exc:
        raise EnvironmentTargetNotFoundError(
            f"environment {environment_id!r} has no approved HTTP target"
        ) from exc
    return EnvironmentTarget(
        environment_id=env.id,
        project_id=application.project_id,
        app_id=env.application_id,
        base_url=base_url,
        version=int(env.target_version),
    )

"""Approved environment-target resolution and ownership checks."""

from types import SimpleNamespace
from typing import Any

import pytest

from apex.services.environments import (
    EnvironmentTargetNotFoundError,
    resolve_environment_target,
)


class FakeEnvironmentRepository:
    def __init__(self, environment: Any) -> None:
        self.environment = environment

    async def get_environment(self, environment_id: str) -> Any:
        if self.environment is not None and self.environment.id == environment_id:
            return self.environment
        return None


def environment(
    *,
    approved: bool = True,
    base_url: str | None = "https://8.8.8.8/load",
    version: int = 4,
    options: Any = None,
) -> Any:
    return SimpleNamespace(
        id="env-1",
        application_id="app-a",
        application=SimpleNamespace(id="app-a", project_id="proj-a"),
        base_url=base_url,
        options={} if options is None else options,
        target_approved=approved,
        target_version=version,
    )


async def test_resolves_only_approved_exact_scope_target() -> None:
    target = await resolve_environment_target(
        FakeEnvironmentRepository(environment()),
        "env-1",
        project_id="proj-a",
        app_id="app-a",
    )

    assert target.base_url == "https://8.8.8.8/load"
    assert target.version == 4


@pytest.mark.parametrize(
    ("approved", "base_url"),
    [(False, "https://8.8.8.8/load"), (True, None)],
)
async def test_rejects_unapproved_or_empty_target(approved: bool, base_url: str | None) -> None:
    with pytest.raises(EnvironmentTargetNotFoundError, match="no approved HTTP target"):
        await resolve_environment_target(
            FakeEnvironmentRepository(environment(approved=approved, base_url=base_url)),
            "env-1",
            project_id="proj-a",
            app_id="app-a",
        )


async def test_revalidates_target_against_private_address_policy() -> None:
    with pytest.raises(EnvironmentTargetNotFoundError, match="no approved HTTP target") as raised:
        await resolve_environment_target(
            FakeEnvironmentRepository(environment(base_url="http://169.254.169.254/latest")),
            "env-1",
            project_id="proj-a",
            app_id="app-a",
        )

    assert raised.value.__cause__ is None
    assert raised.value.__context__ is None


@pytest.mark.parametrize(
    ("base_url", "options"),
    [
        ("https://alice:secret@8.8.8.8/load", {}),
        ("https://8.8.8.8/load", {"nested": {"authorization": "Bearer secret"}}),
        ("https://8.8.8.8/load", ["malformed", "options"]),
    ],
)
async def test_legacy_credential_or_malformed_target_metadata_fails_closed(
    base_url: str,
    options: Any,
) -> None:
    with pytest.raises(EnvironmentTargetNotFoundError, match="no approved HTTP target") as raised:
        await resolve_environment_target(
            FakeEnvironmentRepository(environment(base_url=base_url, options=options)),
            "env-1",
            project_id="proj-a",
            app_id="app-a",
        )

    assert "secret" not in str(raised.value)


async def test_platform_approved_private_target_is_resolved() -> None:
    target = await resolve_environment_target(
        FakeEnvironmentRepository(
            environment(
                base_url="http://10.0.0.8/load",
                options={"_apex_trusted_private_host": True},
            )
        ),
        "env-1",
        project_id="proj-a",
        app_id="app-a",
    )

    assert target.base_url == "http://10.0.0.8/load"


async def test_cross_app_environment_is_hidden() -> None:
    with pytest.raises(EnvironmentTargetNotFoundError, match="not found"):
        await resolve_environment_target(
            FakeEnvironmentRepository(environment()),
            "env-1",
            project_id="proj-a",
            app_id="app-b",
        )


async def test_missing_environment_does_not_reflect_lookup_identifier() -> None:
    canary = "environment-lookup-secret-canary"

    with pytest.raises(EnvironmentTargetNotFoundError) as raised:
        await resolve_environment_target(
            FakeEnvironmentRepository(None),
            canary,
            project_id="proj-a",
            app_id="app-a",
        )

    assert canary not in str(raised.value)

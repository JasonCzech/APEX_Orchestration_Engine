from __future__ import annotations

from collections.abc import Iterator

import pytest

from apex.persistence import migrate


@pytest.fixture(autouse=True)
def _clear_database_role_claim_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in migrate._DATABASE_ROLE_CLAIM_ENV_NAMES:
        monkeypatch.delenv(name, raising=False)


def _compatibility_results(*results: bool) -> Iterator[bool]:
    yield from results


def test_migration_runner_skips_a_proven_compatible_descendant(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    upgraded = False

    def upgrade() -> None:
        nonlocal upgraded
        upgraded = True

    monkeypatch.setattr(migrate, "_schema_is_compatible", lambda: True)
    monkeypatch.setattr(migrate, "_upgrade_to_packaged_head", upgrade)

    assert migrate.main() == 0
    assert upgraded is False


def test_migration_runner_upgrades_and_revalidates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    results = _compatibility_results(False, True)
    upgrades = 0

    def upgrade() -> None:
        nonlocal upgrades
        upgrades += 1

    monkeypatch.setattr(migrate, "_schema_is_compatible", lambda: next(results))
    monkeypatch.setattr(migrate, "_upgrade_to_packaged_head", upgrade)

    assert migrate.main() == 0
    assert upgrades == 1


def test_migration_runner_rejects_an_unproven_post_upgrade_schema(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    results = _compatibility_results(False, False)
    monkeypatch.setattr(migrate, "_schema_is_compatible", lambda: next(results))
    monkeypatch.setattr(migrate, "_upgrade_to_packaged_head", lambda: None)

    assert migrate.main() == 1


def test_migration_runner_sanitizes_upgrade_failures(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    secret = "postgresql://admin:do-not-log@example.invalid/apex"
    monkeypatch.setattr(migrate, "_schema_is_compatible", lambda: False)

    def fail() -> None:
        raise RuntimeError(secret)

    monkeypatch.setattr(migrate, "_upgrade_to_packaged_head", fail)

    assert migrate.main() == 1
    assert secret not in capsys.readouterr().err


def test_migration_runner_sanitizes_compatibility_check_failures(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    secret = "postgresql://admin:do-not-log@example.invalid/apex"

    def fail() -> bool:
        raise RuntimeError(secret)

    monkeypatch.setattr(migrate, "_schema_is_compatible", fail)

    assert migrate.main() == 1
    assert secret not in capsys.readouterr().err


def test_migration_runner_verifies_claims_before_schema_ddl(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    secret = "postgresql://admin:claim-canary@example.invalid/apex"
    schema_checked = False

    async def fail_claim_verification() -> tuple[bool, bool]:
        try:
            raise RuntimeError(secret)
        except RuntimeError as exc:
            raise migrate._OwnershipVerificationFailed from exc

    def schema_check() -> bool:
        nonlocal schema_checked
        schema_checked = True
        return True

    monkeypatch.setenv("APEX_DATABASE_ROLE_CLAIM_KEY", "x" * 64)
    monkeypatch.setattr(
        migrate,
        "_run_claimed_migration",
        fail_claim_verification,
    )
    monkeypatch.setattr(migrate, "_schema_is_compatible", schema_check)

    assert migrate.main() == 1
    assert schema_checked is False
    assert secret not in capsys.readouterr().err


@pytest.mark.parametrize(
    "claim_environment",
    [
        {"APEX_DATABASE_ROLE_CLAIM_KEY": ""},
        {"APEX_RUNTIME_OWNER_ROLE": "apex_runtime"},
    ],
)
def test_partial_database_role_claim_environment_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    claim_environment: dict[str, str],
) -> None:
    unclaimed_path_called = False

    def unexpected_unclaimed_path() -> bool:
        nonlocal unclaimed_path_called
        unclaimed_path_called = True
        return True

    for name, value in claim_environment.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setattr(migrate, "_schema_is_compatible", unexpected_unclaimed_path)

    assert migrate.main() == 1
    assert unclaimed_path_called is False
    assert capsys.readouterr().err == "APEX database ownership verification failed.\n"

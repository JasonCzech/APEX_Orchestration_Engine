from __future__ import annotations

from collections.abc import Iterator

import pytest

from apex.persistence import migrate


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

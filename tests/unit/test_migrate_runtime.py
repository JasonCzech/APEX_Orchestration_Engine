"""Direct tests for the claimed migration connection and compatibility probe."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import Any

import pytest

from apex.persistence import migrate
from apex.persistence.schema_readiness import SchemaNotReadyError


@pytest.fixture(autouse=True)
def _clear_database_role_claim_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in migrate._DATABASE_ROLE_CLAIM_ENV_NAMES:
        monkeypatch.delenv(name, raising=False)


class FakeDriverConnection:
    def __init__(self, events: list[str], *, lock_error: Exception | None = None) -> None:
        self.events = events
        self.lock_error = lock_error

    async def execute(self, *_args: object) -> None:
        self.events.append("lock")
        if self.lock_error is not None:
            raise self.lock_error


class FakeClaimConnection:
    def __init__(
        self,
        events: list[str],
        *,
        lock_error: Exception | None = None,
        upgrade_error: Exception | None = None,
    ) -> None:
        self.events = events
        self.driver = FakeDriverConnection(events, lock_error=lock_error)
        self.upgrade_error = upgrade_error

    async def get_raw_connection(self) -> Any:
        return SimpleNamespace(driver_connection=self.driver)

    async def run_sync(self, callback: Any) -> None:
        self.events.append("upgrade")
        if self.upgrade_error is not None:
            raise self.upgrade_error
        assert callback is migrate._upgrade_to_packaged_head

    async def commit(self) -> None:
        self.events.append("commit")


class FakeEngine:
    def __init__(self, connection: FakeClaimConnection | None = None) -> None:
        self.connection = connection
        self.disposed = False

    @asynccontextmanager
    async def connect(self) -> AsyncIterator[FakeClaimConnection]:
        assert self.connection is not None
        yield self.connection

    async def dispose(self) -> None:
        self.disposed = True


def _context() -> Any:
    return SimpleNamespace(migration_uri="postgresql+asyncpg://migration:hidden@database/apex")


def _settings() -> Any:
    return SimpleNamespace(
        database=SimpleNamespace(
            uri="postgresql+asyncpg://runtime:hidden@database/apex",
            ssl_mode="verify-full",
        )
    )


@pytest.mark.parametrize("compatible", [True, False])
async def test_one_shot_schema_probe_always_disposes_engine(
    monkeypatch: pytest.MonkeyPatch,
    compatible: bool,
) -> None:
    engine = FakeEngine()
    monkeypatch.setattr(migrate, "get_settings", _settings)
    monkeypatch.setattr(migrate, "database_asyncpg_uri", lambda value: value)
    monkeypatch.setattr(migrate, "database_ssl_connect_args", lambda *_: {"ssl": "context"})
    monkeypatch.setattr(migrate, "create_async_engine", lambda *_args, **_kwargs: engine)

    async def validate(_engine: object) -> None:
        if not compatible:
            raise SchemaNotReadyError("opaque")

    monkeypatch.setattr(migrate, "validate_schema_head", validate)

    assert await migrate._schema_is_compatible_async() is compatible
    assert engine.disposed is True


async def test_one_shot_schema_probe_rejects_unauthenticated_remote_transport(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        migrate,
        "get_settings",
        lambda: SimpleNamespace(
            database=SimpleNamespace(
                uri=("postgresql+asyncpg://migration:hidden@database/apex?sslmode=require"),
                ssl_mode=None,
            )
        ),
    )
    monkeypatch.setattr(
        migrate,
        "create_async_engine",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("unsafe transport must be rejected before engine creation")
        ),
    )

    with pytest.raises(ValueError, match="authenticate every remote server"):
        await migrate._schema_is_compatible_async()


async def test_one_shot_schema_probe_disposal_survives_repeated_cancellation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class BlockingDisposeEngine(FakeEngine):
        def __init__(self) -> None:
            super().__init__()
            self.dispose_entered = asyncio.Event()
            self.allow_dispose = asyncio.Event()

        async def dispose(self) -> None:
            self.dispose_entered.set()
            await self.allow_dispose.wait()
            self.disposed = True

    engine = BlockingDisposeEngine()
    monkeypatch.setattr(migrate, "get_settings", _settings)
    monkeypatch.setattr(migrate, "create_async_engine", lambda *_args, **_kwargs: engine)

    async def validate(_engine: object) -> None:
        return None

    monkeypatch.setattr(migrate, "validate_schema_head", validate)
    task = asyncio.create_task(migrate._schema_is_compatible_async())
    await engine.dispose_entered.wait()

    task.cancel()
    await asyncio.sleep(0)
    task.cancel()
    await asyncio.sleep(0)
    assert task.done() is False
    engine.allow_dispose.set()

    with pytest.raises(asyncio.CancelledError):
        await task
    assert engine.disposed is True


def test_sync_schema_probe_runs_the_async_check(monkeypatch: pytest.MonkeyPatch) -> None:
    async def compatible() -> bool:
        return True

    monkeypatch.setattr(migrate, "_schema_is_compatible_async", compatible)

    assert migrate._schema_is_compatible() is True


def test_packaged_upgrade_reuses_supplied_connection(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[Any, str]] = []
    supplied = object()

    monkeypatch.setattr(
        migrate.command,
        "upgrade",
        lambda config, target: calls.append((config, target)),
    )

    migrate._upgrade_to_packaged_head(supplied)  # type: ignore[arg-type]

    config, target = calls[0]
    assert target == "head"
    assert config.attributes["connection"] is supplied
    assert config.get_main_option("script_location").endswith("apex/persistence/migrations")


@pytest.mark.parametrize("initially_compatible", [True, False])
async def test_claimed_migration_holds_lock_through_upgrade_and_revalidation(
    monkeypatch: pytest.MonkeyPatch,
    initially_compatible: bool,
) -> None:
    events: list[str] = []
    connection = FakeClaimConnection(events)
    engine = FakeEngine(connection)
    compatibility = iter([initially_compatible, True])

    monkeypatch.setattr(migrate, "claim_context_from_environment", _context)
    monkeypatch.setattr(migrate, "get_settings", _settings)
    monkeypatch.setattr(migrate, "database_asyncpg_uri", lambda value: value)
    monkeypatch.setattr(migrate, "database_ssl_connect_args", lambda *_: {})
    monkeypatch.setattr(migrate, "create_async_engine", lambda *_args, **_kwargs: engine)

    async def verify(_driver: object, _context_value: object) -> None:
        events.append("verify")

    checked_uris: list[str | None] = []

    async def schema_check(database_uri: str | None = None) -> bool:
        checked_uris.append(database_uri)
        events.append("check")
        return next(compatibility)

    monkeypatch.setattr(migrate, "verify_database_role_claims", verify)
    monkeypatch.setattr(migrate, "_schema_is_compatible_async", schema_check)

    result = await migrate._run_claimed_migration()

    if initially_compatible:
        assert result == (True, True)
        assert events == ["lock", "verify", "check"]
    else:
        assert result == (False, True)
        assert events == ["lock", "verify", "check", "upgrade", "commit", "check"]
    assert checked_uris == [
        _context().migration_uri,
        *([] if initially_compatible else [_context().migration_uri]),
    ]
    assert engine.disposed is True


async def test_claimed_migration_probes_the_verified_migration_uri(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    claimed_engine = FakeEngine(FakeClaimConnection(events))
    probe_engine = FakeEngine()
    engines = iter((claimed_engine, probe_engine))
    created_uris: list[str] = []
    claimed_uri = _context().migration_uri

    monkeypatch.setattr(migrate, "claim_context_from_environment", _context)
    monkeypatch.setattr(migrate, "get_settings", _settings)
    monkeypatch.setattr(migrate, "database_asyncpg_uri", lambda value: value)
    monkeypatch.setattr(migrate, "database_ssl_connect_args", lambda *_args: {})

    def create_engine(uri: str, **_kwargs: object) -> FakeEngine:
        created_uris.append(uri)
        return next(engines)

    monkeypatch.setattr(migrate, "create_async_engine", create_engine)

    async def verify(_driver: object, _context_value: object) -> None:
        events.append("verify")

    async def validate(engine: object) -> None:
        assert engine is probe_engine
        events.append("check")

    monkeypatch.setattr(migrate, "verify_database_role_claims", verify)
    monkeypatch.setattr(migrate, "validate_schema_head", validate)

    assert await migrate._run_claimed_migration() == (True, True)
    assert created_uris == [claimed_uri, claimed_uri]
    assert created_uris[0] != _settings().database.uri
    assert events == ["lock", "verify", "check"]
    assert claimed_engine.disposed is True
    assert probe_engine.disposed is True


@pytest.mark.parametrize(
    ("failure", "expected"),
    [
        ("lock", migrate._OwnershipVerificationFailed),
        ("verify", migrate._OwnershipVerificationFailed),
        ("initial-check", migrate._ClaimedCompatibilityCheckFailed),
        ("upgrade", migrate._ClaimedUpgradeFailed),
        ("final-check", migrate._ClaimedCompatibilityCheckFailed),
    ],
)
async def test_claimed_migration_classifies_failures_without_leaking_driver_errors(
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
    expected: type[Exception],
) -> None:
    secret_error = RuntimeError("postgresql://admin:do-not-log@database/apex")
    connection = FakeClaimConnection(
        [],
        lock_error=secret_error if failure == "lock" else None,
        upgrade_error=secret_error if failure == "upgrade" else None,
    )
    engine = FakeEngine(connection)
    checks = 0

    monkeypatch.setattr(migrate, "claim_context_from_environment", _context)
    monkeypatch.setattr(migrate, "get_settings", _settings)
    monkeypatch.setattr(migrate, "database_asyncpg_uri", lambda value: value)
    monkeypatch.setattr(migrate, "database_ssl_connect_args", lambda *_: {})
    monkeypatch.setattr(migrate, "create_async_engine", lambda *_args, **_kwargs: engine)

    async def verify(_driver: object, _context_value: object) -> None:
        if failure == "verify":
            raise secret_error

    async def schema_check(_database_uri: str | None = None) -> bool:
        nonlocal checks
        checks += 1
        if failure == "initial-check" and checks == 1:
            raise secret_error
        if failure == "final-check" and checks == 2:
            raise secret_error
        return False if checks == 1 else True

    monkeypatch.setattr(migrate, "verify_database_role_claims", verify)
    monkeypatch.setattr(migrate, "_schema_is_compatible_async", schema_check)

    with pytest.raises(expected) as caught:
        await migrate._run_claimed_migration()

    assert str(caught.value) == ""
    assert caught.value.__context__ is None
    assert engine.disposed is True


@pytest.mark.parametrize(
    ("result", "expected_status"),
    [((True, True), 0), ((False, True), 0), ((False, False), 1)],
)
def test_claimed_migration_main_handles_compatible_and_migrated_results(
    monkeypatch: pytest.MonkeyPatch,
    result: tuple[bool, bool],
    expected_status: int,
) -> None:
    async def run() -> tuple[bool, bool]:
        return result

    monkeypatch.setenv("APEX_DATABASE_ROLE_CLAIM_KEY", "x" * 64)
    monkeypatch.setattr(migrate, "_run_claimed_migration", run)

    assert migrate.main() == expected_status


@pytest.mark.parametrize(
    "failure",
    [migrate._ClaimedCompatibilityCheckFailed, migrate._ClaimedUpgradeFailed],
)
def test_claimed_migration_main_sanitizes_classified_failures(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    failure: type[Exception],
) -> None:
    secret = "postgresql://admin:do-not-log@database/apex"

    async def run() -> tuple[bool, bool]:
        try:
            raise RuntimeError(secret)
        except RuntimeError as exc:
            raise failure from exc

    monkeypatch.setenv("APEX_DATABASE_ROLE_CLAIM_KEY", "x" * 64)
    monkeypatch.setattr(migrate, "_run_claimed_migration", run)

    assert migrate.main() == 1
    assert secret not in capsys.readouterr().err

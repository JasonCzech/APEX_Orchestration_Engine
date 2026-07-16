"""Hermetic operation-level coverage for the transactional bootstrap runner."""

from __future__ import annotations

from collections.abc import Iterable
from types import SimpleNamespace
from typing import cast

import pytest
from sqlalchemy.dialects import postgresql
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql import ClauseElement

from apex.bootstrap import runner
from apex.bootstrap.runner import BootstrapError, BootstrapReport
from apex.bootstrap.schema import BootstrapDocument
from apex.persistence.models import (
    ApiConsumer,
    Application,
    Connection,
    ConsumerKey,
    Environment,
)


class FakeSession:
    def __init__(self, scalar_results: Iterable[object | None] = ()) -> None:
        self._scalar_results = iter(scalar_results)
        self.scalar_statements: list[ClauseElement] = []
        self.added: list[object] = []
        self.flushes = 0

    async def scalar(self, _statement: ClauseElement) -> object | None:
        self.scalar_statements.append(_statement)
        return next(self._scalar_results)

    def add(self, row: object) -> None:
        self.added.append(row)

    async def flush(self) -> None:
        self.flushes += 1


def _as_async_session(session: FakeSession) -> AsyncSession:
    """Expose the deliberately narrow fake only at the tested SQLAlchemy seam."""

    return cast(AsyncSession, session)


def _document(value: dict[str, object]) -> BootstrapDocument:
    return BootstrapDocument.model_validate(value)


async def test_seed_default_prompts_creates_only_missing_catalog_entries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from apex.persistence.repositories import prompts as prompts_repository
    from apex.services import prompts as prompts_service

    created: list[str] = []

    class Repo:
        def __init__(self, _session: object, *, commit_on_write: bool) -> None:
            assert commit_on_write is False

        async def get_by_key(self, _namespace: str, key: str) -> object | None:
            return object() if key == "existing/system" else None

    class Catalog:
        def __init__(self, _repo: object) -> None:
            pass

        async def create_prompt(self, **values: object) -> None:
            created.append(str(values["key"]))

    monkeypatch.setattr(prompts_repository, "PromptRepository", Repo)
    monkeypatch.setattr(prompts_service, "PromptCatalogService", Catalog)
    monkeypatch.setattr(
        prompts_service,
        "DEFAULT_PHASE_PROMPTS",
        {"existing/system": "old", "new/user": "new"},
    )
    monkeypatch.setattr(prompts_service, "PHASE_NAMESPACE", "phase")
    report = BootstrapReport()
    messages: list[str] = []

    await runner._seed_default_prompts(_as_async_session(FakeSession()), report, messages.append)

    assert created == ["new/user"]
    assert report.prompts_created == ["phase/new/user"]
    assert messages == [
        "prompt phase/existing/system: exists; unchanged",
        "prompt phase/new/user: created",
    ]


async def test_apply_document_locks_before_natural_key_lookup() -> None:
    doc = _document({"applications": [{"project_id": "p", "name": "app"}]})

    class PostgresSession(FakeSession):
        bind = SimpleNamespace(dialect=SimpleNamespace(name="postgresql"))

        def __init__(self) -> None:
            super().__init__([None])
            self.events: list[str] = []

        async def execute(self, _statement: object, _parameters: object) -> None:
            self.events.append("lock")

        async def scalar(self, statement: ClauseElement) -> object | None:
            self.events.append("lookup")
            return await super().scalar(statement)

    session = PostgresSession()
    report = await runner.apply_document(doc, _as_async_session(session), env={})

    assert session.events == ["lock", "lookup"]
    assert report.applications_created == ["p/app"]


async def test_applications_create_and_accept_exact_existing_rows() -> None:
    doc = _document({"applications": [{"project_id": "p", "name": "app", "description": "desc"}]})
    created_session = FakeSession([None])
    report = BootstrapReport()

    await runner._apply_applications(
        doc,
        _as_async_session(created_session),
        report,
        lambda _message: None,
    )

    assert isinstance(created_session.added[0], Application)
    assert report.applications_created == ["p/app"]
    existing = Application(id="a1", project_id="p", name="app", description="desc")
    existing_session = FakeSession([existing])
    await runner._apply_applications(
        doc,
        _as_async_session(existing_session),
        BootstrapReport(),
        lambda _message: None,
    )
    assert existing_session.added == []


async def test_application_drift_is_rejected_by_safe_field_name() -> None:
    doc = _document({"applications": [{"project_id": "p", "name": "app"}]})
    existing = Application(id="a1", project_id="p", name="app", description="unexpected")

    with pytest.raises(BootstrapError, match="description"):
        await runner._apply_applications(
            doc,
            _as_async_session(FakeSession([existing])),
            BootstrapReport(),
            lambda _message: None,
        )


async def test_environment_create_persists_hosts_and_target_generation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(runner, "validate_adapter_base_url", lambda *_args, **_kwargs: None)
    doc = _document(
        {
            "environments": [
                {
                    "project_id": "p",
                    "application": "app",
                    "name": "prod",
                    "base_url": "https://api.example.com",
                    "hosts": [{"hostname": "api.internal", "role": "app"}],
                }
            ]
        }
    )
    app = Application(id="a1", project_id="p", name="app")
    session = FakeSession([app, None])
    report = BootstrapReport()

    await runner._apply_environments(
        doc,
        _as_async_session(session),
        report,
        lambda _message: None,
    )

    environment = session.added[0]
    assert isinstance(environment, Environment)
    assert environment.target_approved is True
    assert environment.target_version == 1
    assert [(host.hostname, host.role) for host in environment.hosts] == [("api.internal", "app")]
    assert report.environments_created == ["app/prod"]


async def test_environment_existing_target_is_reapproved_only_after_exact_match(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(runner, "validate_adapter_base_url", lambda *_args, **_kwargs: None)
    doc = _document(
        {
            "environments": [
                {
                    "project_id": "p",
                    "application": "app",
                    "name": "prod",
                    "base_url": "https://api.example.com",
                }
            ]
        }
    )
    app = Application(id="a1", project_id="p", name="app")
    existing = Environment(
        id="e1",
        application_id="a1",
        name="prod",
        base_url="https://api.example.com",
        options={},
        target_approved=False,
        target_version=4,
        hosts=[],
    )
    session = FakeSession([app, existing])

    await runner._apply_environments(
        doc,
        _as_async_session(session),
        BootstrapReport(),
        lambda _message: None,
    )

    assert existing.target_approved is True
    assert existing.target_version == 5
    assert session.flushes == 1


async def test_environment_rejects_missing_application_and_invalid_target(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    doc = _document(
        {
            "environments": [
                {
                    "project_id": "p",
                    "application": "app",
                    "name": "prod",
                    "base_url": "https://api.example.com",
                }
            ]
        }
    )
    with pytest.raises(BootstrapError, match="does not exist"):
        await runner._apply_environments(
            doc,
            _as_async_session(FakeSession([None])),
            BootstrapReport(),
            lambda _message: None,
        )

    app = Application(id="a1", project_id="p", name="app")
    monkeypatch.setattr(
        runner,
        "validate_adapter_base_url",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(ValueError("unsafe target")),
    )
    with pytest.raises(BootstrapError, match="invalid target") as raised:
        await runner._apply_environments(
            doc,
            _as_async_session(FakeSession([app])),
            BootstrapReport(),
            lambda _message: None,
        )
    assert raised.value.__cause__ is None
    assert raised.value.__context__ is None


async def test_connections_create_match_and_reconcile_known_minio_alias(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(runner, "validate_adapter_base_url", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(runner, "validate_connection_config", lambda _config: None)
    doc = _document(
        {
            "connections": [
                {
                    "name": "minio-artifacts",
                    "kind": "artifact_store",
                    "provider": "s3",
                    "options": {"endpoint": "apex-minio:9000", "bucket": "artifacts"},
                    "secret_ref": "env:APEX_INTEGRATION_MINIO_SECRET_KEY",
                }
            ]
        }
    )
    create_session = FakeSession([None])
    report = BootstrapReport()
    await runner._apply_connections(
        doc,
        _as_async_session(create_session),
        report,
        lambda _message: None,
    )
    assert isinstance(create_session.added[0], Connection)
    assert report.connections_created == ["minio-artifacts"]

    existing = Connection(
        id="c1",
        name="minio-artifacts",
        kind="artifact_store",
        provider="s3",
        project_id=None,
        base_url=None,
        options={
            "endpoint": "apex-minio.apex.svc.cluster.local:9000",
            "bucket": "artifacts",
        },
        secret_ref="env:APEX_INTEGRATION_MINIO_SECRET_KEY",
        enabled=True,
    )
    reconcile_session = FakeSession([existing])
    await runner._apply_connections(
        doc,
        _as_async_session(reconcile_session),
        BootstrapReport(),
        lambda _message: None,
    )
    assert existing.options["endpoint"] == "apex-minio:9000"
    assert reconcile_session.flushes == 1

    matching_session = FakeSession([existing])
    await runner._apply_connections(
        doc,
        _as_async_session(matching_session),
        BootstrapReport(),
        lambda _message: None,
    )
    assert matching_session.flushes == 0


async def test_connection_validation_and_configuration_drift_fail_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    doc = _document({"connections": [{"name": "events", "kind": "secrets", "provider": "env"}]})
    monkeypatch.setattr(runner, "validate_adapter_base_url", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        runner,
        "validate_connection_config",
        lambda _config: (_ for _ in ()).throw(ValueError("invalid provider")),
    )
    with pytest.raises(BootstrapError, match="invalid configuration") as raised:
        await runner._apply_connections(
            doc,
            _as_async_session(FakeSession()),
            BootstrapReport(),
            lambda _message: None,
        )
    assert raised.value.__cause__ is None
    assert raised.value.__context__ is None

    monkeypatch.setattr(runner, "validate_connection_config", lambda _config: None)
    existing = Connection(
        id="c1",
        name="events",
        kind="secrets",
        provider="unexpected",
        options={},
        enabled=True,
    )
    with pytest.raises(BootstrapError, match="provider"):
        await runner._apply_connections(
            doc,
            _as_async_session(FakeSession([existing])),
            BootstrapReport(),
            lambda _message: None,
        )


async def test_bootstrap_rejects_scoped_jira_without_external_project(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(runner, "validate_adapter_base_url", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(runner, "validate_connection_config", lambda _config: None)
    doc = _document(
        {
            "connections": [
                {
                    "name": "scoped-jira",
                    "kind": "work_tracking",
                    "provider": "jira",
                    "project_id": "project-1",
                    "options": {},
                }
            ]
        }
    )

    with pytest.raises(BootstrapError, match="invalid configuration"):
        await runner._apply_connections(
            doc,
            _as_async_session(FakeSession()),
            BootstrapReport(),
            lambda _message: None,
        )


async def test_admin_create_and_existing_key_proof(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(runner, "hash_api_key", lambda _key: "a" * 64)
    monkeypatch.setattr(runner, "_candidate_key_hashes", lambda _key: ("a" * 64,))
    doc = _document(
        {
            "admin": {
                "name": "bootstrap-admin",
                "scopes": [{"project_id": "p", "app_id": "app"}],
            }
        }
    )
    create_session = FakeSession([None, None])
    report = BootstrapReport()
    await runner._apply_admin(
        doc,
        _as_async_session(create_session),
        {"APEX_BOOTSTRAP_ADMIN_KEY": "plaintext"},
        report,
        lambda _message: None,
    )
    consumer = create_session.added[0]
    assert isinstance(consumer, ApiConsumer)
    assert consumer.key_hash == "a" * 64
    assert report.admin_created == "bootstrap-admin"

    existing = ApiConsumer(
        id="u1",
        name="bootstrap-admin",
        key_hash="a" * 64,
        consumer_type="internal",
        role="admin",
        enabled=True,
        scopes=consumer.scopes,
        keys=[ConsumerKey(key_hash="a" * 64, expiry_source="independent")],
    )
    existing_report = BootstrapReport()
    await runner._apply_admin(
        doc,
        _as_async_session(FakeSession([existing, None])),
        {"APEX_BOOTSTRAP_ADMIN_KEY": "plaintext"},
        existing_report,
        lambda _message: None,
    )
    assert existing_report.admin_existing == "bootstrap-admin"


async def test_admin_requires_mounted_key_and_rejects_existing_drift(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    doc = _document({"admin": {"name": "bootstrap-admin"}})
    with pytest.raises(BootstrapError, match="unset/empty"):
        await runner._apply_admin(
            doc,
            _as_async_session(FakeSession()),
            {},
            BootstrapReport(),
            lambda _message: None,
        )

    monkeypatch.setattr(runner, "hash_api_key", lambda _key: "a" * 64)
    monkeypatch.setattr(runner, "_candidate_key_hashes", lambda _key: ("a" * 64,))
    existing = ApiConsumer(
        id="u1",
        name="bootstrap-admin",
        key_hash="b" * 64,
        consumer_type="internal",
        role="viewer",
        enabled=True,
        scopes=[],
        keys=[ConsumerKey(key_hash="b" * 64, expiry_source="independent")],
    )
    with pytest.raises(BootstrapError, match="role"):
        await runner._apply_admin(
            doc,
            _as_async_session(FakeSession([existing, None])),
            {"APEX_BOOTSTRAP_ADMIN_KEY": "plaintext"},
            BootstrapReport(),
            lambda _message: None,
        )


async def test_admin_rejects_key_claimed_by_another_hash_generation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(runner, "hash_api_key", lambda _key: "a" * 64)
    monkeypatch.setattr(
        runner,
        "_candidate_key_hashes",
        lambda _key: ("a" * 64, "b" * 64, "c" * 64),
    )
    doc = _document({"admin": {"name": "bootstrap-admin"}})
    session = FakeSession([None, "other-consumer"])

    with pytest.raises(BootstrapError, match="already assigned"):
        await runner._apply_admin(
            doc,
            _as_async_session(session),
            {"APEX_BOOTSTRAP_ADMIN_KEY": "shared-plaintext"},
            BootstrapReport(),
            lambda _message: None,
        )

    assert session.added == []
    conflict_sql = str(
        session.scalar_statements[1].compile(
            dialect=postgresql.dialect(),
            compile_kwargs={"literal_binds": True},
        )
    )
    assert "api_consumers.key_hash IN" in conflict_sql
    assert "EXISTS" in conflict_sql
    assert "consumer_keys.key_hash IN" in conflict_sql


async def test_admin_key_claim_takes_stable_postgres_lock_before_lookup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(runner, "hash_api_key", lambda _key: "a" * 64)
    monkeypatch.setattr(runner, "_candidate_key_hashes", lambda _key: ("a" * 64,))
    doc = _document({"admin": {"name": "bootstrap-admin"}})

    class PostgresSession(FakeSession):
        bind = SimpleNamespace(dialect=SimpleNamespace(name="postgresql"))

        def __init__(self) -> None:
            super().__init__([None, None])
            self.events: list[tuple[str, object]] = []

        async def execute(self, statement: object, parameters: object) -> None:
            self.events.append(("execute", (statement, parameters)))

        async def scalar(self, statement: ClauseElement) -> object | None:
            self.events.append(("scalar", statement))
            return await super().scalar(statement)

    lock_parameters_by_plaintext: list[dict[str, int]] = []
    for plaintext in ("stable-plaintext", "entirely-different-plaintext"):
        session = PostgresSession()
        await runner._apply_admin(
            doc,
            _as_async_session(session),
            {"APEX_BOOTSTRAP_ADMIN_KEY": plaintext},
            BootstrapReport(),
            lambda _message: None,
        )

        assert [event for event, _value in session.events] == [
            "execute",
            "scalar",
            "scalar",
        ]
        lock_statement, lock_parameters = cast(tuple[object, dict[str, int]], session.events[0][1])
        assert "pg_advisory_xact_lock" in str(lock_statement)
        assert plaintext not in repr((lock_statement, lock_parameters))
        lock_parameters_by_plaintext.append(lock_parameters)

    assert lock_parameters_by_plaintext == [
        {"lock_key": runner._BOOTSTRAP_ADMIN_LOCK_KEY},
        {"lock_key": runner._BOOTSTRAP_ADMIN_LOCK_KEY},
    ]


async def test_admin_rejects_synthetic_dev_key_before_database_io(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("APEX_AUTH__DEV_API_KEY", "shared-dev-key")
    doc = _document({"admin": {"name": "bootstrap-admin"}})
    session = FakeSession()

    with pytest.raises(BootstrapError, match="development API key"):
        await runner._apply_admin(
            doc,
            _as_async_session(session),
            {"APEX_BOOTSTRAP_ADMIN_KEY": "shared-dev-key"},
            BootstrapReport(),
            lambda _message: None,
        )

    assert session.scalar_statements == []
    assert session.added == []


@pytest.mark.parametrize(
    "raw_key",
    [
        " leading-space",
        "trailing-space ",
        "   ",
        "x" * 4_097,
        "é" * 2_049,
        "\ud800",
        b"not-a-native-string",
        123,
    ],
)
async def test_admin_rejects_unusable_key_before_hash_or_database_io(
    monkeypatch: pytest.MonkeyPatch,
    raw_key: object,
) -> None:
    def unexpected_hash(_value: str) -> str:
        raise AssertionError("invalid bootstrap key must not be hashed")

    monkeypatch.setattr(runner, "hash_api_key", unexpected_hash)
    monkeypatch.setattr(runner, "_candidate_key_hashes", unexpected_hash)
    doc = _document({"admin": {"name": "bootstrap-admin"}})
    session = FakeSession()
    env = cast(dict[str, str], {"APEX_BOOTSTRAP_ADMIN_KEY": raw_key})

    with pytest.raises(BootstrapError, match="bootstrap admin key") as exc_info:
        await runner._apply_admin(
            doc,
            _as_async_session(session),
            env,
            BootstrapReport(),
            lambda _message: None,
        )

    assert exc_info.value.__context__ is None
    assert exc_info.value.__cause__ is None
    assert session.scalar_statements == []
    assert session.added == []

"""DB-backed ConnectionResolver: precedence, scope checks, caching, fallbacks."""

import asyncio
import threading
from collections.abc import AsyncIterator, Iterator
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy.dialects import postgresql
from sqlalchemy.exc import SQLAlchemyError

from apex.adapters.registry import AdapterRegistry, ConnectionConfig, PortKind
from apex.services import connections as connections_service
from apex.services.connections import ConnectionResolver, StoredConnection


def _locked_settings() -> SimpleNamespace:
    return SimpleNamespace(is_locked_down=True, allow_private_adapter_hosts=False)


def stored(
    connection_id: str,
    *,
    kind: PortKind = PortKind.WORK_TRACKING,
    provider: str = "stub",
    project_id: str | None = None,
    enabled: bool = True,
    runtime_version: datetime | None = None,
    options: dict[str, Any] | None = None,
) -> StoredConnection:
    return StoredConnection(
        config=ConnectionConfig(
            id=connection_id,
            kind=kind,
            provider=provider,
            name=connection_id,
            options=options or {},
        ),
        project_id=project_id,
        enabled=enabled,
        runtime_version=runtime_version or datetime(2026, 1, 1, tzinfo=UTC),
    )


class FakeStore:
    """In-memory ConnectionStore mirroring DbConnectionStore selection rules."""

    def __init__(self, rows: list[StoredConnection] | None = None) -> None:
        self.rows: dict[str, StoredConnection] = {r.config.id: r for r in rows or []}
        self.error: Exception | None = None

    async def get(self, connection_id: str) -> StoredConnection | None:
        if self.error is not None:
            raise self.error
        return self.rows.get(connection_id)

    async def find_default(self, kind: PortKind, project_id: str | None) -> StoredConnection | None:
        if self.error is not None:
            raise self.error
        candidates = [r for r in self.rows.values() if r.config.kind is kind and r.enabled]
        if project_id is not None:
            scoped = [r for r in candidates if r.project_id == project_id]
            if scoped:
                return scoped[0]
        global_rows = [r for r in candidates if r.project_id is None]
        return global_rows[0] if global_rows else None


def _conn_id(adapter: object) -> str:
    """The stub adapters keep their ConnectionConfig on ._conn."""
    config = getattr(adapter, "_conn", None)
    assert config is not None
    return config.id


def test_internal_project_binding_never_executes_or_coerces_adapter_hooks() -> None:
    calls: list[str] = []

    class HostileBinding:
        def __str__(self) -> str:
            calls.append("str")
            raise AssertionError("binding coercion hook ran")

    class HostileAdapter:
        @property
        def apex_project_id(self) -> str:
            calls.append("property")
            raise AssertionError("binding descriptor hook ran")

    assert connections_service.internal_project_binding(HostileAdapter()) is None
    assert (
        connections_service.internal_project_binding(
            SimpleNamespace(apex_project_id=HostileBinding())
        )
        is None
    )
    assert (
        connections_service.internal_project_binding(SimpleNamespace(apex_project_id="project-a"))
        == "project-a"
    )
    assert calls == []


# ── precedence: connection_id > project > global > static ───────────────────


def test_db_default_selection_is_scoped_ordered_and_limited_in_sql() -> None:
    stmt = connections_service._default_connection_stmt(  # noqa: SLF001
        PortKind.WORK_TRACKING,
        "demo",
    )

    sql = str(
        stmt.compile(
            dialect=postgresql.dialect(),
            compile_kwargs={"literal_binds": True},
        )
    )

    assert "project_id = 'demo' OR apex.connections.project_id IS NULL" in sql
    assert "ORDER BY CASE WHEN (apex.connections.project_id = 'demo') THEN 0 ELSE 1 END" in sql
    assert "LIMIT 1" in sql


def test_db_global_default_selection_excludes_project_rows_in_sql() -> None:
    stmt = connections_service._default_connection_stmt(  # noqa: SLF001
        PortKind.WORK_TRACKING,
        None,
    )

    sql = str(
        stmt.compile(
            dialect=postgresql.dialect(),
            compile_kwargs={"literal_binds": True},
        )
    )

    assert "apex.connections.project_id IS NULL" in sql
    assert "project_id =" not in sql
    assert "LIMIT 1" in sql


async def test_project_row_beats_global_row() -> None:
    store = FakeStore([stored("global-wt"), stored("demo-wt", project_id="demo")])
    resolver = ConnectionResolver(store=store)
    assert _conn_id(await resolver.resolve(PortKind.WORK_TRACKING, project_id="demo")) == "demo-wt"
    assert _conn_id(await resolver.resolve(PortKind.WORK_TRACKING)) == "global-wt"


async def test_explicit_connection_id_beats_project_default() -> None:
    store = FakeStore([stored("global-wt"), stored("demo-wt", project_id="demo")])
    resolver = ConnectionResolver(store=store)
    adapter = await resolver.resolve(
        PortKind.WORK_TRACKING, connection_id="global-wt", project_id="demo"
    )
    assert _conn_id(adapter) == "global-wt"  # global rows are usable from any project


async def test_no_db_row_falls_back_to_static_stub() -> None:
    resolver = ConnectionResolver(store=FakeStore())
    adapter = await resolver.resolve(PortKind.WORK_TRACKING)
    assert _conn_id(adapter) == "dev-work-tracking-stub"


async def test_db_error_falls_back_to_static_stub() -> None:
    store = FakeStore([stored("global-wt")])
    store.error = SQLAlchemyError("connection refused")
    resolver = ConnectionResolver(store=store)
    adapter = await resolver.resolve(PortKind.WORK_TRACKING)
    assert _conn_id(adapter) == "dev-work-tracking-stub"


async def test_db_error_fails_closed_in_locked_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = FakeStore([stored("global-wt")])
    store.error = SQLAlchemyError("connection refused")
    monkeypatch.setattr(
        connections_service, "get_settings", lambda: SimpleNamespace(is_locked_down=True)
    )
    resolver = ConnectionResolver(store=store)

    with pytest.raises(RuntimeError, match="connection store unavailable"):
        await resolver.resolve(PortKind.WORK_TRACKING)


async def test_locked_store_failure_does_not_retain_raw_database_diagnostic(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    canary = "bare-database-driver-canary"
    store = FakeStore()
    store.error = SQLAlchemyError(canary)
    monkeypatch.setattr(
        connections_service, "get_settings", lambda: SimpleNamespace(is_locked_down=True)
    )
    resolver = ConnectionResolver(store=store)

    with pytest.raises(RuntimeError, match="connection store unavailable") as excinfo:
        await resolver.resolve(PortKind.WORK_TRACKING)

    assert excinfo.value.__cause__ is None
    assert excinfo.value.__context__ is None
    assert canary not in str(excinfo.value)


async def test_empty_store_does_not_fall_back_in_locked_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        connections_service, "get_settings", lambda: SimpleNamespace(is_locked_down=True)
    )
    resolver = ConnectionResolver(store=FakeStore())

    with pytest.raises(RuntimeError, match="static development fallbacks are disabled"):
        await resolver.resolve(PortKind.WORK_TRACKING)

    # An explicitly constructed non-DB resolver remains available to tests and
    # local embedded use even when settings are patched to a locked environment.
    assert _conn_id(await ConnectionResolver().resolve(PortKind.WORK_TRACKING)) == (
        "dev-work-tracking-stub"
    )


async def test_resolve_with_connection_id_merges_options_and_checks_provider() -> None:
    row = stored("engine-a", kind=PortKind.EXECUTION_ENGINE, provider="sim")
    row = StoredConnection(
        config=row.config.model_copy(update={"options": {"base": "kept"}}),
        project_id=row.project_id,
        enabled=row.enabled,
        runtime_version=row.runtime_version,
    )
    resolver = ConnectionResolver(store=FakeStore([row]))

    base = await resolver.resolve(PortKind.EXECUTION_ENGINE)
    adapter, resolved_id = await resolver.resolve_with_connection_id(
        PortKind.EXECUTION_ENGINE,
        expected_provider="sim",
        options_overlay={"fail_at_pct": 50.0},
    )
    second_overlay, _ = await resolver.resolve_with_connection_id(
        PortKind.EXECUTION_ENGINE,
        expected_provider="sim",
        options_overlay={"fail_at_pct": 75.0},
    )
    assert resolved_id == "engine-a"
    assert base._conn.options == {"base": "kept"}
    assert adapter._conn.options == {"base": "kept", "fail_at_pct": 50.0}
    assert second_overlay._conn.options == {"base": "kept", "fail_at_pct": 75.0}
    assert len({id(base), id(adapter), id(second_overlay)}) == 3

    with pytest.raises(ValueError, match="not requested provider"):
        await resolver.resolve_with_connection_id(
            PortKind.EXECUTION_ENGINE, expected_provider="loadrunner"
        )


async def test_overlay_cache_key_uses_the_full_sha256_digest(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    digests = iter(("a" * 16 + "1" * 48, "a" * 16 + "2" * 48))

    class FakeDigest:
        def __init__(self, value: str) -> None:
            self.value = value

        def hexdigest(self) -> str:
            return self.value

    monkeypatch.setattr(
        connections_service.hashlib,
        "sha256",
        lambda _payload: FakeDigest(next(digests)),
    )
    row = stored("engine-a", kind=PortKind.EXECUTION_ENGINE, provider="sim")
    resolver = ConnectionResolver(store=FakeStore([row]))

    first, _ = await resolver.resolve_with_connection_id(
        PortKind.EXECUTION_ENGINE,
        expected_provider="sim",
        options_overlay={"fail_at_pct": 50.0},
    )
    second, _ = await resolver.resolve_with_connection_id(
        PortKind.EXECUTION_ENGINE,
        expected_provider="sim",
        options_overlay={"fail_at_pct": 75.0},
    )

    assert first._managed is not second._managed
    assert first._conn.options != second._conn.options


async def test_resolve_with_metadata_supports_slotted_execution_adapter() -> None:
    provider = "test-slotted-execution"
    version = datetime(2026, 2, 3, tzinfo=UTC)

    class SlottedAdapter:
        __slots__ = ("conn",)

        def __init__(self, conn: ConnectionConfig, secret: object | None = None) -> None:
            self.conn = conn

    AdapterRegistry.register(PortKind.EXECUTION_ENGINE, provider)(SlottedAdapter)
    try:
        resolver = ConnectionResolver(
            store=FakeStore(
                [
                    stored(
                        "slot-engine",
                        kind=PortKind.EXECUTION_ENGINE,
                        provider=provider,
                        runtime_version=version,
                    )
                ]
            )
        )

        resolved = await resolver.resolve_with_metadata(
            PortKind.EXECUTION_ENGINE,
            connection_id="slot-engine",
            expected_provider=provider,
        )
        assert resolved.adapter.conn.id == "slot-engine"
        assert resolved.connection_id == "slot-engine"
        assert resolved.connection_version == version
        assert resolved.persisted is True
    finally:
        AdapterRegistry._factories.pop((PortKind.EXECUTION_ENGINE, provider), None)


@pytest.mark.parametrize(
    "key",
    ["base_url", "endpoint", "secure", "verify_tls", "_apex_trusted_private_host"],
)
async def test_options_overlay_cannot_change_target_or_trust_policy(key: str) -> None:
    row = stored("engine-a", kind=PortKind.EXECUTION_ENGINE, provider="sim")
    resolver = ConnectionResolver(store=FakeStore([row]))

    with pytest.raises(ValueError, match="cannot change network target or trust policy"):
        await resolver.resolve_with_connection_id(
            PortKind.EXECUTION_ENGINE,
            options_overlay={key: "https://attacker.example"},
        )


@pytest.mark.parametrize(
    ("provider", "option_name", "configured", "override"),
    [
        ("jira", "project_key", "PHX", "OTHER"),
        ("jira", "project_key", "PHX", None),
        ("ado", "project", "Phoenix", "Sibling"),
        ("ado", "project", "Phoenix", None),
    ],
)
async def test_scoped_tracker_overlay_cannot_change_external_project_before_checkout(
    monkeypatch: pytest.MonkeyPatch,
    provider: str,
    option_name: str,
    configured: str,
    override: str | None,
) -> None:
    row = stored(
        "scoped-tracker",
        provider=provider,
        project_id="project-1",
        options={option_name: configured},
    )
    resolver = ConnectionResolver(store=FakeStore([row]))
    builds = 0

    async def build(*args: object, **kwargs: object) -> object:
        nonlocal builds
        builds += 1
        return SimpleNamespace(provider=provider, project_id=configured)

    monkeypatch.setattr(resolver, "_build_cached", build)

    with pytest.raises(ValueError, match="provider project boundary"):
        await resolver.resolve_with_metadata(
            PortKind.WORK_TRACKING,
            project_id="project-1",
            options_overlay={option_name: override},
        )

    # The overlay is rejected before a checkout exists, so there is no lease to leak.
    assert builds == 0


def test_locked_environment_requires_https_for_public_adapter_targets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(connections_service, "get_settings", _locked_settings)

    with pytest.raises(ValueError, match="must use https"):
        connections_service.validate_adapter_base_url("http://api.example.com")
    connections_service.validate_adapter_base_url("https://api.example.com")


def test_locked_environment_preserves_explicit_trusted_private_http_target(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(connections_service, "get_settings", _locked_settings)

    connections_service.validate_adapter_base_url(
        "http://minio.apex.svc.cluster.local:9000",
        allow_private_hosts=True,
    )


def test_adapter_url_rejects_hostile_string_and_approval_subclasses_without_hooks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    hooks: list[str] = []

    class HostileUrl(str):
        def strip(self, *args: object, **kwargs: object) -> str:
            hooks.append("strip")
            raise AssertionError("hostile URL hook executed")

    class HostileApproval:
        def __bool__(self) -> bool:
            hooks.append("bool")
            raise AssertionError("hostile approval hook executed")

    monkeypatch.setattr(connections_service, "get_settings", _locked_settings)
    with pytest.raises(ValueError, match="must be a string"):
        connections_service.validate_adapter_base_url(HostileUrl("https://safe.example"))
    with pytest.raises(ValueError, match="approval must be a boolean"):
        connections_service.validate_adapter_base_url(
            "https://safe.example",
            allow_private_hosts=HostileApproval(),  # type: ignore[arg-type]
        )

    assert hooks == []


def test_connection_row_rejects_hostile_or_unbounded_options_before_copy_hooks() -> None:
    hooks: list[str] = []

    class HostileOptions(dict[str, Any]):
        def items(self):  # type: ignore[no-untyped-def]
            hooks.append("items")
            raise AssertionError("hostile mapping hook executed")

    def row(options: object) -> SimpleNamespace:
        return SimpleNamespace(
            id="conn-1",
            kind="work_tracking",
            provider="stub",
            name="connection",
            base_url=None,
            options=options,
            secret_ref=None,
        )

    with pytest.raises(ValueError, match="options require repair"):
        connections_service.connection_config_from_row(row(HostileOptions()))

    cycle: dict[str, Any] = {}
    cycle["cycle"] = cycle
    with pytest.raises(ValueError, match="repeated or circular"):
        connections_service.connection_config_from_row(row(cycle))

    with pytest.raises(ValueError, match="node limit"):
        connections_service.connection_config_from_row(row({"items": [None] * 2_001}))

    assert hooks == []


def test_locked_environment_rejects_disabled_tls_verification(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(connections_service, "get_settings", _locked_settings)
    config = ConnectionConfig(
        id="elk",
        kind=PortKind.LOG_SEARCH,
        provider="elk",
        name="ELK",
        options={"base_url": "https://elk.example.com", "verify_tls": False},
    )

    with pytest.raises(ValueError, match="TLS verification cannot be disabled"):
        connections_service.validate_connection_config(config)

    trusted = config.model_copy(
        update={
            "options": {
                "base_url": "http://elk.apex.svc.cluster.local:9200",
                "verify_tls": False,
                connections_service.TRUSTED_PRIVATE_HOST_OPTION: True,
            }
        }
    )
    connections_service.validate_connection_config(trusted)


@pytest.mark.parametrize(
    "endpoint",
    [
        "https://minio.example.test:9000/prefix",
        "https://minio.example.test:9000?signature=opaque",
        "https://user:password@minio.example.test:9000",
        "minio.example.test:9000/prefix",
    ],
)
def test_s3_endpoint_requires_exact_host_port_transport_contract(endpoint: str) -> None:
    config = ConnectionConfig(
        id="s3-endpoint",
        kind=PortKind.ARTIFACT_STORE,
        provider="s3",
        name="S3",
        options={"endpoint": endpoint, "secure": True},
    )

    with pytest.raises(ValueError, match="s3 endpoint") as error:
        connections_service.validate_connection_config(config)

    assert "password" not in str(error.value)
    assert "signature" not in str(error.value)


async def test_invalid_transport_is_rejected_before_secret_resolution(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(connections_service, "get_settings", _locked_settings)
    config = ConnectionConfig(
        id="plaintext-secret-connection",
        kind=PortKind.WORK_TRACKING,
        provider="stub",
        name="plaintext",
        options={"base_url": "http://tracker.example.com"},
        secret_ref="env:APEX_INTEGRATION_TRACKER_TOKEN",
    )
    resolver = ConnectionResolver([config])
    secret_resolution_calls: list[PortKind] = []

    async def resolve(kind: PortKind, *_args: object, **_kwargs: object) -> object:
        secret_resolution_calls.append(kind)
        raise AssertionError("secret resolution must not run for an invalid transport")

    monkeypatch.setattr(resolver, "resolve", resolve)

    with pytest.raises(ValueError, match="must use https"):
        await resolver._build_cached(config, None)  # noqa: SLF001
    assert secret_resolution_calls == []


@pytest.mark.parametrize(
    ("options", "secret_ref", "expected_error", "canary"),
    [
        (
            {"nested": {"api_token": "legacy-option-canary-7c3a"}},
            "env:APEX_INTEGRATION_TRACKER_TOKEN",
            "connection options secrets must be supplied through secret_ref",
            "legacy-option-canary-7c3a",
        ),
        (
            {
                "base_url": (
                    "https://operator:legacy-url-canary-8d4b@tracker.example/api"
                    "?access_token=query-canary#fragment-canary"
                )
            },
            "env:APEX_INTEGRATION_TRACKER_TOKEN",
            "without embedded credentials",
            "legacy-url-canary-8d4b",
        ),
        (
            {},
            "legacy-secret-ref-canary-6b2e",
            "supported env:NAME reference format",
            "legacy-secret-ref-canary-6b2e",
        ),
    ],
)
async def test_legacy_connection_credentials_fail_before_provider_build(
    monkeypatch: pytest.MonkeyPatch,
    options: dict[str, Any],
    secret_ref: str,
    expected_error: str,
    canary: str,
) -> None:
    row = stored("legacy-connection")
    row = StoredConnection(
        config=row.config.model_copy(
            update={"options": options, "secret_ref": secret_ref},
        ),
        project_id=row.project_id,
        enabled=row.enabled,
        runtime_version=row.runtime_version,
    )
    resolver = ConnectionResolver(store=FakeStore([row]))
    build_called = False

    async def build(*_args: object, **_kwargs: object) -> object:
        nonlocal build_called
        build_called = True
        raise AssertionError("provider build must not run for a repair-required row")

    monkeypatch.setattr(resolver, "_build_cached", build)

    with pytest.raises(ValueError, match=expected_error) as error:
        await resolver.resolve(
            PortKind.WORK_TRACKING,
            connection_id="legacy-connection",
        )

    assert build_called is False
    assert canary not in str(error.value)


async def test_raw_legacy_option_is_rejected_before_secret_resolution(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    canary = "direct-build-secret-canary-5e1f"
    config = ConnectionConfig(
        id="legacy-direct-build",
        kind=PortKind.WORK_TRACKING,
        provider="stub",
        name="legacy",
        options={"password": canary},
        secret_ref="env:APEX_INTEGRATION_TRACKER_TOKEN",
    )
    resolver = ConnectionResolver([config])
    secret_resolution_calls: list[PortKind] = []

    async def resolve(kind: PortKind, *_args: object, **_kwargs: object) -> object:
        secret_resolution_calls.append(kind)
        raise AssertionError("secret resolution must not run for raw legacy options")

    monkeypatch.setattr(resolver, "resolve", resolve)

    with pytest.raises(ValueError, match="secrets must be supplied") as error:
        await resolver._build_cached(config, None)  # noqa: SLF001

    assert secret_resolution_calls == []
    assert canary not in str(error.value)


async def test_work_tracking_uses_internal_binding_not_external_project_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    resolver = ConnectionResolver(
        store=FakeStore(
            [
                stored(
                    "jira-p1",
                    provider="jira",
                    project_id="internal-project-1",
                    options={"project_key": "PHX"},
                )
            ]
        )
    )

    async def jira(*args: object, **kwargs: object) -> object:
        # External Jira key intentionally differs from the owning APEX project.
        return SimpleNamespace(provider="jira", project_id="PHX")

    monkeypatch.setattr(resolver, "_build_cached", jira)
    adapter = await resolver.resolve(PortKind.WORK_TRACKING, project_id="internal-project-1")

    assert adapter.project_id == "PHX"
    assert adapter.apex_project_id == "internal-project-1"


async def test_scoped_jira_without_external_project_is_rejected_before_build(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    resolver = ConnectionResolver(
        store=FakeStore(
            [stored("jira-p1", provider="jira", project_id="internal-project-secret-canary")]
        )
    )
    built = False

    async def jira(*args: object, **kwargs: object) -> object:
        nonlocal built
        built = True
        return SimpleNamespace(provider="jira", project_id=None)

    monkeypatch.setattr(resolver, "_build_cached", jira)

    with pytest.raises(ValueError) as error:
        await resolver.resolve(
            PortKind.WORK_TRACKING,
            project_id="internal-project-secret-canary",
        )

    assert built is False
    assert "internal-project-secret-canary" not in str(error.value)


async def test_scoped_real_tracker_missing_runtime_project_closes_rejected_checkout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    closed = 0

    class MissingProjectAdapter:
        provider = "jira"

        async def aclose(self) -> None:
            nonlocal closed
            closed += 1

    resolver = ConnectionResolver(
        store=FakeStore(
            [
                stored(
                    "jira-p1",
                    provider="jira",
                    project_id="internal-project-1",
                    options={"project_key": "PHX"},
                )
            ]
        )
    )

    async def jira(*args: object, **kwargs: object) -> object:
        return MissingProjectAdapter()

    monkeypatch.setattr(resolver, "_build_cached", jira)

    with pytest.raises(ValueError, match="requires a bounded external project"):
        await resolver.resolve(PortKind.WORK_TRACKING, project_id="internal-project-1")

    assert closed == 1


async def test_global_real_work_tracking_connection_rejected_for_scoped_project(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    resolver = ConnectionResolver(store=FakeStore([stored("global-jira", provider="jira")]))
    built = False

    async def jira(*args: object, **kwargs: object) -> object:
        nonlocal built
        built = True
        return SimpleNamespace(provider="jira", project_id="PHX")

    monkeypatch.setattr(resolver, "_build_cached", jira)
    with pytest.raises(ValueError, match="not internally bound"):
        await resolver.resolve(PortKind.WORK_TRACKING, project_id="internal-project-1")

    assert built is False


async def test_global_stub_work_tracking_connection_allowed_for_scoped_project(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    resolver = ConnectionResolver(store=FakeStore([stored("global-wt")]))

    async def stub(*args: object, **kwargs: object) -> object:
        return SimpleNamespace(provider="stub")

    monkeypatch.setattr(resolver, "_build_cached", stub)
    assert await resolver.resolve(PortKind.WORK_TRACKING, project_id="project-a")


# ── scope / state checks on explicit connection_id ───────────────────────────


async def test_explicit_id_scoped_to_other_project_raises() -> None:
    store = FakeStore([stored("other-wt", project_id="other")])
    resolver = ConnectionResolver(store=store)
    with pytest.raises(ValueError, match="scoped to project 'other'"):
        await resolver.resolve(PortKind.WORK_TRACKING, connection_id="other-wt", project_id="demo")


async def test_explicit_id_scoped_project_requires_project_context() -> None:
    store = FakeStore([stored("demo-wt", project_id="demo")])
    resolver = ConnectionResolver(store=store)
    with pytest.raises(ValueError, match="not None"):
        await resolver.resolve(PortKind.WORK_TRACKING, connection_id="demo-wt")


async def test_explicit_id_disabled_raises() -> None:
    store = FakeStore([stored("off-wt", enabled=False)])
    resolver = ConnectionResolver(store=store)
    with pytest.raises(ValueError, match="disabled"):
        await resolver.resolve(PortKind.WORK_TRACKING, connection_id="off-wt")


async def test_explicit_id_kind_mismatch_raises() -> None:
    store = FakeStore([stored("logs", kind=PortKind.LOG_SEARCH)])
    resolver = ConnectionResolver(store=store)
    with pytest.raises(ValueError, match="kind"):
        await resolver.resolve(PortKind.WORK_TRACKING, connection_id="logs")


async def test_unknown_id_still_raises_keyerror() -> None:
    resolver = ConnectionResolver(store=FakeStore())
    with pytest.raises(KeyError, match="no-such-conn"):
        await resolver.resolve(PortKind.WORK_TRACKING, connection_id="no-such-conn")


async def test_runtime_rejects_private_s3_endpoint_before_adapter_build() -> None:
    config = ConnectionConfig(
        id="private-store",
        kind=PortKind.ARTIFACT_STORE,
        provider="s3",
        name="private-store",
        options={"endpoint": "http://169.254.169.254"},
    )
    resolver = ConnectionResolver(connections=[config])

    with pytest.raises(ValueError, match="private adapter hosts are disabled"):
        await resolver.resolve(PortKind.ARTIFACT_STORE)


async def test_static_dev_id_resolves_even_with_store() -> None:
    resolver = ConnectionResolver(store=FakeStore())
    adapter = await resolver.resolve(PortKind.WORK_TRACKING, connection_id="dev-work-tracking-stub")
    assert _conn_id(adapter) == "dev-work-tracking-stub"


# ── caching keyed by (connection_id, runtime_version) ────────────────────────


async def test_cache_hit_for_unchanged_row() -> None:
    store = FakeStore([stored("global-wt")])
    resolver = ConnectionResolver(store=store)
    first = await resolver.resolve(PortKind.WORK_TRACKING)
    second = await resolver.resolve(PortKind.WORK_TRACKING)
    assert first._managed is second._managed


async def test_cache_invalidated_when_runtime_version_changes() -> None:
    store = FakeStore([stored("global-wt", runtime_version=datetime(2026, 1, 1, tzinfo=UTC))])
    resolver = ConnectionResolver(store=store)
    before = await resolver.resolve(PortKind.WORK_TRACKING)

    # Runtime-affecting PATCH advances the generation and rebuilds the adapter.
    store.rows["global-wt"] = stored("global-wt", runtime_version=datetime(2026, 1, 2, tzinfo=UTC))
    after = await resolver.resolve(PortKind.WORK_TRACKING)
    assert after is not before

    again = await resolver.resolve(PortKind.WORK_TRACKING)
    assert again._managed is after._managed  # the new generation is cached


async def test_static_resolver_unchanged_without_store() -> None:
    resolver = ConnectionResolver()
    first = await resolver.resolve(PortKind.WORK_TRACKING)
    second = await resolver.resolve(PortKind.WORK_TRACKING, "dev-work-tracking-stub")
    assert first._managed is second._managed


@pytest.fixture
def closeable_provider() -> Iterator[tuple[str, list[str]]]:
    provider = "test-closeable"
    closed: list[str] = []

    class CloseableAdapter:
        def __init__(self, conn: ConnectionConfig, secret: object | None = None) -> None:
            self.conn = conn

        async def aclose(self) -> None:
            closed.append(self.conn.id)

    AdapterRegistry.register(PortKind.WORK_TRACKING, provider)(CloseableAdapter)
    try:
        yield provider, closed
    finally:
        AdapterRegistry._factories.pop((PortKind.WORK_TRACKING, provider), None)


async def test_cache_invalidation_closes_idle_replaced_adapter(
    closeable_provider: tuple[str, list[str]],
) -> None:
    provider, closed = closeable_provider
    store = FakeStore(
        [
            stored(
                "global-wt",
                provider=provider,
                runtime_version=datetime(2026, 1, 1, tzinfo=UTC),
            )
        ]
    )
    resolver = ConnectionResolver(store=store)

    first = await resolver.resolve(PortKind.WORK_TRACKING)
    first_generation = first._managed
    del first
    store.rows["global-wt"] = stored(
        "global-wt", provider=provider, runtime_version=datetime(2026, 1, 2, tzinfo=UTC)
    )
    second = await resolver.resolve(PortKind.WORK_TRACKING)

    assert second._managed is not first_generation
    assert closed == ["global-wt"]

    await second.aclose()
    await resolver.close()

    assert closed == ["global-wt", "global-wt"]


async def test_cache_rotation_defers_close_until_inflight_coroutine_finishes() -> None:
    provider = "test-leased-coroutine"
    started = asyncio.Event()
    release = asyncio.Event()
    closed: list[int] = []
    created: list[object] = []

    class LeasedAdapter:
        def __init__(self, conn: ConnectionConfig, secret: object | None = None) -> None:
            self.generation = len(created)
            created.append(self)

        async def operation(self) -> str:
            if self.generation == 0:
                started.set()
                await release.wait()
            return f"generation-{self.generation}"

        async def aclose(self) -> None:
            closed.append(self.generation)

    AdapterRegistry.register(PortKind.WORK_TRACKING, provider)(LeasedAdapter)
    store = FakeStore(
        [
            stored(
                "global-wt",
                provider=provider,
                runtime_version=datetime(2026, 1, 1, tzinfo=UTC),
            )
        ]
    )
    resolver = ConnectionResolver(store=store)
    try:
        first = await resolver.resolve(PortKind.WORK_TRACKING)
        store.rows["global-wt"] = stored(
            "global-wt", provider=provider, runtime_version=datetime(2026, 1, 2, tzinfo=UTC)
        )
        second = await resolver.resolve(PortKind.WORK_TRACKING)
        assert closed == []

        # Generation A remains callable even though the resolver retired it in
        # the gap after resolve and before the first port method started.
        call = asyncio.create_task(first.operation())
        await started.wait()
        release.set()
        assert await call == "generation-0"
        assert closed == []  # the per-resolution checkout is still held
        await first.aclose()
        assert closed == [0]
        assert await second.operation() == "generation-1"

        await second.aclose()
        await resolver.close()
        assert closed == [0, 1]
    finally:
        AdapterRegistry._factories.pop((PortKind.WORK_TRACKING, provider), None)


async def test_cache_rotation_leases_async_iterator_through_explicit_close() -> None:
    provider = "test-leased-iterator"
    started = asyncio.Event()
    release = asyncio.Event()
    closed: list[int] = []
    generation = 0

    class StreamingAdapter:
        def __init__(self, conn: ConnectionConfig, secret: object | None = None) -> None:
            nonlocal generation
            self.generation = generation
            generation += 1

        async def iter_bytes(self) -> AsyncIterator[bytes]:
            if self.generation == 0:
                started.set()
                await release.wait()
            yield b"chunk"

        async def aclose(self) -> None:
            closed.append(self.generation)

    AdapterRegistry.register(PortKind.WORK_TRACKING, provider)(StreamingAdapter)
    store = FakeStore(
        [
            stored(
                "global-wt",
                provider=provider,
                runtime_version=datetime(2026, 1, 1, tzinfo=UTC),
            )
        ]
    )
    resolver = ConnectionResolver(store=store)
    try:
        first = await resolver.resolve(PortKind.WORK_TRACKING)
        iterator = first.iter_bytes()
        store.rows["global-wt"] = stored(
            "global-wt", provider=provider, runtime_version=datetime(2026, 1, 2, tzinfo=UTC)
        )
        second = await resolver.resolve(PortKind.WORK_TRACKING)
        assert closed == []

        # The iterator's lease covers the gap between construction and first
        # pull, even after the resolver retires its generation.
        first_pull = asyncio.create_task(anext(iterator))
        await started.wait()
        release.set()
        assert await first_pull == b"chunk"
        assert closed == []  # lease spans the iterator, not just one __anext__
        await iterator.aclose()
        assert closed == []  # the original checkout still protects generation A
        await first.aclose()
        assert closed == [0]

        await second.aclose()
        await resolver.close()
        assert closed == [0, 1]
    finally:
        AdapterRegistry._factories.pop((PortKind.WORK_TRACKING, provider), None)


async def test_repeated_idle_rotations_do_not_accumulate_retired_adapters(
    closeable_provider: tuple[str, list[str]],
) -> None:
    provider, closed = closeable_provider
    store = FakeStore()
    resolver = ConnectionResolver(store=store)

    for day in range(1, 11):
        store.rows["global-wt"] = stored(
            "global-wt",
            provider=provider,
            runtime_version=datetime(2026, 1, day, tzinfo=UTC),
        )
        checkout = await resolver.resolve(PortKind.WORK_TRACKING)
        del checkout
        await asyncio.sleep(0)

    assert len(closed) == 9
    await resolver.close()
    assert len(closed) == 10


async def test_resolver_close_closes_cached_adapters(
    closeable_provider: tuple[str, list[str]],
) -> None:
    provider, closed = closeable_provider
    store = FakeStore([stored("global-wt", provider=provider)])
    resolver = ConnectionResolver(store=store)

    await resolver.resolve(PortKind.WORK_TRACKING)
    await resolver.close()

    assert closed == ["global-wt"]


async def test_close_failure_and_proxy_class_projection_do_not_invoke_adapter_class_hook() -> None:
    hooks: list[str] = []

    class HostileCloseAdapter:
        def __getattribute__(self, name: str) -> Any:
            if name == "__class__":
                hooks.append(name)
                raise AssertionError("adapter __class__ hook executed")
            return object.__getattribute__(self, name)

        async def aclose(self) -> None:
            raise OSError("close failed")

    managed = connections_service._ManagedAdapter(HostileCloseAdapter())
    checkout = managed.checkout()
    assert checkout.__class__ is HostileCloseAdapter
    await checkout.aclose()
    assert managed.closed is False
    await managed.retire()

    assert managed.closed is True
    assert hooks == []


async def test_sync_adapter_result_descriptor_is_rejected_without_execution() -> None:
    hooks: list[str] = []

    class HostileResult:
        @property
        def __aiter__(self) -> Any:
            hooks.append("__aiter__")
            raise AssertionError("hostile result descriptor executed")

    class SyncAdapter:
        def result(self) -> HostileResult:
            return HostileResult()

    managed = connections_service._ManagedAdapter(SyncAdapter())
    checkout = managed.checkout()
    with pytest.raises(TypeError, match="unsupported asynchronous result"):
        checkout.result()
    await checkout.aclose()

    assert hooks == []


async def test_cancelled_proxy_close_still_finishes_retired_adapter_cleanup() -> None:
    close_started = asyncio.Event()
    allow_close = asyncio.Event()
    close_finished = asyncio.Event()

    class SlowCloseAdapter:
        async def aclose(self) -> None:
            close_started.set()
            await allow_close.wait()
            close_finished.set()

    managed = connections_service._ManagedAdapter(SlowCloseAdapter())
    checkout = managed.checkout()
    await checkout.aclose()
    close_task = asyncio.create_task(managed.retire())
    await asyncio.wait_for(close_started.wait(), timeout=1)

    close_task.cancel()
    await asyncio.sleep(0)
    close_task.cancel()
    await asyncio.sleep(0)
    assert close_task.done() is False
    assert managed.retired is True
    assert managed.closed is False

    allow_close.set()
    with pytest.raises(asyncio.CancelledError):
        await close_task
    assert close_finished.is_set()
    assert managed.active_calls == 0
    assert managed.closed is True


async def test_adapter_close_survives_repeated_caller_cancellation() -> None:
    close_started = asyncio.Event()
    allow_close = asyncio.Event()
    close_finished = asyncio.Event()

    class SlowCloseAdapter:
        async def aclose(self) -> None:
            close_started.set()
            await allow_close.wait()
            close_finished.set()

    close_task = asyncio.create_task(connections_service.close_adapter(SlowCloseAdapter()))
    await close_started.wait()
    close_task.cancel()
    await asyncio.sleep(0)
    close_task.cancel()
    await asyncio.sleep(0)

    assert close_task.done() is False
    allow_close.set()
    with pytest.raises(asyncio.CancelledError):
        await close_task
    assert close_finished.is_set()


async def test_proxy_close_releases_checkout_without_retiring_shared_generation() -> None:
    closed = 0

    class Adapter:
        async def aclose(self) -> None:
            nonlocal closed
            closed += 1

    managed = connections_service._ManagedAdapter(Adapter())
    first = managed.checkout()
    await first.aclose()

    assert managed.active_calls == 0
    assert managed.retired is False
    assert managed.closed is False

    second = managed.checkout()
    await second.aclose()
    await managed.retire()
    assert managed.closed is True
    assert closed == 1


async def test_nested_secret_checkout_is_released_after_adapter_build(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = "test-secret-checkout-release"
    monkeypatch.setenv("APEX_INTEGRATION_TRACKER_TOKEN", "secret-checkout-value")

    class Adapter:
        def __init__(self, conn: ConnectionConfig, secret: object | None = None) -> None:
            assert secret is not None

    AdapterRegistry.register(PortKind.WORK_TRACKING, provider)(Adapter)
    resolver = ConnectionResolver(
        connections=[
            ConnectionConfig(
                id="tracker-with-secret",
                kind=PortKind.WORK_TRACKING,
                provider=provider,
                name="tracker with secret",
                secret_ref="env:APEX_INTEGRATION_TRACKER_TOKEN",
            ),
            ConnectionConfig(
                id="secret-source",
                kind=PortKind.SECRETS,
                provider="env",
                name="secret source",
            ),
        ]
    )
    try:
        checkout = await resolver.resolve(PortKind.WORK_TRACKING)
        cache = resolver._cache_for_current_loop()
        secret_managed = cache.instances["secret-source"][1]
        assert secret_managed.active_calls == 0
        assert secret_managed.retired is False
        await checkout.aclose()
    finally:
        await resolver.close()
        AdapterRegistry._factories.pop((PortKind.WORK_TRACKING, provider), None)


async def test_nested_secret_checkout_is_released_when_adapter_factory_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = "test-secret-checkout-factory-failure"
    monkeypatch.setenv("APEX_INTEGRATION_TRACKER_TOKEN", "secret-checkout-value")

    def fail_factory(conn: ConnectionConfig, secret: object | None = None) -> None:
        assert secret is not None
        raise RuntimeError("factory failed")

    AdapterRegistry.register(PortKind.WORK_TRACKING, provider)(fail_factory)
    resolver = ConnectionResolver(
        connections=[
            ConnectionConfig(
                id="failing-tracker-with-secret",
                kind=PortKind.WORK_TRACKING,
                provider=provider,
                name="failing tracker with secret",
                secret_ref="env:APEX_INTEGRATION_TRACKER_TOKEN",
            ),
            ConnectionConfig(
                id="secret-source",
                kind=PortKind.SECRETS,
                provider="env",
                name="secret source",
            ),
        ]
    )
    try:
        with pytest.raises(RuntimeError, match="factory failed"):
            await resolver.resolve(PortKind.WORK_TRACKING)
        cache = resolver._cache_for_current_loop()
        assert cache.instances["secret-source"][1].active_calls == 0
        assert "failing-tracker-with-secret" not in cache.instances
    finally:
        await resolver.close()
        AdapterRegistry._factories.pop((PortKind.WORK_TRACKING, provider), None)


async def test_cancelled_iterator_close_still_releases_retired_adapter_lease() -> None:
    iterator_close_started = asyncio.Event()
    allow_iterator_close = asyncio.Event()
    adapter_closed = asyncio.Event()

    class SlowIterator:
        def __aiter__(self) -> "SlowIterator":
            return self

        async def __anext__(self) -> bytes:
            return b"chunk"

        async def aclose(self) -> None:
            iterator_close_started.set()
            await allow_iterator_close.wait()

    class CloseableAdapter:
        async def aclose(self) -> None:
            adapter_closed.set()

    managed = connections_service._ManagedAdapter(CloseableAdapter())
    iterator = connections_service._LeasedAsyncIterator(
        managed,
        SlowIterator(),
        asyncio.get_running_loop(),
    )
    await managed.retire()

    close_task = asyncio.create_task(iterator.aclose())
    await asyncio.wait_for(iterator_close_started.wait(), timeout=1)
    close_task.cancel()
    await asyncio.sleep(0)
    close_task.cancel()
    await asyncio.sleep(0)
    assert close_task.done() is False
    assert managed.active_calls == 1

    allow_iterator_close.set()
    with pytest.raises(asyncio.CancelledError):
        await close_task
    assert adapter_closed.is_set()
    assert managed.active_calls == 0
    assert managed.closed is True


async def test_cancelled_resolver_close_still_retires_every_cached_adapter() -> None:
    close_started = [asyncio.Event(), asyncio.Event()]
    allow_close = asyncio.Event()
    close_finished = [asyncio.Event(), asyncio.Event()]
    created = 0
    provider = "test-cancelled-resolver-close"

    class SlowCloseAdapter:
        def __init__(self, conn: ConnectionConfig, secret: object | None = None) -> None:
            nonlocal created
            self.index = created
            created += 1

        async def aclose(self) -> None:
            close_started[self.index].set()
            await allow_close.wait()
            close_finished[self.index].set()

    AdapterRegistry.register(PortKind.WORK_TRACKING, provider)(SlowCloseAdapter)
    AdapterRegistry.register(PortKind.LOG_SEARCH, provider)(SlowCloseAdapter)
    resolver = ConnectionResolver(
        connections=[
            ConnectionConfig(
                id="slow-work-tracking",
                kind=PortKind.WORK_TRACKING,
                provider=provider,
                name="slow work tracking",
            ),
            ConnectionConfig(
                id="slow-log-search",
                kind=PortKind.LOG_SEARCH,
                provider=provider,
                name="slow log search",
            ),
        ]
    )
    try:
        work_tracking = await resolver.resolve(PortKind.WORK_TRACKING)
        log_search = await resolver.resolve(PortKind.LOG_SEARCH)
        del work_tracking, log_search
        await asyncio.sleep(0)

        close_task = asyncio.create_task(resolver.close())
        await asyncio.gather(*(event.wait() for event in close_started))
        close_task.cancel()
        await asyncio.sleep(0)
        close_task.cancel()
        await asyncio.sleep(0)
        assert close_task.done() is False

        # A repeated shutdown attempt sees the detached cache, but the first
        # owned close task must still retain and retire its complete snapshot.
        await resolver.close()
        allow_close.set()
        with pytest.raises(asyncio.CancelledError):
            await close_task
        assert all(event.is_set() for event in close_finished)
    finally:
        AdapterRegistry._factories.pop((PortKind.WORK_TRACKING, provider), None)
        AdapterRegistry._factories.pop((PortKind.LOG_SEARCH, provider), None)


def test_shared_resolver_isolates_and_closes_adapters_per_event_loop() -> None:
    provider = "test-multiloop"
    rendezvous = threading.Barrier(2)
    resolver = ConnectionResolver(
        connections=[
            ConnectionConfig(
                id="multi-loop",
                kind=PortKind.WORK_TRACKING,
                provider=provider,
                name="multi-loop",
            )
        ]
    )

    class LoopOwnedAdapter:
        def __init__(self, conn: ConnectionConfig, secret: object | None = None) -> None:
            self.created_on = id(asyncio.get_running_loop())
            self.closed_on: int | None = None

        async def aclose(self) -> None:
            self.closed_on = id(asyncio.get_running_loop())

    AdapterRegistry.register(PortKind.WORK_TRACKING, provider)(LoopOwnedAdapter)

    async def use_from_one_loop() -> LoopOwnedAdapter:
        rendezvous.wait(timeout=5)
        first = await resolver.resolve(PortKind.WORK_TRACKING)
        second = await resolver.resolve(PortKind.WORK_TRACKING)
        assert first._managed is second._managed
        raw = first._managed.adapter
        await second.aclose()
        await first.aclose()
        await resolver.close()
        return raw

    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(asyncio.run, use_from_one_loop()) for _ in range(2)]
            adapters = [future.result(timeout=10) for future in futures]
    finally:
        AdapterRegistry._factories.pop((PortKind.WORK_TRACKING, provider), None)

    assert adapters[0] is not adapters[1]
    assert len({adapter.created_on for adapter in adapters}) == 2
    assert all(adapter.closed_on == adapter.created_on for adapter in adapters)

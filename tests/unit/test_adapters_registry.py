"""AdapterRegistry register/build behavior, secret resolution, and error messages."""

from collections.abc import Iterator

import pytest

import apex.adapters.stubs  # noqa: F401  (registers built-in providers)
from apex.adapters.registry import AdapterRegistry, ConnectionConfig, PortKind
from apex.domain.integrations import SecretValue

TEST_PROVIDER = "registry-test-provider"


@pytest.fixture(autouse=True)
def _clean_test_registrations() -> Iterator[None]:
    yield
    for key in [k for k in AdapterRegistry._factories if k[1] == TEST_PROVIDER]:
        del AdapterRegistry._factories[key]


def _conn(kind: PortKind, provider: str, secret_ref: str | None = None) -> ConnectionConfig:
    return ConnectionConfig(
        id="conn-test", kind=kind, provider=provider, name="Test", secret_ref=secret_ref
    )


async def test_register_and_build_roundtrip() -> None:
    @AdapterRegistry.register(PortKind.WORK_TRACKING, TEST_PROVIDER)
    class FakeAdapter:
        def __init__(
            self, conn: ConnectionConfig | None = None, secret: SecretValue | None = None
        ) -> None:
            self.conn = conn
            self.secret = secret

    adapter = await AdapterRegistry.build(_conn(PortKind.WORK_TRACKING, TEST_PROVIDER))
    assert isinstance(adapter, FakeAdapter)
    assert adapter.conn is not None and adapter.conn.id == "conn-test"
    assert adapter.secret is None


async def test_unknown_provider_raises_helpful_keyerror() -> None:
    with pytest.raises(KeyError) as excinfo:
        await AdapterRegistry.build(_conn(PortKind.WORK_TRACKING, "no-such-provider"))
    message = str(excinfo.value)
    assert "no-such-provider" in message
    assert "work_tracking" in message
    assert "stub" in message  # lists registered providers for the kind


async def test_build_resolves_secret_ref_first(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, SecretValue | None] = {}

    @AdapterRegistry.register(PortKind.LOG_SEARCH, TEST_PROVIDER)
    def factory(conn: ConnectionConfig, secret: SecretValue | None) -> object:
        captured["secret"] = secret
        return object()

    class FakeSecrets:
        async def resolve(self, secret_ref: str) -> SecretValue:
            assert secret_ref == "env:API_TOKEN"
            return SecretValue(value="s3cret")

    await AdapterRegistry.build(
        _conn(PortKind.LOG_SEARCH, TEST_PROVIDER, secret_ref="env:API_TOKEN"), FakeSecrets()
    )
    secret = captured["secret"]
    assert secret is not None and secret.value == "s3cret"


async def test_secret_ref_without_secrets_port_raises() -> None:
    @AdapterRegistry.register(PortKind.LOG_SEARCH, TEST_PROVIDER)
    def factory(conn: ConnectionConfig, secret: SecretValue | None) -> object:
        return object()

    with pytest.raises(ValueError, match="secret_ref"):
        await AdapterRegistry.build(
            _conn(PortKind.LOG_SEARCH, TEST_PROVIDER, secret_ref="env:API_TOKEN")
        )


def test_port_kind_covers_all_nine_kinds() -> None:
    assert {k.value for k in PortKind} == {
        "work_tracking",
        "log_search",
        "observability",
        "documents",
        "cluster_inventory",
        "source_control",
        "execution_engine",
        "artifact_store",
        "secrets",
    }

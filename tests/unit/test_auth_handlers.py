from collections.abc import Iterator
from typing import Any, cast

import pytest
from langgraph_sdk import Auth

import apex.auth.service as auth_service
from apex.auth.handlers import (
    authenticate,
    ensure_thread_scope,
    identity_from_user,
    on_assistants_write,
    on_threads_create,
    on_threads_create_run,
    on_threads_read,
    on_threads_search,
    scope_filter,
    user_payload,
)
from apex.auth.identity import ConsumerIdentity, ConsumerType, Role, ScopeRef


def make_identity(role: Role, scopes: list[ScopeRef] | None = None) -> ConsumerIdentity:
    return ConsumerIdentity(
        consumer_id="c1",
        name="tester",
        consumer_type=ConsumerType.HEADLESS,
        role=role,
        scopes=scopes or [],
    )


class FakeResolver:
    def __init__(self, identity: ConsumerIdentity | None) -> None:
        self.identity = identity
        self.seen_keys: list[str | None] = []

    async def resolve(self, api_key: str | None) -> ConsumerIdentity | None:
        self.seen_keys.append(api_key)
        return self.identity


class DictUser:
    """Mimics the server's user proxy: attribute access + `.get` over the auth dict."""

    def __init__(self, payload: dict[str, Any]) -> None:
        self._payload = payload

    @property
    def identity(self) -> str:
        return self._payload["identity"]

    @property
    def display_name(self) -> str:
        return self._payload["display_name"]

    @property
    def is_authenticated(self) -> bool:
        return True

    @property
    def permissions(self) -> list[str]:
        return []

    def get(self, key: str, default: Any = None) -> Any:
        return self._payload.get(key, default)

    def __getitem__(self, key: str) -> Any:
        return self._payload[key]

    def __contains__(self, key: object) -> bool:
        return key in self._payload

    def __iter__(self) -> Iterator[str]:
        return iter(self._payload)


def make_ctx(
    identity: ConsumerIdentity, resource: str = "threads", action: str = "create"
) -> Auth.types.AuthContext:
    return Auth.types.AuthContext(
        permissions=[],
        # cast: BaseUser's unannotated dunders defeat structural matching in pyright
        user=cast("Auth.types.BaseUser", DictUser(user_payload(identity))),
        resource=resource,  # type: ignore[arg-type]
        action=action,  # type: ignore[arg-type]
    )


# ── authenticate ─────────────────────────────────────────────────────────────


async def test_authenticate_success(monkeypatch: pytest.MonkeyPatch) -> None:
    identity = make_identity(Role.OPERATOR, [ScopeRef(project_id="p1", app_id="a1")])
    resolver = FakeResolver(identity)
    monkeypatch.setattr(auth_service, "default_resolver", resolver)
    user = await authenticate(headers={b"x-api-key": b"good-key"})
    assert resolver.seen_keys == ["good-key"]
    assert user["identity"] == "c1"
    assert user["name"] == "tester"
    assert user["role"] == "operator"
    assert user["consumer_type"] == "headless"
    assert user["scopes"] == [{"project_id": "p1", "app_id": "a1"}]


async def test_authenticate_rejects_unknown_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(auth_service, "default_resolver", FakeResolver(None))
    with pytest.raises(Auth.exceptions.HTTPException) as excinfo:
        await authenticate(headers={b"x-api-key": b"bad-key"})
    assert excinfo.value.status_code == 401


def test_identity_round_trips_through_user_payload() -> None:
    identity = make_identity(Role.ADMIN, [ScopeRef(project_id="p1")])
    assert identity_from_user(user_payload(identity)) == identity


def test_identity_from_attribute_style_user() -> None:
    class User:
        identity = "c9"
        name = "attr-user"
        role = "viewer"
        consumer_type = "dashboard"
        scopes: list[dict[str, Any]] = []

    rebuilt = identity_from_user(User())
    assert rebuilt.consumer_id == "c9"
    assert rebuilt.role is Role.VIEWER
    assert rebuilt.consumer_type is ConsumerType.DASHBOARD


# ── scope helpers ────────────────────────────────────────────────────────────


def test_scope_filter_unscoped_admin_is_none() -> None:
    assert scope_filter(make_identity(Role.ADMIN)) is None


def test_scope_filter_single_project() -> None:
    identity = make_identity(Role.VIEWER, [ScopeRef(project_id="p1")])
    assert scope_filter(identity) == {"project_id": {"$eq": "p1"}}


def test_scope_filter_multiple_projects_uses_or() -> None:
    identity = make_identity(Role.VIEWER, [ScopeRef(project_id="p1"), ScopeRef(project_id="p2")])
    assert scope_filter(identity) == {
        "$or": [{"project_id": {"$eq": "p1"}}, {"project_id": {"$eq": "p2"}}]
    }


def test_scope_filter_no_scopes_non_admin_denies() -> None:
    assert scope_filter(make_identity(Role.VIEWER)) is False


def test_ensure_thread_scope_unscoped_admin_passes_untouched() -> None:
    metadata: dict[str, Any] = {"project_id": "anything"}
    ensure_thread_scope(make_identity(Role.ADMIN), metadata)
    assert metadata == {"project_id": "anything"}


def test_ensure_thread_scope_stamps_single_project() -> None:
    metadata: dict[str, Any] = {}
    ensure_thread_scope(make_identity(Role.OPERATOR, [ScopeRef(project_id="p1")]), metadata)
    assert metadata["project_id"] == "p1"


def test_ensure_thread_scope_in_scope_passes() -> None:
    metadata: dict[str, Any] = {"project_id": "p2"}
    identity = make_identity(Role.OPERATOR, [ScopeRef(project_id="p1"), ScopeRef(project_id="p2")])
    ensure_thread_scope(identity, metadata)
    assert metadata["project_id"] == "p2"


def test_ensure_thread_scope_out_of_scope_403() -> None:
    identity = make_identity(Role.OPERATOR, [ScopeRef(project_id="p1")])
    with pytest.raises(Auth.exceptions.HTTPException) as excinfo:
        ensure_thread_scope(identity, {"project_id": "p9"})
    assert excinfo.value.status_code == 403


def test_ensure_thread_scope_ambiguous_missing_project_403() -> None:
    identity = make_identity(Role.OPERATOR, [ScopeRef(project_id="p1"), ScopeRef(project_id="p2")])
    with pytest.raises(Auth.exceptions.HTTPException) as excinfo:
        ensure_thread_scope(identity, {})
    assert excinfo.value.status_code == 403


# ── resource handlers ────────────────────────────────────────────────────────


async def test_threads_create_requires_operator() -> None:
    identity = make_identity(Role.VIEWER)
    with pytest.raises(Auth.exceptions.HTTPException) as excinfo:
        await on_threads_create(ctx=make_ctx(identity), value={})
    assert excinfo.value.status_code == 403


async def test_threads_create_stamps_scope_and_creator() -> None:
    identity = make_identity(Role.OPERATOR, [ScopeRef(project_id="p1")])
    value: Auth.types.ThreadsCreate = {}
    await on_threads_create(ctx=make_ctx(identity), value=value)
    metadata = value.get("metadata")
    assert metadata is not None
    assert metadata["project_id"] == "p1"
    assert metadata["created_by"] == "c1"


async def test_threads_create_out_of_scope_403() -> None:
    identity = make_identity(Role.OPERATOR, [ScopeRef(project_id="p1")])
    with pytest.raises(Auth.exceptions.HTTPException) as excinfo:
        await on_threads_create(ctx=make_ctx(identity), value={"metadata": {"project_id": "p9"}})
    assert excinfo.value.status_code == 403


async def test_threads_create_unscoped_admin_any_project() -> None:
    value: Auth.types.ThreadsCreate = {"metadata": {"project_id": "p42"}}
    await on_threads_create(ctx=make_ctx(make_identity(Role.ADMIN)), value=value)
    metadata = value.get("metadata")
    assert metadata is not None
    assert metadata["project_id"] == "p42"


async def test_threads_create_run_requires_operator() -> None:
    with pytest.raises(Auth.exceptions.HTTPException) as excinfo:
        await on_threads_create_run(
            ctx=make_ctx(make_identity(Role.VIEWER), action="create_run"), value={}
        )
    assert excinfo.value.status_code == 403


async def test_threads_read_and_search_return_scope_filter() -> None:
    identity = make_identity(Role.VIEWER, [ScopeRef(project_id="p1")])
    expected = {"project_id": {"$eq": "p1"}}
    assert await on_threads_read(ctx=make_ctx(identity, action="read"), value={}) == expected
    assert await on_threads_search(ctx=make_ctx(identity, action="search"), value={}) == expected
    admin = make_identity(Role.ADMIN)
    assert await on_threads_read(ctx=make_ctx(admin, action="read"), value={}) is None
    assert await on_threads_search(ctx=make_ctx(admin, action="search"), value={}) is None


@pytest.mark.parametrize("role", [Role.VIEWER, Role.OPERATOR])
async def test_assistants_write_rejects_non_admin(role: Role) -> None:
    ctx = make_ctx(make_identity(role), resource="assistants", action="create")
    with pytest.raises(Auth.exceptions.HTTPException) as excinfo:
        await on_assistants_write(ctx=ctx, value={})
    assert excinfo.value.status_code == 403


async def test_assistants_write_allows_admin() -> None:
    ctx = make_ctx(make_identity(Role.ADMIN), resource="assistants", action="delete")
    assert await on_assistants_write(ctx=ctx, value={}) is None

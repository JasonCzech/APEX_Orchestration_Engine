import asyncio
from collections.abc import Iterator
from types import SimpleNamespace
from typing import Any, cast

import pytest
from langgraph_sdk import Auth

import apex.auth.service as auth_service
from apex.auth.handlers import (
    authenticate,
    ensure_thread_scope,
    identity_from_user,
    on_anything_else,
    on_assistants_read,
    on_assistants_write,
    on_crons_read,
    on_crons_write,
    on_store_read,
    on_store_write,
    on_threads_create,
    on_threads_create_run,
    on_threads_delete,
    on_threads_read,
    on_threads_search,
    on_threads_update,
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


async def test_authenticate_rejects_unknown_key_and_audits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[Any] = []

    async def capture(event: Any) -> None:
        events.append(event)

    monkeypatch.setattr("apex.auth.handlers.append_audit_event_best_effort", capture)
    monkeypatch.setattr(auth_service, "default_resolver", FakeResolver(None))
    with pytest.raises(Auth.exceptions.HTTPException) as excinfo:
        await authenticate(headers={b"x-api-key": b"bad-key"})
    await asyncio.sleep(0)
    assert excinfo.value.status_code == 401
    assert len(events) == 1
    event = events[0]
    assert event.category == "authz_decision"
    assert event.action == "authenticate"
    assert event.decision == "unauthenticated"
    assert event.reason == "Invalid or missing API key"
    assert event.resource_type == "langgraph"
    assert event.status_code == 401


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


@pytest.mark.parametrize("field,value", [("role", "owner"), ("consumer_type", "service")])
def test_identity_from_user_rejects_unknown_role_or_type(field: str, value: str) -> None:
    payload = user_payload(make_identity(Role.VIEWER))
    payload[field] = value

    with pytest.raises(Auth.exceptions.HTTPException) as excinfo:
        identity_from_user(payload)

    assert excinfo.value.status_code == 401


def test_studio_user_is_admin_in_dev(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("apex.auth.handlers.is_studio_user", lambda _user: True)
    monkeypatch.setattr(
        "apex.auth.handlers.get_settings", lambda: SimpleNamespace(is_locked_down=False)
    )
    identity = identity_from_user(object())
    assert identity.consumer_id == "studio"
    assert identity.role is Role.ADMIN


def test_studio_user_rejected_in_locked_down_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("apex.auth.handlers.is_studio_user", lambda _user: True)
    monkeypatch.setattr(
        "apex.auth.handlers.get_settings", lambda: SimpleNamespace(is_locked_down=True)
    )
    with pytest.raises(Auth.exceptions.HTTPException) as excinfo:
        identity_from_user(object())
    assert excinfo.value.status_code == 401


# ── scope helpers ────────────────────────────────────────────────────────────


def test_scope_filter_unscoped_admin_is_none() -> None:
    assert scope_filter(make_identity(Role.ADMIN)) is None


def test_scope_filter_single_project() -> None:
    identity = make_identity(Role.VIEWER, [ScopeRef(project_id="p1")])
    assert scope_filter(identity) == {"project_id": {"$eq": "p1"}}


def test_scope_filter_single_app_scope() -> None:
    identity = make_identity(Role.VIEWER, [ScopeRef(project_id="p1", app_id="a1")])
    assert scope_filter(identity) == {
        "project_id": {"$eq": "p1"},
        "app_id": {"$eq": "a1"},
    }


def test_scope_filter_project_wide_scope_dominates_app_scope() -> None:
    identity = make_identity(
        Role.VIEWER,
        [ScopeRef(project_id="p1", app_id="a1"), ScopeRef(project_id="p1")],
    )
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


def test_ensure_thread_scope_stamps_single_app_scope() -> None:
    metadata: dict[str, Any] = {}
    ensure_thread_scope(
        make_identity(Role.OPERATOR, [ScopeRef(project_id="p1", app_id="a1")]), metadata
    )
    assert metadata == {"project_id": "p1", "app_id": "a1"}


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


def test_ensure_thread_scope_out_of_scope_app_403() -> None:
    identity = make_identity(Role.OPERATOR, [ScopeRef(project_id="p1", app_id="a1")])
    with pytest.raises(Auth.exceptions.HTTPException) as excinfo:
        ensure_thread_scope(identity, {"project_id": "p1", "app_id": "a9"})
    assert excinfo.value.status_code == 403


def test_ensure_thread_scope_ambiguous_missing_project_403() -> None:
    identity = make_identity(Role.OPERATOR, [ScopeRef(project_id="p1"), ScopeRef(project_id="p2")])
    with pytest.raises(Auth.exceptions.HTTPException) as excinfo:
        ensure_thread_scope(identity, {})
    assert excinfo.value.status_code == 403


def test_ensure_thread_scope_ambiguous_missing_app_403() -> None:
    identity = make_identity(
        Role.OPERATOR,
        [ScopeRef(project_id="p1", app_id="a1"), ScopeRef(project_id="p1", app_id="a2")],
    )
    with pytest.raises(Auth.exceptions.HTTPException) as excinfo:
        ensure_thread_scope(identity, {"project_id": "p1"})
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


async def test_threads_create_stamps_single_app_scope() -> None:
    identity = make_identity(Role.OPERATOR, [ScopeRef(project_id="p1", app_id="a1")])
    value: Auth.types.ThreadsCreate = {}
    await on_threads_create(ctx=make_ctx(identity), value=value)
    metadata = value.get("metadata")
    assert metadata is not None
    assert metadata["project_id"] == "p1"
    assert metadata["app_id"] == "a1"


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


async def test_threads_create_run_rejects_out_of_scope_input_project() -> None:
    identity = make_identity(Role.OPERATOR, [ScopeRef(project_id="p1")])
    value: Any = {
        "assistant_id": "context",
        "thread_id": None,
        "input": {"project_id": "p9"},
    }
    with pytest.raises(Auth.exceptions.HTTPException) as excinfo:
        await on_threads_create_run(ctx=make_ctx(identity, action="create_run"), value=value)
    assert excinfo.value.status_code == 403


async def test_threads_create_run_rejects_out_of_scope_configurable_project() -> None:
    identity = make_identity(Role.OPERATOR, [ScopeRef(project_id="p1")])
    value: Any = {
        "assistant_id": "pipeline",
        "thread_id": None,
        "config": {"configurable": {"project_id": "p9"}},
    }
    with pytest.raises(Auth.exceptions.HTTPException) as excinfo:
        await on_threads_create_run(ctx=make_ctx(identity, action="create_run"), value=value)
    assert excinfo.value.status_code == 403


async def test_threads_create_run_stamps_single_scope_stateless_context() -> None:
    identity = make_identity(Role.OPERATOR, [ScopeRef(project_id="p1")])
    value: Any = {"assistant_id": "context", "thread_id": None, "input": {}}
    result = await on_threads_create_run(ctx=make_ctx(identity, action="create_run"), value=value)
    assert result == {"project_id": {"$eq": "p1"}}
    assert value["input"]["project_id"] == "p1"
    assert value["config"]["configurable"]["project_id"] == "p1"
    assert value["metadata"]["project_id"] == "p1"


async def test_threads_create_run_stamps_single_app_scope_stateless() -> None:
    identity = make_identity(Role.OPERATOR, [ScopeRef(project_id="p1", app_id="a1")])
    value: Any = {"assistant_id": "context", "thread_id": None, "input": {}}
    result = await on_threads_create_run(ctx=make_ctx(identity, action="create_run"), value=value)
    assert result == {"project_id": {"$eq": "p1"}, "app_id": {"$eq": "a1"}}
    assert value["input"] == {"project_id": "p1", "app_id": "a1"}
    assert value["config"]["configurable"] == {"project_id": "p1", "app_id": "a1"}
    assert value["metadata"] == {"project_id": "p1", "app_id": "a1"}


async def test_threads_create_run_rejects_out_of_scope_app() -> None:
    identity = make_identity(Role.OPERATOR, [ScopeRef(project_id="p1", app_id="a1")])
    value: Any = {
        "assistant_id": "context",
        "thread_id": None,
        "input": {"project_id": "p1", "app_id": "a9"},
    }
    with pytest.raises(Auth.exceptions.HTTPException) as excinfo:
        await on_threads_create_run(ctx=make_ctx(identity, action="create_run"), value=value)
    assert excinfo.value.status_code == 403


async def test_threads_create_run_requires_app_for_ambiguous_app_scopes() -> None:
    identity = make_identity(
        Role.OPERATOR,
        [ScopeRef(project_id="p1", app_id="a1"), ScopeRef(project_id="p1", app_id="a2")],
    )
    value: Any = {"assistant_id": "context", "thread_id": None, "input": {}}
    with pytest.raises(Auth.exceptions.HTTPException) as excinfo:
        await on_threads_create_run(ctx=make_ctx(identity, action="create_run"), value=value)
    assert excinfo.value.status_code == 403


async def test_threads_create_run_requires_project_for_ambiguous_stateless_pipeline() -> None:
    identity = make_identity(Role.OPERATOR, [ScopeRef(project_id="p1"), ScopeRef(project_id="p2")])
    value: Any = {"assistant_id": "pipeline", "thread_id": None}
    with pytest.raises(Auth.exceptions.HTTPException) as excinfo:
        await on_threads_create_run(ctx=make_ctx(identity, action="create_run"), value=value)
    assert excinfo.value.status_code == 403


async def test_threads_create_run_stamps_single_scope_stateless_playground() -> None:
    identity = make_identity(Role.OPERATOR, [ScopeRef(project_id="p1")])
    value: Any = {"assistant_id": "playground", "thread_id": None}
    result = await on_threads_create_run(ctx=make_ctx(identity, action="create_run"), value=value)
    assert result == {"project_id": {"$eq": "p1"}}
    assert value["input"]["project_id"] == "p1"
    assert value["config"]["configurable"]["project_id"] == "p1"
    assert value["metadata"]["project_id"] == "p1"


async def test_threads_create_run_requires_project_for_ambiguous_stateless_playground() -> None:
    identity = make_identity(Role.OPERATOR, [ScopeRef(project_id="p1"), ScopeRef(project_id="p2")])
    value: Any = {"assistant_id": "playground", "thread_id": None}
    with pytest.raises(Auth.exceptions.HTTPException) as excinfo:
        await on_threads_create_run(ctx=make_ctx(identity, action="create_run"), value=value)
    assert excinfo.value.status_code == 403


async def test_threads_create_run_stamps_single_scope_stateless_uuid_assistant() -> None:
    identity = make_identity(Role.OPERATOR, [ScopeRef(project_id="p1")])
    value: Any = {"assistant_id": "018faea9-8b68-7df5-94b7-1d5a0d771620", "thread_id": None}
    result = await on_threads_create_run(ctx=make_ctx(identity, action="create_run"), value=value)
    assert result == {"project_id": {"$eq": "p1"}}
    assert value["config"]["configurable"]["project_id"] == "p1"
    assert value["metadata"]["project_id"] == "p1"


async def test_threads_create_run_rejects_conflicting_project_ids() -> None:
    identity = make_identity(Role.OPERATOR, [ScopeRef(project_id="p1"), ScopeRef(project_id="p2")])
    value: Any = {
        "assistant_id": "018faea9-8b68-7df5-94b7-1d5a0d771620",
        "thread_id": None,
        "input": {"project_id": "p1"},
        "config": {"configurable": {"project_id": "p2"}},
    }
    with pytest.raises(Auth.exceptions.HTTPException) as excinfo:
        await on_threads_create_run(ctx=make_ctx(identity, action="create_run"), value=value)
    assert excinfo.value.status_code == 403


async def test_threads_read_and_search_return_scope_filter() -> None:
    identity = make_identity(Role.VIEWER, [ScopeRef(project_id="p1")])
    expected = {"project_id": {"$eq": "p1"}}
    assert await on_threads_read(ctx=make_ctx(identity, action="read"), value={}) == expected
    assert await on_threads_search(ctx=make_ctx(identity, action="search"), value={}) == expected
    admin = make_identity(Role.ADMIN)
    assert await on_threads_read(ctx=make_ctx(admin, action="read"), value={}) is None
    assert await on_threads_search(ctx=make_ctx(admin, action="search"), value={}) is None


@pytest.mark.parametrize("handler", [on_threads_update, on_threads_delete])
async def test_thread_mutations_require_operator(handler: Any) -> None:
    with pytest.raises(Auth.exceptions.HTTPException) as excinfo:
        await handler(ctx=make_ctx(make_identity(Role.VIEWER), action="update"), value={})
    assert excinfo.value.status_code == 403


@pytest.mark.parametrize("handler", [on_threads_update, on_threads_delete])
async def test_thread_mutations_return_scope_filter(handler: Any) -> None:
    identity = make_identity(Role.OPERATOR, [ScopeRef(project_id="p1")])
    assert await handler(ctx=make_ctx(identity, action="update"), value={}) == {
        "project_id": {"$eq": "p1"}
    }


async def test_anything_else_fallback_is_scoped() -> None:
    identity = make_identity(Role.VIEWER, [ScopeRef(project_id="p1")])
    result = await on_anything_else(
        ctx=make_ctx(identity, resource="store", action="get"), value={}
    )
    assert result == {"project_id": {"$eq": "p1"}}


@pytest.mark.parametrize("role", [Role.VIEWER, Role.OPERATOR])
async def test_assistants_write_rejects_non_admin(role: Role) -> None:
    ctx = make_ctx(make_identity(role), resource="assistants", action="create")
    with pytest.raises(Auth.exceptions.HTTPException) as excinfo:
        await on_assistants_write(ctx=ctx, value={})
    assert excinfo.value.status_code == 403


async def test_assistants_write_allows_admin() -> None:
    ctx = make_ctx(make_identity(Role.ADMIN), resource="assistants", action="delete")
    assert await on_assistants_write(ctx=ctx, value={}) is None


async def test_assistants_write_stamps_scoped_admin_metadata() -> None:
    identity = make_identity(Role.ADMIN, [ScopeRef(project_id="p1")])
    value: dict[str, Any] = {}
    ctx = make_ctx(identity, resource="assistants", action="create")

    assert await on_assistants_write(ctx=ctx, value=value) is None

    assert value["metadata"]["project_id"] == "p1"


async def test_assistants_write_stamps_scoped_admin_app_metadata() -> None:
    identity = make_identity(Role.ADMIN, [ScopeRef(project_id="p1", app_id="a1")])
    value: dict[str, Any] = {}
    ctx = make_ctx(identity, resource="assistants", action="create")

    assert await on_assistants_write(ctx=ctx, value=value) is None

    assert value["metadata"] == {"project_id": "p1", "app_id": "a1"}


async def test_assistants_delete_requires_unscoped_admin() -> None:
    identity = make_identity(Role.ADMIN, [ScopeRef(project_id="p1")])
    ctx = make_ctx(identity, resource="assistants", action="delete")

    with pytest.raises(Auth.exceptions.HTTPException) as excinfo:
        await on_assistants_write(ctx=ctx, value={"assistant_id": "a1"})

    assert excinfo.value.status_code == 403


async def test_assistants_read_is_viewer_scoped() -> None:
    identity = make_identity(Role.VIEWER, [ScopeRef(project_id="p1")])
    value: dict[str, Any] = {}
    assert await on_assistants_read(
        ctx=make_ctx(identity, resource="assistants", action="search"), value=value
    ) == {"project_id": {"$eq": "p1"}}
    assert value["metadata"]["project_id"] == "p1"


async def test_assistants_read_rejects_out_of_scope_metadata() -> None:
    identity = make_identity(Role.VIEWER, [ScopeRef(project_id="p1")])
    with pytest.raises(Auth.exceptions.HTTPException) as excinfo:
        await on_assistants_read(
            ctx=make_ctx(identity, resource="assistants", action="search"),
            value={"metadata": {"project_id": "p9"}},
        )
    assert excinfo.value.status_code == 403


async def test_crons_write_requires_operator() -> None:
    with pytest.raises(Auth.exceptions.HTTPException) as excinfo:
        await on_crons_write(
            ctx=make_ctx(make_identity(Role.VIEWER), resource="crons", action="create"), value={}
        )
    assert excinfo.value.status_code == 403


async def test_crons_read_requires_unscoped_admin_for_existing_crons() -> None:
    identity = make_identity(Role.VIEWER, [ScopeRef(project_id="p1")])
    with pytest.raises(Auth.exceptions.HTTPException) as excinfo:
        await on_crons_read(ctx=make_ctx(identity, resource="crons", action="search"), value={})
    assert excinfo.value.status_code == 403


async def test_crons_read_allows_unscoped_admin() -> None:
    assert (
        await on_crons_read(
            ctx=make_ctx(make_identity(Role.ADMIN), resource="crons", action="search"), value={}
        )
        is None
    )


async def test_store_write_requires_operator() -> None:
    with pytest.raises(Auth.exceptions.HTTPException) as excinfo:
        await on_store_write(
            ctx=make_ctx(make_identity(Role.VIEWER), resource="store", action="put"), value={}
        )
    assert excinfo.value.status_code == 403


async def test_store_read_prefixes_single_project_namespace() -> None:
    identity = make_identity(Role.VIEWER, [ScopeRef(project_id="p1")])
    value: dict[str, Any] = {"namespace": ("memories",), "key": "k1"}
    assert (
        await on_store_read(ctx=make_ctx(identity, resource="store", action="get"), value=value)
        is None
    )
    assert value["namespace"] == ("apex", "project", "p1", "memories")


async def test_store_write_accepts_allowed_project_prefixed_namespace() -> None:
    identity = make_identity(Role.OPERATOR, [ScopeRef(project_id="p1"), ScopeRef(project_id="p2")])
    value: dict[str, Any] = {"namespace": ("apex", "project", "p2", "memories"), "key": "k1"}
    assert (
        await on_store_write(ctx=make_ctx(identity, resource="store", action="put"), value=value)
        is None
    )
    assert value["namespace"] == ("apex", "project", "p2", "memories")


async def test_store_write_rejects_out_of_scope_project_namespace() -> None:
    identity = make_identity(Role.OPERATOR, [ScopeRef(project_id="p1")])
    with pytest.raises(Auth.exceptions.HTTPException) as excinfo:
        await on_store_write(
            ctx=make_ctx(identity, resource="store", action="put"),
            value={"namespace": ("apex", "project", "p9", "memories"), "key": "k1"},
        )
    assert excinfo.value.status_code == 403


async def test_store_write_rejects_malformed_project_namespace() -> None:
    identity = make_identity(Role.OPERATOR, [ScopeRef(project_id="p1")])
    with pytest.raises(Auth.exceptions.HTTPException) as excinfo:
        await on_store_write(
            ctx=make_ctx(identity, resource="store", action="put"),
            value={"namespace": ("apex", "project"), "key": "k1"},
        )
    assert excinfo.value.status_code == 403


async def test_store_search_requires_project_namespace_for_multi_project_consumers() -> None:
    identity = make_identity(Role.VIEWER, [ScopeRef(project_id="p1"), ScopeRef(project_id="p2")])
    with pytest.raises(Auth.exceptions.HTTPException) as excinfo:
        await on_store_read(
            ctx=make_ctx(identity, resource="store", action="search"),
            value={"namespace": ("memories",)},
        )
    assert excinfo.value.status_code == 403


async def test_langgraph_role_denial_is_audited(monkeypatch: pytest.MonkeyPatch) -> None:
    events: list[Any] = []

    async def capture(event: Any) -> None:
        events.append(event)

    monkeypatch.setattr("apex.auth.handlers.append_audit_event_best_effort", capture)

    with pytest.raises(Auth.exceptions.HTTPException) as excinfo:
        await on_crons_write(
            ctx=make_ctx(make_identity(Role.VIEWER), resource="crons", action="create"),
            value={},
        )
    await asyncio.sleep(0)

    assert excinfo.value.status_code == 403
    assert len(events) == 1
    event = events[0]
    assert event.category == "authz_decision"
    assert event.action == "crons.create"
    assert event.decision == "denied"
    assert event.reason == "Requires role 'operator' or higher"
    assert event.principal_id == "c1"
    assert event.principal_role == "viewer"
    assert event.principal_scopes == {"scopes": []}
    assert event.resource_type == "langgraph"
    assert event.status_code == 403


async def test_langgraph_scope_denial_is_audited(monkeypatch: pytest.MonkeyPatch) -> None:
    events: list[Any] = []

    async def capture(event: Any) -> None:
        events.append(event)

    monkeypatch.setattr("apex.auth.handlers.append_audit_event_best_effort", capture)
    identity = make_identity(Role.OPERATOR, [ScopeRef(project_id="p1")])

    with pytest.raises(Auth.exceptions.HTTPException) as excinfo:
        await on_threads_create(
            ctx=make_ctx(identity, resource="threads", action="create"),
            value={"metadata": {"project_id": "p9"}},
        )
    await asyncio.sleep(0)

    assert excinfo.value.status_code == 403
    assert len(events) == 1
    event = events[0]
    assert event.action == "threads.create"
    assert event.decision == "denied"
    assert event.reason == "Project 'p9' is outside this consumer's scopes"
    assert event.principal_id == "c1"
    assert event.principal_scopes == {"scopes": [{"project_id": "p1", "app_id": None}]}
    assert event.status_code == 403

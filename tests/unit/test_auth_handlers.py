import asyncio
from collections.abc import Iterator
from types import SimpleNamespace
from typing import Any, cast

import pytest
from fastapi import HTTPException
from langgraph_sdk import Auth
from starlette.requests import Request

import apex.auth.handlers as auth_handlers
import apex.auth.service as auth_service
from apex.app.dependencies import get_current_identity
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
from apex.auth.service import AuthStoreUnavailableError
from apex.services.langgraph_client import (
    LAUNCH_ROOT_FINGERPRINT_METADATA_KEY,
    RERUN_CLAIM_METADATA_KEY,
    RERUN_FINGERPRINT_METADATA_KEY,
)

_DIRECT_RUNS_ALLOWED_METADATA_KEY = "apex_direct_runs_allowed"


@pytest.fixture(autouse=True)
def _catalog_application(monkeypatch: pytest.MonkeyPatch) -> None:
    async def load(app_id: str) -> SimpleNamespace:
        return SimpleNamespace(id=app_id, project_id="p1", archived_at=None)

    monkeypatch.setattr(auth_handlers, "_load_catalog_application", load)


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


class UnavailableResolver:
    async def resolve(self, _api_key: str | None) -> ConsumerIdentity | None:
        try:
            raise RuntimeError("postgresql://admin:raw-secret@database.internal/apex")
        except RuntimeError as exc:
            raise AuthStoreUnavailableError("API key store is unavailable") from exc


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
    identity: ConsumerIdentity,
    resource: str = "threads",
    action: str = "create",
    *,
    trusted_loopback: bool = False,
) -> Auth.types.AuthContext:
    return Auth.types.AuthContext(
        permissions=[],
        # cast: BaseUser's unannotated dunders defeat structural matching in pyright
        user=cast(
            "Auth.types.BaseUser",
            DictUser(user_payload(identity, trusted_loopback=trusted_loopback)),
        ),
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


async def test_authenticate_completes_deferred_stream_admission(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    identity = make_identity(Role.OPERATOR)
    scope = {"type": "http", "path": "/runs/stream"}
    seen: list[dict[str, Any]] = []

    async def mark_authenticated(request_scope: dict[str, Any]) -> None:
        seen.append(request_scope)

    monkeypatch.setattr(auth_service, "default_resolver", FakeResolver(identity))
    monkeypatch.setattr(
        auth_handlers,
        "mark_stream_request_authenticated",
        mark_authenticated,
    )

    await authenticate(
        headers={b"x-api-key": b"good-key"},
        scope=scope,
    )

    assert seen == [scope]


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


async def test_authenticate_detaches_raw_store_failure_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(auth_service, "default_resolver", UnavailableResolver())

    with pytest.raises(Auth.exceptions.HTTPException) as exc_info:
        await authenticate(headers={b"x-api-key": b"some-key"})

    assert exc_info.value.status_code == 503
    assert exc_info.value.__context__ is None
    assert exc_info.value.__cause__ is None
    assert "raw-secret" not in repr(exc_info.value)


async def test_fastapi_identity_detaches_raw_store_failure_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(auth_service, "default_resolver", UnavailableResolver())
    request = Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/v1/system/info",
            "headers": [(b"x-api-key", b"some-key")],
        }
    )

    with pytest.raises(HTTPException) as exc_info:
        await get_current_identity(request)

    assert exc_info.value.status_code == 503
    assert exc_info.value.__context__ is None
    assert exc_info.value.__cause__ is None
    assert "raw-secret" not in repr(exc_info.value)


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
    assert excinfo.value.__context__ is None
    assert excinfo.value.__cause__ is None


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


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("project_id", {"secret": "must-not-leak"}),
        ("project_id", "x" * 256),
        ("app_id", "bad\x00app"),
    ],
)
@pytest.mark.parametrize(
    "identity",
    [
        make_identity(Role.ADMIN),
        make_identity(Role.OPERATOR, [ScopeRef(project_id="p1")]),
    ],
)
def test_ensure_thread_scope_rejects_invalid_identifiers_for_every_identity(
    field: str, value: object, identity: ConsumerIdentity
) -> None:
    with pytest.raises(Auth.exceptions.HTTPException) as excinfo:
        ensure_thread_scope(identity, {field: value})

    assert excinfo.value.status_code == 422
    assert "must-not-leak" not in str(excinfo.value.detail)


@pytest.mark.parametrize(
    "value",
    [
        {"metadata": {"project_id": {"secret": "must-not-leak"}}},
        {"input": {"project_id": "x" * 256}},
        {"config": {"metadata": {"app_id": "bad\x00app"}}},
    ],
)
def test_unscoped_run_scope_rejects_invalid_durable_identifiers(value: Any) -> None:
    with pytest.raises(Auth.exceptions.HTTPException) as excinfo:
        auth_handlers.ensure_run_scope(make_identity(Role.ADMIN), value)

    assert excinfo.value.status_code == 422
    assert "must-not-leak" not in str(excinfo.value.detail)


@pytest.mark.parametrize(
    "value",
    [
        {
            "input": {"project_id": "p1"},
            "config": {"configurable": {"project_id": "p2"}},
        },
        {
            "input": {"project_id": "p1", "app_id": "a1"},
            "metadata": {"app_id": "a2"},
        },
        {"input": {"app_id": "a1"}},
    ],
)
def test_unscoped_run_scope_rejects_ambiguous_tenant_identity(value: Any) -> None:
    with pytest.raises(Auth.exceptions.HTTPException) as excinfo:
        auth_handlers.ensure_run_scope(make_identity(Role.ADMIN), value)

    assert excinfo.value.status_code == 422


def test_unscoped_run_scope_canonicalizes_explicit_tenant_and_filters_thread() -> None:
    value: Any = {
        "input": {"project_id": "p1", "app_id": "a1"},
        "config": {"configurable": {}, "metadata": {}},
        "metadata": {},
    }

    result = auth_handlers.ensure_run_scope(make_identity(Role.ADMIN), value)

    assert result == {"project_id": {"$eq": "p1"}, "app_id": {"$eq": "a1"}}
    assert value["input"] == {"project_id": "p1", "app_id": "a1"}
    assert value["config"]["configurable"] == {"project_id": "p1", "app_id": "a1"}
    assert value["config"]["metadata"] == {"project_id": "p1", "app_id": "a1"}
    assert value["metadata"] == {"project_id": "p1", "app_id": "a1"}


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
    result = await on_threads_create(ctx=make_ctx(identity), value=value)
    metadata = value.get("metadata")
    assert metadata is not None
    assert metadata["project_id"] == "p1"
    assert metadata["created_by"] == "c1"
    assert metadata[_DIRECT_RUNS_ALLOWED_METADATA_KEY] is True
    assert result == {"project_id": {"$eq": "p1"}}


async def test_threads_create_overwrites_forged_creator() -> None:
    value: Auth.types.ThreadsCreate = {"metadata": {"created_by": "forged"}}

    await on_threads_create(ctx=make_ctx(make_identity(Role.ADMIN)), value=value)

    assert value["metadata"]["created_by"] == "c1"
    assert value["metadata"][_DIRECT_RUNS_ALLOWED_METADATA_KEY] is True


async def test_threads_create_overwrites_forged_direct_run_policy() -> None:
    value: Auth.types.ThreadsCreate = {"metadata": {_DIRECT_RUNS_ALLOWED_METADATA_KEY: False}}

    await on_threads_create(ctx=make_ctx(make_identity(Role.ADMIN)), value=value)

    assert value["metadata"][_DIRECT_RUNS_ALLOWED_METADATA_KEY] is True


@pytest.mark.parametrize(
    "field",
    [
        "launch_idempotency_key",
        "launch_idempotency_fingerprint",
        "launch_principal_id",
        LAUNCH_ROOT_FINGERPRINT_METADATA_KEY,
        RERUN_CLAIM_METADATA_KEY,
        RERUN_FINGERPRINT_METADATA_KEY,
    ],
)
async def test_threads_create_rejects_public_launch_idempotency_metadata(field: str) -> None:
    with pytest.raises(Auth.exceptions.HTTPException) as excinfo:
        await on_threads_create(
            ctx=make_ctx(make_identity(Role.ADMIN)),
            value={"metadata": {field: "forged"}},
        )

    assert excinfo.value.status_code == 403
    assert "server-owned" in str(excinfo.value.detail)


async def test_threads_create_allows_trusted_facade_launch_idempotency_metadata() -> None:
    metadata = {
        "launch_idempotency_key": "key",
        "launch_idempotency_fingerprint": "fingerprint",
        "launch_principal_id": "principal",
    }
    value: Auth.types.ThreadsCreate = {"metadata": metadata}

    await on_threads_create(
        ctx=make_ctx(make_identity(Role.ADMIN), trusted_loopback=True),
        value=value,
    )

    assert value["metadata"] == {
        **metadata,
        _DIRECT_RUNS_ALLOWED_METADATA_KEY: False,
        "created_by": "c1",
    }


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


async def test_assistant_write_rejects_invalid_scope_identifier_for_unscoped_admin() -> None:
    value = {"metadata": {"project_id": {"secret": "must-not-leak"}}}

    with pytest.raises(Auth.exceptions.HTTPException) as excinfo:
        await on_assistants_write(
            ctx=make_ctx(make_identity(Role.ADMIN), resource="assistants", action="create"),
            value=value,
        )

    assert excinfo.value.status_code == 422
    assert "must-not-leak" not in str(excinfo.value.detail)


@pytest.mark.parametrize(
    "metadata",
    [
        {"title": "bad\x00title"},
        {"bad\x00key": "value"},
        {"nested": {"value": "bad\x00value"}},
    ],
)
async def test_threads_create_rejects_nul_metadata(metadata: dict[str, Any]) -> None:
    with pytest.raises(Auth.exceptions.HTTPException) as excinfo:
        await on_threads_create(
            ctx=make_ctx(make_identity(Role.ADMIN)),
            value={"metadata": metadata},
        )

    assert excinfo.value.status_code == 422
    assert "U+0000" in str(excinfo.value.detail)


@pytest.mark.parametrize("operation", ["create", "update"])
async def test_public_thread_writes_reject_credential_metadata_without_reflection(
    operation: str,
) -> None:
    value = {"metadata": {"connectionString": "thread-durable-secret-canary"}}

    with pytest.raises(Auth.exceptions.HTTPException) as excinfo:
        if operation == "create":
            await on_threads_create(
                ctx=make_ctx(make_identity(Role.ADMIN), action="create"),
                value=cast(Any, value),
            )
        else:
            await on_threads_update(
                ctx=make_ctx(make_identity(Role.ADMIN), action="update"),
                value=cast(Any, value),
            )

    assert excinfo.value.status_code == 422
    assert "thread-durable-secret-canary" not in str(excinfo.value.detail)
    assert "credential material" in str(excinfo.value.detail)


async def test_threads_create_run_requires_operator() -> None:
    with pytest.raises(Auth.exceptions.HTTPException) as excinfo:
        await on_threads_create_run(
            ctx=make_ctx(make_identity(Role.VIEWER), action="create_run"), value={}
        )
    assert excinfo.value.status_code == 403


@pytest.mark.parametrize(
    "value",
    [
        {"assistant_id": "pipeline", "metadata": {"note": "bad\x00note"}},
        {"assistant_id": "pipeline", "kwargs": {"input": {"title": "bad\x00title"}}},
        {
            "assistant_id": "pipeline",
            "kwargs": {"config": {"metadata": {"bad\x00key": "value"}}},
        },
    ],
)
async def test_threads_create_run_rejects_nul_persisted_json(value: Any) -> None:
    with pytest.raises(Auth.exceptions.HTTPException) as excinfo:
        await on_threads_create_run(
            ctx=make_ctx(make_identity(Role.ADMIN), action="create_run"),
            value=value,
        )

    assert excinfo.value.status_code == 422
    assert "U+0000" in str(excinfo.value.detail)


@pytest.mark.parametrize(
    "value",
    [
        {"assistant_id": "pipeline", "metadata": {"privateKey": "run-secret-canary"}},
        {"assistant_id": "pipeline", "kwargs": {"input": {"cookie": "run-secret-canary"}}},
        {
            "assistant_id": "pipeline",
            "kwargs": {
                "config": {
                    "configurable": {"database_uri": "run-secret-canary"},
                }
            },
        },
    ],
)
async def test_public_run_writes_reject_credential_material_without_reflection(
    value: Any,
) -> None:
    with pytest.raises(Auth.exceptions.HTTPException) as excinfo:
        await on_threads_create_run(
            ctx=make_ctx(make_identity(Role.ADMIN), action="create_run"),
            value=value,
        )

    assert excinfo.value.status_code == 422
    assert "run-secret-canary" not in str(excinfo.value.detail)
    assert "credential material" in str(excinfo.value.detail)


@pytest.mark.parametrize(
    "value",
    [
        {
            "assistant_id": "pipeline",
            "thread_id": "thread-1",
            "metadata": {"note": "Authorization: Bearer trusted-secret-canary"},
        },
        {
            "assistant_id": "pipeline",
            "thread_id": "thread-1",
            "kwargs": {"input": {"request": "password=trusted-secret-canary"}},
        },
        {
            "assistant_id": "pipeline",
            "thread_id": "thread-1",
            "kwargs": {
                "config": {
                    "configurable": {
                        "langsmith-metadata": {"database_uri": "trusted-secret-canary"}
                    }
                }
            },
        },
        {
            "assistant_id": "pipeline",
            "thread_id": "thread-1",
            "kwargs": {
                "command": {
                    "resume": {
                        "interrupt-1": {
                            "action": "revise",
                            "instructions": "private_key=trusted-secret-canary",
                        }
                    }
                }
            },
        },
        {
            "assistant_id": "pipeline",
            "thread_id": "thread-1",
            "kwargs": {"webhook": "https://user:trusted-secret-canary@example.test"},
        },
    ],
)
async def test_trusted_loopback_run_writes_still_reject_credential_material(
    value: Any,
) -> None:
    with pytest.raises(Auth.exceptions.HTTPException) as excinfo:
        await on_threads_create_run(
            ctx=make_ctx(
                make_identity(Role.OPERATOR),
                action="create_run",
                trusted_loopback=True,
            ),
            value=value,
        )

    assert excinfo.value.status_code == 422
    assert "trusted-secret-canary" not in str(excinfo.value.detail)
    assert "credential material" in str(excinfo.value.detail)


async def test_runtime_auth_user_extra_fields_cannot_evade_credential_scan() -> None:
    identity = make_identity(Role.OPERATOR)
    forged_user = {
        **user_payload(identity, trusted_loopback=True),
        "apiKey": "trusted-auth-user-secret-canary",
    }
    value: Any = {
        "assistant_id": "pipeline",
        "thread_id": "thread-1",
        "kwargs": {
            "input": {},
            "config": {"configurable": {"langgraph_auth_user": forged_user}},
        },
    }

    with pytest.raises(Auth.exceptions.HTTPException) as excinfo:
        await on_threads_create_run(
            ctx=make_ctx(identity, action="create_run", trusted_loopback=True),
            value=value,
        )

    assert excinfo.value.status_code == 422
    assert "trusted-auth-user-secret-canary" not in str(excinfo.value.detail)
    assert "authenticated consumer" in str(excinfo.value.detail)


async def test_pinned_proxy_user_no_arg_model_dump_is_accepted_when_canonical() -> None:
    # An unscoped admin is the only identity that can launch without a
    # project selector; keep this regression focused on ProxyUser shape.
    identity = make_identity(Role.ADMIN)
    canonical = user_payload(identity, trusted_loopback=True)

    class PinnedProxyUserShape:
        def get(self, key: str, default: Any = None) -> Any:
            return canonical.get(key, default)

        def model_dump(self) -> dict[str, Any]:
            # LangGraph 0.10 ProxyUser accepts no kwargs and adds this one field.
            return {**canonical, "is_authenticated": True}

    value: Any = {
        "assistant_id": "pipeline",
        "thread_id": "thread-1",
        "kwargs": {
            "input": {},
            "config": {"configurable": {"langgraph_auth_user": PinnedProxyUserShape()}},
        },
    }

    await on_threads_create_run(
        ctx=make_ctx(identity, action="create_run", trusted_loopback=True),
        value=value,
    )


@pytest.mark.parametrize("handler", [on_threads_create, on_threads_update])
async def test_trusted_loopback_thread_metadata_still_rejects_credentials(
    handler: Any,
) -> None:
    with pytest.raises(Auth.exceptions.HTTPException) as excinfo:
        await handler(
            ctx=make_ctx(
                make_identity(Role.OPERATOR),
                action="update" if handler is on_threads_update else "create",
                trusted_loopback=True,
            ),
            value={"metadata": {"note": "password=trusted-thread-secret-canary"}},
        )

    assert excinfo.value.status_code == 422
    assert "trusted-thread-secret-canary" not in str(excinfo.value.detail)


async def test_threads_create_run_rejects_out_of_scope_input_project() -> None:
    identity = make_identity(Role.OPERATOR, [ScopeRef(project_id="p1")])
    value: Any = {
        "assistant_id": "context",
        "thread_id": None,
        "input": {"subject": "bounded context", "project_id": "p9"},
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
    value: Any = {
        "assistant_id": "context",
        "thread_id": None,
        "input": {"subject": "bounded context"},
    }
    result = await on_threads_create_run(ctx=make_ctx(identity, action="create_run"), value=value)
    assert result == {
        "project_id": {"$eq": "p1"},
        _DIRECT_RUNS_ALLOWED_METADATA_KEY: {"$eq": True},
    }
    assert value["input"]["project_id"] == "p1"
    assert value["config"]["configurable"]["project_id"] == "p1"
    assert value["metadata"]["project_id"] == "p1"


async def test_threads_create_run_stamps_single_scope_on_existing_thread() -> None:
    identity = make_identity(Role.OPERATOR, [ScopeRef(project_id="p1")])
    value: Any = {"assistant_id": "pipeline", "thread_id": "thread-1"}

    result = await on_threads_create_run(ctx=make_ctx(identity, action="create_run"), value=value)

    assert result == {
        "project_id": {"$eq": "p1"},
        _DIRECT_RUNS_ALLOWED_METADATA_KEY: {"$eq": True},
    }
    assert value["input"]["project_id"] == "p1"
    assert value["config"]["configurable"]["project_id"] == "p1"
    assert value["metadata"]["project_id"] == "p1"


async def test_threads_create_run_stamps_single_app_scope_on_existing_thread() -> None:
    identity = make_identity(Role.OPERATOR, [ScopeRef(project_id="p1", app_id="a1")])
    value: Any = {"assistant_id": "pipeline", "thread_id": "thread-1"}

    result = await on_threads_create_run(ctx=make_ctx(identity, action="create_run"), value=value)

    assert result == {
        "project_id": {"$eq": "p1"},
        "app_id": {"$eq": "a1"},
        _DIRECT_RUNS_ALLOWED_METADATA_KEY: {"$eq": True},
    }
    assert value["config"]["configurable"] == {"project_id": "p1", "app_id": "a1"}
    assert value["metadata"] == {
        _DIRECT_RUNS_ALLOWED_METADATA_KEY: True,
        "created_by": "c1",
        "project_id": "p1",
        "app_id": "a1",
    }


async def test_threads_create_run_rejects_ambiguous_existing_thread_scope() -> None:
    identity = make_identity(Role.OPERATOR, [ScopeRef(project_id="p1"), ScopeRef(project_id="p2")])
    value: Any = {"assistant_id": "pipeline", "thread_id": "thread-1"}

    with pytest.raises(Auth.exceptions.HTTPException) as excinfo:
        await on_threads_create_run(ctx=make_ctx(identity, action="create_run"), value=value)

    assert excinfo.value.status_code == 403
    assert "project_id is required for scoped runs" in str(excinfo.value.detail)


async def test_threads_create_run_returns_exact_selected_thread_filter() -> None:
    identity = make_identity(Role.OPERATOR, [ScopeRef(project_id="p1"), ScopeRef(project_id="p2")])
    value: Any = {
        "assistant_id": "pipeline",
        "thread_id": "thread-2",
        "config": {"configurable": {"project_id": "p2"}},
    }

    result = await on_threads_create_run(ctx=make_ctx(identity, action="create_run"), value=value)

    assert result == {
        "project_id": {"$eq": "p2"},
        _DIRECT_RUNS_ALLOWED_METADATA_KEY: {"$eq": True},
    }
    assert value["input"]["project_id"] == "p2"
    assert value["metadata"]["project_id"] == "p2"


async def test_threads_create_run_stamps_single_app_scope_stateless() -> None:
    identity = make_identity(Role.OPERATOR, [ScopeRef(project_id="p1", app_id="a1")])
    value: Any = {
        "assistant_id": "context",
        "thread_id": None,
        "input": {"subject": "bounded context"},
    }
    result = await on_threads_create_run(ctx=make_ctx(identity, action="create_run"), value=value)
    assert result == {
        "project_id": {"$eq": "p1"},
        "app_id": {"$eq": "a1"},
        _DIRECT_RUNS_ALLOWED_METADATA_KEY: {"$eq": True},
    }
    assert value["input"] == {
        "subject": "bounded context",
        "project_id": "p1",
        "app_id": "a1",
    }
    assert value["config"]["configurable"] == {"project_id": "p1", "app_id": "a1"}
    assert value["metadata"] == {
        _DIRECT_RUNS_ALLOWED_METADATA_KEY: True,
        "created_by": "c1",
        "project_id": "p1",
        "app_id": "a1",
    }


async def test_threads_create_run_rejects_out_of_scope_app() -> None:
    identity = make_identity(Role.OPERATOR, [ScopeRef(project_id="p1", app_id="a1")])
    value: Any = {
        "assistant_id": "context",
        "thread_id": None,
        "input": {
            "subject": "bounded context",
            "project_id": "p1",
            "app_id": "a9",
        },
    }
    with pytest.raises(Auth.exceptions.HTTPException) as excinfo:
        await on_threads_create_run(ctx=make_ctx(identity, action="create_run"), value=value)
    assert excinfo.value.status_code == 403


async def test_threads_create_run_requires_app_for_ambiguous_app_scopes() -> None:
    identity = make_identity(
        Role.OPERATOR,
        [ScopeRef(project_id="p1", app_id="a1"), ScopeRef(project_id="p1", app_id="a2")],
    )
    value: Any = {
        "assistant_id": "context",
        "thread_id": None,
        "input": {"subject": "bounded context"},
    }
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
    assert result == {
        "project_id": {"$eq": "p1"},
        _DIRECT_RUNS_ALLOWED_METADATA_KEY: {"$eq": True},
    }
    assert value["input"]["project_id"] == "p1"
    assert value["config"]["configurable"]["project_id"] == "p1"
    assert value["metadata"]["project_id"] == "p1"


async def test_prompt_test_thread_and_run_shapes_pass_real_scope_handlers() -> None:
    identity = make_identity(Role.OPERATOR, [ScopeRef(project_id="p1", app_id="a1")])
    scratch_metadata = {
        "purpose": "prompt_test",
        "prompt_id": "prompt-1",
        "requested_by": "tester",
        "project_id": "p1",
        "app_id": "a1",
    }
    thread_value: Auth.types.ThreadsCreate = {"metadata": dict(scratch_metadata)}

    thread_filter = await on_threads_create(ctx=make_ctx(identity), value=thread_value)

    assert thread_filter == {"project_id": {"$eq": "p1"}, "app_id": {"$eq": "a1"}}
    run_value: Any = {
        "assistant_id": "playground",
        "thread_id": "thread-1",
        "input": {
            "prompt": {"system": "bounded", "user": ""},
            "sample_input": {},
            "project_id": "p1",
            "app_id": "a1",
        },
        "metadata": dict(scratch_metadata),
        "config": {"configurable": {"project_id": "p1", "app_id": "a1"}},
    }

    run_filter = await on_threads_create_run(
        ctx=make_ctx(identity, action="create_run"),
        value=run_value,
    )

    assert run_filter == {
        "project_id": {"$eq": "p1"},
        "app_id": {"$eq": "a1"},
        _DIRECT_RUNS_ALLOWED_METADATA_KEY: {"$eq": True},
    }
    assert run_value["input"]["project_id"] == "p1"
    assert run_value["input"]["app_id"] == "a1"


async def test_threads_create_run_requires_project_for_ambiguous_stateless_playground() -> None:
    identity = make_identity(Role.OPERATOR, [ScopeRef(project_id="p1"), ScopeRef(project_id="p2")])
    value: Any = {"assistant_id": "playground", "thread_id": None}
    with pytest.raises(Auth.exceptions.HTTPException) as excinfo:
        await on_threads_create_run(ctx=make_ctx(identity, action="create_run"), value=value)
    assert excinfo.value.status_code == 403


async def test_threads_create_run_rejects_context_provider_fanout() -> None:
    identity = make_identity(Role.OPERATOR, [ScopeRef(project_id="p1")])
    value: Any = {
        "assistant_id": "context",
        "thread_id": None,
        "input": {
            "subject": "incident",
            "work_item_keys": [f"ITEM-{index}" for index in range(51)],
            "work_tracking_connection_id": "conn-work-a",
        },
    }
    with pytest.raises(Auth.exceptions.HTTPException) as excinfo:
        await on_threads_create_run(ctx=make_ctx(identity, action="create_run"), value=value)
    assert excinfo.value.status_code == 422
    assert "work_item_keys exceeds" in str(excinfo.value.detail)


async def test_threads_create_run_rejects_context_keys_without_connection_affinity() -> None:
    identity = make_identity(Role.OPERATOR, [ScopeRef(project_id="p1")])
    value: Any = {
        "assistant_id": "context",
        "thread_id": None,
        "input": {
            "subject": "incident",
            "work_item_keys": ["ITEM-1"],
        },
    }

    with pytest.raises(Auth.exceptions.HTTPException) as excinfo:
        await on_threads_create_run(ctx=make_ctx(identity, action="create_run"), value=value)

    assert excinfo.value.status_code == 422
    assert "context run input is invalid" in str(excinfo.value.detail)
    assert "work_tracking_connection_id" not in str(excinfo.value.detail)


async def test_threads_create_run_rejects_deep_playground_sample() -> None:
    identity = make_identity(Role.OPERATOR, [ScopeRef(project_id="p1")])
    nested: Any = "value"
    for _ in range(13):
        nested = {"nested": nested}
    value: Any = {
        "assistant_id": "playground",
        "thread_id": None,
        "input": {"sample_input": {"value": nested}},
    }
    with pytest.raises(Auth.exceptions.HTTPException) as excinfo:
        await on_threads_create_run(ctx=make_ctx(identity, action="create_run"), value=value)
    assert excinfo.value.status_code == 422
    assert "nesting exceeds" in str(excinfo.value.detail)


async def test_threads_create_run_stamps_single_scope_stateless_uuid_assistant() -> None:
    identity = make_identity(Role.OPERATOR, [ScopeRef(project_id="p1")])
    value: Any = {"assistant_id": "018faea9-8b68-7df5-94b7-1d5a0d771620", "thread_id": None}
    result = await on_threads_create_run(ctx=make_ctx(identity, action="create_run"), value=value)
    assert result == {
        "project_id": {"$eq": "p1"},
        _DIRECT_RUNS_ALLOWED_METADATA_KEY: {"$eq": True},
    }
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


async def test_unscoped_threads_create_run_validates_catalog_app_project_pair(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def mismatched_app(_app_id: str) -> SimpleNamespace:
        return SimpleNamespace(id="a1", project_id="p2", archived_at=None)

    monkeypatch.setattr(auth_handlers, "_load_catalog_application", mismatched_app)
    value: Any = {
        "assistant_id": "018faea9-8b68-7df5-94b7-1d5a0d771620",
        "thread_id": "existing-thread",
        "input": {"project_id": "p1", "app_id": "a1"},
    }

    with pytest.raises(Auth.exceptions.HTTPException) as excinfo:
        await on_threads_create_run(
            ctx=make_ctx(make_identity(Role.ADMIN), action="create_run"),
            value=value,
        )

    assert excinfo.value.status_code == 403
    assert excinfo.value.detail == "app_id is not authorized for project_id"


async def test_threads_create_run_scopes_realistic_nested_kwargs_in_place() -> None:
    identity = make_identity(Role.OPERATOR, [ScopeRef(project_id="p1", app_id="a1")])
    runtime_user = DictUser(user_payload(identity))
    config: dict[str, Any] = {
        "configurable": {
            "connections": {"execution_engine": "engine-a"},
            "__after_seconds__": 0,
            "__request_start_time_ms__": 1_751_234_567_890.5,
            "langgraph_auth_user": runtime_user,
            "langgraph_auth_user_id": "c1",
            "langgraph_auth_permissions": ["threads:read", "threads:write"],
            "langgraph_request_id": "request-1",
            "langsmith-trace": "trace-1",
            "langsmith-metadata": {"source": "test"},
        },
        "metadata": {},
    }
    value: Any = {
        "assistant_id": "pipeline",
        "thread_id": "thread-1",
        "metadata": {},
        "kwargs": {"input": {}, "config": config},
    }

    result = await on_threads_create_run(ctx=make_ctx(identity, action="create_run"), value=value)

    assert result == {
        "project_id": {"$eq": "p1"},
        "app_id": {"$eq": "a1"},
        _DIRECT_RUNS_ALLOWED_METADATA_KEY: {"$eq": True},
    }
    assert value["kwargs"]["input"] == {"project_id": "p1", "app_id": "a1"}
    assert value["metadata"] == {
        _DIRECT_RUNS_ALLOWED_METADATA_KEY: True,
        "created_by": "c1",
        "project_id": "p1",
        "app_id": "a1",
    }
    assert config["metadata"] == {"project_id": "p1", "app_id": "a1"}
    assert config["configurable"] == {
        "connections": {"execution_engine": "engine-a"},
        "__after_seconds__": 0,
        "__request_start_time_ms__": 1_751_234_567_890.5,
        "langgraph_auth_user": runtime_user,
        "langgraph_auth_user_id": "c1",
        "langgraph_auth_permissions": ["threads:read", "threads:write"],
        "langgraph_request_id": "request-1",
        "langsmith-trace": "trace-1",
        "langsmith-metadata": {"source": "test"},
        "project_id": "p1",
        "app_id": "a1",
    }
    assert value["kwargs"]["durability"] == "sync"
    assert value["kwargs"]["checkpoint_during"] is True


@pytest.mark.parametrize(
    ("configurable", "expected_detail"),
    [
        ({"checkpoint_id": "checkpoint-1"}, "1 unsupported field(s)"),
        ({"__after_seconds__": 1}, "__after_seconds__ must be 0"),
        ({"__after_seconds__": "0"}, "__after_seconds__ must be an integer"),
        (
            {"__request_start_time_ms__": -1},
            "must be a finite non-negative number",
        ),
        (
            {"langgraph_auth_user_id": "another-consumer"},
            "does not match the authenticated consumer",
        ),
        (
            {"langgraph_auth_permissions": "threads:read"},
            "must be a list of strings",
        ),
        ({"langgraph_request_id": 17}, "must be a string"),
        ({"langgraph_request_id": "bad\x00request"}, "must not contain U+0000"),
        ({"__dd_trace_headers__": []}, "must be an object"),
        (
            {"__dd_trace_headers__": {f"trace-{index}": "ok" for index in range(65)}},
            "at most 32 entries",
        ),
        ({"__pregel_node_finished": "not-callable"}, "must be a runtime callback"),
        ({"langsmith-metadata": []}, "must be a JSON object"),
        (
            {
                "langgraph_auth_user": {
                    **user_payload(make_identity(Role.OPERATOR)),
                    "identity": "another-consumer",
                }
            },
            "does not match the authenticated consumer",
        ),
        ({"langsmith-tags": ["tag"] * 129}, "at most 128 entries"),
    ],
)
async def test_threads_create_run_rejects_invalid_runtime_configurable(
    configurable: dict[str, Any], expected_detail: str
) -> None:
    identity = make_identity(Role.OPERATOR)
    value = cast(
        Any,
        {
            "assistant_id": "pipeline",
            "thread_id": "thread-1",
            "kwargs": {"input": {}, "config": {"configurable": configurable}},
        },
    )

    with pytest.raises(Auth.exceptions.HTTPException) as excinfo:
        await on_threads_create_run(ctx=make_ctx(identity, action="create_run"), value=value)

    assert excinfo.value.status_code == 422
    assert expected_detail in str(excinfo.value.detail)
    assert excinfo.value.__context__ is None
    assert excinfo.value.__cause__ is None


async def test_threads_create_run_accepts_bounded_runtime_trace_metadata() -> None:
    value = cast(
        Any,
        {
            "assistant_id": "pipeline",
            "thread_id": "thread-1",
            "kwargs": {
                "input": {},
                "config": {
                    "configurable": {
                        "__request_start_time_ms__": 0,
                        "__dd_trace_headers__": {
                            "x-datadog-trace-id": "123",
                            "x-datadog-parent-id": "456",
                        },
                        "langsmith-metadata": {"source": "bounded-test"},
                        "langsmith-tags": ["unit", "auth-boundary"],
                    }
                },
            },
        },
    )

    await on_threads_create_run(
        ctx=make_ctx(
            make_identity(Role.OPERATOR, [ScopeRef(project_id="p1")]),
            action="create_run",
        ),
        value=value,
    )

    runtime = value["kwargs"]["config"]["configurable"]
    assert runtime["__dd_trace_headers__"]["x-datadog-trace-id"] == "123"
    assert runtime["langsmith-tags"] == ["unit", "auth-boundary"]


async def test_trusted_loopback_run_bounds_delayed_dispatch_window() -> None:
    identity = make_identity(Role.OPERATOR, [ScopeRef(project_id="p1")])

    accepted = cast(
        Any,
        {
            "assistant_id": "pipeline",
            "thread_id": "thread-1",
            "kwargs": {
                "input": {},
                "config": {"configurable": {"__after_seconds__": 86_400}},
            },
        },
    )
    await on_threads_create_run(
        ctx=make_ctx(identity, action="create_run", trusted_loopback=True),
        value=accepted,
    )

    rejected = cast(
        Any,
        {
            "assistant_id": "pipeline",
            "thread_id": "thread-1",
            "kwargs": {
                "input": {},
                "config": {"configurable": {"__after_seconds__": 86_401}},
            },
        },
    )
    with pytest.raises(Auth.exceptions.HTTPException) as excinfo:
        await on_threads_create_run(
            ctx=make_ctx(identity, action="create_run", trusted_loopback=True),
            value=rejected,
        )

    assert excinfo.value.status_code == 422
    assert "between 0 and 86400" in str(excinfo.value.detail)


@pytest.mark.parametrize(
    ("proxy", "message"),
    [
        (
            type(
                "ExplodingDescriptor",
                (),
                {
                    "__getattribute__": lambda self, name: (
                        (_ for _ in ()).throw(RuntimeError("secret"))
                        if name == "model_dump"
                        else object.__getattribute__(self, name)
                    )
                },
            )(),
            "not inspectable",
        ),
        (
            type(
                "ExplodingRenderer",
                (),
                {"model_dump": lambda self, **_kwargs: (_ for _ in ()).throw(RuntimeError("x"))},
            )(),
            "not inspectable",
        ),
        (type("ScalarRenderer", (), {"model_dump": lambda self, **_kwargs: 17})(), "render"),
    ],
)
def test_runtime_auth_user_rejects_uninspectable_proxy_objects(
    proxy: object,
    message: str,
) -> None:
    with pytest.raises(ValueError, match=message) as exc_info:
        auth_handlers._runtime_auth_user_payload(proxy)

    assert exc_info.value.__context__ is None
    assert exc_info.value.__cause__ is None
    assert "secret" not in repr(exc_info.value)


def test_runtime_auth_user_rejects_unenumerable_or_unreadable_proxies() -> None:
    class Unenumerable:
        def __iter__(self) -> Iterator[str]:
            raise RuntimeError("secret")

    class Unreadable:
        def __iter__(self) -> Iterator[str]:
            yield "identity"

        def __getitem__(self, _key: str) -> object:
            raise RuntimeError("secret")

    with pytest.raises(ValueError, match="not enumerable") as unenumerable_error:
        auth_handlers._runtime_auth_user_payload(Unenumerable())
    with pytest.raises(ValueError, match="not enumerable") as unreadable_error:
        auth_handlers._runtime_auth_user_payload(Unreadable())
    with pytest.raises(ValueError, match="invalid fields"):
        auth_handlers._runtime_auth_user_payload(iter(str(index) for index in range(33)))

    for error in (unenumerable_error.value, unreadable_error.value):
        assert error.__context__ is None
        assert error.__cause__ is None
        assert "secret" not in repr(error)


@pytest.mark.parametrize(
    "configurable",
    [
        {"langsmith-metadata": {"database_uri": "trace-secret-canary"}},
        {"langsmith-tags": ["Authorization: Bearer trace-secret-canary"]},
        {"langsmith-project": "password=trace-secret-canary"},
        {"langsmith-trace": "passphrase=trace-secret-canary"},
        {"__langsmith_project__": "connection_string=trace-secret-canary"},
        {"__langsmith_example_id__": "dsn=trace-secret-canary"},
        {"__otel_traceparent__": "private_key=trace-secret-canary"},
        {"__otel_tracestate__": "cookie=trace-secret-canary"},
        {"__dd_trace_headers__": {"authorization": "Bearer trace-secret-canary"}},
        {"langgraph_request_id": "signing_key=trace-secret-canary"},
    ],
)
async def test_public_run_rejects_credential_material_in_runtime_trace_fields(
    configurable: dict[str, Any],
) -> None:
    value = cast(
        Any,
        {
            "assistant_id": "pipeline",
            "thread_id": "thread-1",
            "kwargs": {"input": {}, "config": {"configurable": configurable}},
        },
    )

    with pytest.raises(Auth.exceptions.HTTPException) as excinfo:
        await on_threads_create_run(
            ctx=make_ctx(make_identity(Role.OPERATOR), action="create_run"),
            value=value,
        )

    assert excinfo.value.status_code == 422
    assert "trace-secret-canary" not in str(excinfo.value.detail)
    assert "credential material" in str(excinfo.value.detail)


async def test_threads_create_run_counts_unknown_runtime_fields_without_reflecting_names() -> None:
    canary = "CANARY_NATIVE_RUN_CONFIG_SENSITIVE"
    configurable = {
        **{f"unknown_{index}": index for index in range(256)},
        canary: True,
    }
    value = cast(
        Any,
        {
            "assistant_id": "pipeline",
            "thread_id": "thread-1",
            "kwargs": {"input": {}, "config": {"configurable": configurable}},
        },
    )

    with pytest.raises(Auth.exceptions.HTTPException) as excinfo:
        await on_threads_create_run(
            ctx=make_ctx(make_identity(Role.OPERATOR), action="create_run"),
            value=value,
        )

    detail = str(excinfo.value.detail)
    assert excinfo.value.status_code == 422
    assert "257 unsupported field(s)" in detail
    assert canary not in detail
    assert len(detail) <= 1_048


async def test_threads_create_run_oversized_runtime_key_is_not_reflected() -> None:
    canary = "CANARY_OVERSIZED_NATIVE_RUN_CONFIG_SECRET"
    oversized_key = f"{canary}_{'x' * 4_096}"
    value = cast(
        Any,
        {
            "assistant_id": "pipeline",
            "thread_id": "thread-1",
            "kwargs": {
                "input": {},
                "config": {"configurable": {oversized_key: True}},
            },
        },
    )

    with pytest.raises(Auth.exceptions.HTTPException) as excinfo:
        await on_threads_create_run(
            ctx=make_ctx(make_identity(Role.OPERATOR), action="create_run"),
            value=value,
        )

    detail = str(excinfo.value.detail)
    assert excinfo.value.status_code == 422
    assert "keys must be 1-255 characters" in detail
    assert canary not in detail
    assert len(detail) <= 1_048


async def test_threads_create_run_pydantic_error_does_not_reflect_nested_unknown_key() -> None:
    canary = "CANARY_NATIVE_RUN_VALIDATION_SENSITIVE"
    value = cast(
        Any,
        {
            "assistant_id": "pipeline",
            "thread_id": "thread-1",
            "kwargs": {
                "input": {},
                "config": {"configurable": {"limits": {canary: 1}}},
            },
        },
    )

    with pytest.raises(Auth.exceptions.HTTPException) as excinfo:
        await on_threads_create_run(
            ctx=make_ctx(make_identity(Role.OPERATOR), action="create_run"),
            value=value,
        )

    detail = str(excinfo.value.detail)
    assert excinfo.value.status_code == 422
    assert "extra_forbidden" in detail
    assert canary not in detail
    assert len(detail) <= 1_048


async def test_threads_create_run_enforces_write_ahead_lifecycle_controls() -> None:
    identity = make_identity(Role.OPERATOR, [ScopeRef(project_id="p1")])
    value = cast(
        Any,
        {
            "assistant_id": "pipeline",
            "thread_id": "thread-1",
            "kwargs": {
                "input": {},
                "config": {"configurable": {"__after_seconds__": 0}},
                "durability": "exit",
            },
        },
    )

    await on_threads_create_run(ctx=make_ctx(identity, action="create_run"), value=value)

    assert value["kwargs"]["durability"] == "sync"
    assert value["kwargs"]["checkpoint_during"] is True


async def test_threads_create_run_rejects_persistent_stateless_runtime_run() -> None:
    identity = make_identity(Role.OPERATOR, [ScopeRef(project_id="p1")])
    value = cast(
        Any,
        {
            "assistant_id": "playground",
            "thread_id": None,
            "kwargs": {
                "input": {"prompt": "test"},
                "config": {"configurable": {"__after_seconds__": 0}},
                "temporary": False,
            },
        },
    )

    with pytest.raises(Auth.exceptions.HTTPException) as excinfo:
        await on_threads_create_run(ctx=make_ctx(identity, action="create_run"), value=value)

    assert excinfo.value.status_code == 422
    assert "stateless direct runs must be temporary" in str(excinfo.value.detail)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("checkpoint_during", False),
        ("interrupt_before", "*"),
        ("interrupt_after", ["engine_start"]),
    ],
)
async def test_threads_create_run_rejects_unsafe_lifecycle_controls(field: str, value: Any) -> None:
    identity = make_identity(Role.OPERATOR)
    run_kwargs = {
        "input": {},
        "config": {"configurable": {"__after_seconds__": 0}},
        field: value,
    }

    with pytest.raises(Auth.exceptions.HTTPException) as excinfo:
        await on_threads_create_run(
            ctx=make_ctx(identity, action="create_run"),
            value=cast(
                Any,
                {
                    "assistant_id": "pipeline",
                    "thread_id": "thread-1",
                    "kwargs": run_kwargs,
                },
            ),
        )

    assert excinfo.value.status_code == 422


async def test_threads_create_run_overwrites_forged_creator() -> None:
    value: Any = {
        "assistant_id": "pipeline",
        "thread_id": None,
        "metadata": {"created_by": "forged"},
    }

    await on_threads_create_run(
        ctx=make_ctx(make_identity(Role.ADMIN), action="create_run"),
        value=value,
    )

    assert value["metadata"]["created_by"] == "c1"


@pytest.mark.parametrize(
    "field",
    [
        "launch_idempotency_key",
        "launch_idempotency_fingerprint",
        "launch_principal_id",
        LAUNCH_ROOT_FINGERPRINT_METADATA_KEY,
        RERUN_CLAIM_METADATA_KEY,
        RERUN_FINGERPRINT_METADATA_KEY,
    ],
)
async def test_threads_create_run_rejects_public_launch_idempotency_metadata(
    field: str,
) -> None:
    value: Any = {
        "assistant_id": "pipeline",
        "thread_id": "thread-1",
        "metadata": {field: "forged"},
    }

    with pytest.raises(Auth.exceptions.HTTPException) as excinfo:
        await on_threads_create_run(
            ctx=make_ctx(make_identity(Role.ADMIN), action="create_run"),
            value=value,
        )

    assert excinfo.value.status_code == 403
    assert "server-owned" in str(excinfo.value.detail)


async def test_threads_create_run_allows_trusted_facade_launch_idempotency_metadata() -> None:
    metadata = {
        "launch_idempotency_key": "key",
        "launch_idempotency_fingerprint": "fingerprint",
        "launch_principal_id": "principal",
    }
    value: Any = {
        "assistant_id": "pipeline",
        "thread_id": "thread-1",
        "metadata": metadata,
    }

    await on_threads_create_run(
        ctx=make_ctx(
            make_identity(Role.ADMIN),
            action="create_run",
            trusted_loopback=True,
        ),
        value=value,
    )

    assert value["metadata"] == {
        **metadata,
        _DIRECT_RUNS_ALLOWED_METADATA_KEY: False,
        "created_by": "c1",
    }


async def test_threads_create_run_allows_trusted_launch_root_fingerprint() -> None:
    value: Any = {
        "assistant_id": "pipeline",
        "thread_id": "thread-1",
        "metadata": {LAUNCH_ROOT_FINGERPRINT_METADATA_KEY: "fingerprint"},
    }

    result = await on_threads_create_run(
        ctx=make_ctx(
            make_identity(Role.ADMIN),
            action="create_run",
            trusted_loopback=True,
        ),
        value=value,
    )

    assert result is None
    assert value["metadata"] == {
        LAUNCH_ROOT_FINGERPRINT_METADATA_KEY: "fingerprint",
        _DIRECT_RUNS_ALLOWED_METADATA_KEY: True,
        "created_by": "c1",
    }


async def test_public_direct_run_filter_excludes_facade_launch_threads() -> None:
    launch_thread: Auth.types.ThreadsCreate = {
        "metadata": {
            "launch_idempotency_key": "key",
            "launch_idempotency_fingerprint": "fingerprint",
            "launch_principal_id": "principal",
        }
    }
    await on_threads_create(
        ctx=make_ctx(make_identity(Role.ADMIN), trusted_loopback=True),
        value=launch_thread,
    )

    public_run: Any = {"assistant_id": "pipeline", "thread_id": "launch-thread"}
    public_filter = await on_threads_create_run(
        ctx=make_ctx(make_identity(Role.ADMIN), action="create_run"),
        value=public_run,
    )
    trusted_run: Any = {
        "assistant_id": "pipeline",
        "thread_id": "launch-thread",
        "metadata": {
            "launch_idempotency_key": "key",
            "launch_idempotency_fingerprint": "fingerprint",
            "launch_principal_id": "principal",
        },
    }
    trusted_filter = await on_threads_create_run(
        ctx=make_ctx(
            make_identity(Role.ADMIN),
            action="create_run",
            trusted_loopback=True,
        ),
        value=trusted_run,
    )

    assert launch_thread["metadata"][_DIRECT_RUNS_ALLOWED_METADATA_KEY] is False
    assert public_filter == {
        _DIRECT_RUNS_ALLOWED_METADATA_KEY: {"$eq": True},
    }
    assert public_run["metadata"][_DIRECT_RUNS_ALLOWED_METADATA_KEY] is True
    assert trusted_filter is None
    assert trusted_run["metadata"][_DIRECT_RUNS_ALLOWED_METADATA_KEY] is False


async def test_threads_create_run_rejects_nested_forged_scope() -> None:
    identity = make_identity(Role.OPERATOR, [ScopeRef(project_id="p1", app_id="a1")])
    value: Any = {
        "assistant_id": "pipeline",
        "thread_id": "thread-1",
        "kwargs": {
            "input": {},
            "config": {
                "configurable": {
                    "project_id": "p2",
                    "app_id": "a2",
                    "connections": {"execution_engine": "engine-sibling"},
                }
            },
        },
    }

    with pytest.raises(Auth.exceptions.HTTPException) as excinfo:
        await on_threads_create_run(ctx=make_ctx(identity, action="create_run"), value=value)

    assert excinfo.value.status_code == 403


async def test_threads_create_run_resolves_nested_environment_target(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[tuple[str, str | None, str | None]] = []

    async def resolve(
        environment_id: str, project_id: str | None, app_id: str | None
    ) -> tuple[str, int, str]:
        seen.append((environment_id, project_id, app_id))
        return "https://approved.example.test", 7, "a1"

    monkeypatch.setattr("apex.auth.handlers._load_run_environment_target", resolve)
    identity = make_identity(Role.OPERATOR, [ScopeRef(project_id="p1", app_id="a1")])
    value: Any = {
        "assistant_id": "pipeline",
        "kwargs": {
            "config": {"configurable": {"environment_id": "env-1"}},
        },
    }

    await on_threads_create_run(ctx=make_ctx(identity, action="create_run"), value=value)

    assert seen == [("env-1", "p1", "a1")]
    assert value["kwargs"]["config"]["configurable"]["environment_target"] == (
        "https://approved.example.test"
    )
    assert value["kwargs"]["config"]["configurable"]["environment_target_version"] == 7


async def test_environment_target_stamps_authoritative_app_for_project_wide_caller(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def resolve(
        environment_id: str, project_id: str | None, app_id: str | None
    ) -> tuple[str, int, str]:
        assert (environment_id, project_id, app_id) == ("env-1", "p1", None)
        return "https://approved.example.test", 7, "app-a"

    monkeypatch.setattr("apex.auth.handlers._load_run_environment_target", resolve)
    identity = make_identity(Role.OPERATOR, [ScopeRef(project_id="p1")])
    value: Any = {
        "assistant_id": "pipeline",
        "kwargs": {
            "input": {},
            "config": {"configurable": {"environment_id": "env-1"}},
        },
    }

    scope = await on_threads_create_run(
        ctx=make_ctx(identity, action="create_run", trusted_loopback=True),
        value=value,
    )

    assert scope == {
        "project_id": {"$eq": "p1"},
        "app_id": {"$eq": "app-a"},
    }
    assert value["kwargs"]["input"]["app_id"] == "app-a"
    assert value["kwargs"]["config"]["configurable"]["app_id"] == "app-a"
    assert value["kwargs"]["config"]["metadata"]["app_id"] == "app-a"
    assert value["metadata"]["app_id"] == "app-a"


async def test_unscoped_environment_run_filters_existing_thread_to_authoritative_app(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def resolve(
        environment_id: str, project_id: str | None, app_id: str | None
    ) -> tuple[str, int, str]:
        assert (environment_id, project_id, app_id) == ("env-1", "p1", None)
        return "https://approved.example.test", 7, "app-a"

    monkeypatch.setattr("apex.auth.handlers._load_run_environment_target", resolve)
    value: Any = {
        "assistant_id": "pipeline",
        "thread_id": "existing-thread",
        "kwargs": {
            "input": {},
            "config": {
                "configurable": {
                    "project_id": "p1",
                    "environment_id": "env-1",
                }
            },
        },
    }

    scope = await on_threads_create_run(
        ctx=make_ctx(make_identity(Role.ADMIN), action="create_run"),
        value=value,
    )

    assert scope == {
        "project_id": {"$eq": "p1"},
        "app_id": {"$eq": "app-a"},
        _DIRECT_RUNS_ALLOWED_METADATA_KEY: {"$eq": True},
    }
    assert value["kwargs"]["input"]["app_id"] == "app-a"
    assert value["metadata"]["app_id"] == "app-a"


async def test_environment_target_rejects_injected_conflicting_app_ownership(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def resolve(
        _environment_id: str, _project_id: str | None, _app_id: str | None
    ) -> tuple[str, int, str]:
        return "https://approved.example.test", 7, "app-b"

    monkeypatch.setattr("apex.auth.handlers._load_run_environment_target", resolve)
    identity = make_identity(Role.OPERATOR, [ScopeRef(project_id="p1", app_id="app-a")])
    value: Any = {
        "assistant_id": "pipeline",
        "kwargs": {"config": {"configurable": {"environment_id": "env-1"}}},
    }

    with pytest.raises(Auth.exceptions.HTTPException) as excinfo:
        await on_threads_create_run(ctx=make_ctx(identity, action="create_run"), value=value)

    assert excinfo.value.status_code == 404


async def test_environment_target_not_found_detaches_lookup_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret = "environment-lookup-secret-canary"

    async def resolve(
        _environment_id: str, _project_id: str | None, _app_id: str | None
    ) -> tuple[str, int, str]:
        raise LookupError(secret)

    monkeypatch.setattr("apex.auth.handlers._load_run_environment_target", resolve)
    identity = make_identity(Role.OPERATOR, [ScopeRef(project_id="p1")])
    value: Any = {
        "assistant_id": "pipeline",
        "kwargs": {"config": {"configurable": {"environment_id": "env-1"}}},
    }

    with pytest.raises(Auth.exceptions.HTTPException) as exc_info:
        await on_threads_create_run(ctx=make_ctx(identity, action="create_run"), value=value)

    assert exc_info.value.status_code == 404
    assert exc_info.value.__context__ is None
    assert exc_info.value.__cause__ is None
    assert secret not in repr(exc_info.value)


async def test_threads_create_run_rejects_nested_direct_environment_url() -> None:
    identity = make_identity(Role.OPERATOR, [ScopeRef(project_id="p1")])
    value: Any = {
        "assistant_id": "pipeline",
        "kwargs": {
            "config": {
                "configurable": {
                    "load_test": {"target_environment": "http://169.254.169.254/latest/meta-data"}
                }
            }
        },
    }

    with pytest.raises(Auth.exceptions.HTTPException) as excinfo:
        await on_threads_create_run(ctx=make_ctx(identity, action="create_run"), value=value)

    assert excinfo.value.status_code == 403


async def test_threads_create_run_rejects_disallowed_model_at_auth_boundary() -> None:
    identity = make_identity(Role.OPERATOR, [ScopeRef(project_id="p1")])
    value: Any = {
        "assistant_id": "pipeline",
        "kwargs": {
            "input": {},
            "config": {
                "configurable": {"model_by_phase": {"reporting": "unapproved-expensive-model"}}
            },
        },
    }

    with pytest.raises(Auth.exceptions.HTTPException) as excinfo:
        await on_threads_create_run(ctx=make_ctx(identity, action="create_run"), value=value)

    assert excinfo.value.status_code == 422
    assert "value_error" in str(excinfo.value.detail)
    assert "unapproved-expensive-model" not in str(excinfo.value.detail)


async def test_threads_create_run_rejects_oversized_direct_context_input() -> None:
    identity = make_identity(Role.OPERATOR, [ScopeRef(project_id="p1")])
    value: Any = {
        "assistant_id": "pipeline",
        "kwargs": {
            "input": {
                "context_packets": [
                    {"id": f"p-{index}", "source": "sdk", "title": "packet"} for index in range(33)
                ]
            }
        },
    }

    with pytest.raises(Auth.exceptions.HTTPException) as excinfo:
        await on_threads_create_run(ctx=make_ctx(identity, action="create_run"), value=value)

    assert excinfo.value.status_code == 422
    assert "context_packets exceeds" in str(excinfo.value.detail)


async def test_threads_create_run_rejects_forged_internal_pipeline_state() -> None:
    identity = make_identity(Role.OPERATOR, [ScopeRef(project_id="p1")])
    value: Any = {
        "assistant_id": "pipeline",
        "thread_id": "thread-1",
        "kwargs": {
            "input": {
                "phase_results": {
                    "execution": {
                        "status": "succeeded",
                        "test_summary": {"passed": True},
                    }
                },
                "prompt_reviews": {"execution": {"action": "approve"}},
            }
        },
    }

    with pytest.raises(Auth.exceptions.HTTPException) as excinfo:
        await on_threads_create_run(ctx=make_ctx(identity, action="create_run"), value=value)

    assert excinfo.value.status_code == 422
    assert "phase_results" in str(excinfo.value.detail)
    assert "prompt_reviews" in str(excinfo.value.detail)


async def test_threads_create_run_rejects_caller_owned_stateless_thread_id() -> None:
    identity = make_identity(Role.OPERATOR, [ScopeRef(project_id="p1")])
    value: Any = {
        "assistant_id": "pipeline",
        "thread_id": None,
        "config": {"configurable": {"thread_id": "forged-thread"}},
    }

    with pytest.raises(Auth.exceptions.HTTPException) as excinfo:
        await on_threads_create_run(ctx=make_ctx(identity, action="create_run"), value=value)

    assert excinfo.value.status_code == 422
    assert "thread_id is server-owned" in str(excinfo.value.detail)


@pytest.mark.parametrize(
    "control",
    [
        {"action": "interrupt"},
        {"action": "rollback"},
        {"multitask_strategy": "interrupt"},
        {"multitask_strategy": "rollback"},
        {"multitask_strategy": "enqueue"},
    ],
)
async def test_direct_run_create_rejects_destructive_inflight_controls(
    control: dict[str, str],
) -> None:
    identity = make_identity(Role.ADMIN)
    value: Any = {
        "assistant_id": "pipeline",
        "thread_id": "thread-with-live-engine",
        "kwargs": {
            "input": {"title": "unsafe replacement", "request": "replace it"},
            "config": {"configurable": {}},
        },
        **control,
    }

    with pytest.raises(Auth.exceptions.HTTPException) as excinfo:
        await on_threads_create_run(ctx=make_ctx(identity, action="create_run"), value=value)

    assert excinfo.value.status_code == 422
    expected = "must be 'reject'" if "multitask_strategy" in control else "interrupt or roll back"
    assert expected in str(excinfo.value.detail)


async def test_direct_run_create_rejects_webhook_ssrf_target() -> None:
    value: Any = {
        "assistant_id": "pipeline",
        "thread_id": "thread-1",
        "webhook": "http://10.0.0.4/internal-hook",
        "kwargs": {
            "input": {
                "title": "unsafe webhook",
                "request": "run it",
                "project_id": "p1",
            },
            "config": {"configurable": {"project_id": "p1"}},
        },
    }

    with pytest.raises(Auth.exceptions.HTTPException) as excinfo:
        await on_threads_create_run(
            ctx=make_ctx(
                make_identity(Role.OPERATOR, [ScopeRef(project_id="p1")]),
                action="create_run",
            ),
            value=value,
        )

    assert excinfo.value.status_code == 422
    assert "webhooks are disabled" in str(excinfo.value.detail)


@pytest.mark.parametrize(
    ("container", "control"),
    [
        ("top", {"after_seconds": -1}),
        ("top", {"after_seconds": 0.5}),
        ("top", {"after_seconds": 10**100}),
        ("nested", {"after_seconds": 1}),
        ("top", {"feedback_keys": ["quality"]}),
        ("nested", {"feedback_keys": [f"key-{index}" for index in range(1000)]}),
    ],
)
async def test_direct_run_create_rejects_delayed_and_feedback_controls(
    container: str,
    control: dict[str, Any],
) -> None:
    value: Any = {
        "assistant_id": "pipeline",
        "thread_id": "thread-1",
        "kwargs": {
            "input": {"title": "bounded run", "request": "run it"},
            "config": {"configurable": {}},
        },
    }
    if container == "nested":
        value["kwargs"].update(control)
    else:
        value.update(control)

    with pytest.raises(Auth.exceptions.HTTPException) as excinfo:
        await on_threads_create_run(
            ctx=make_ctx(make_identity(Role.ADMIN), action="create_run"),
            value=value,
        )

    assert excinfo.value.status_code == 422
    assert "after_seconds" in str(excinfo.value.detail) or "feedback_keys" in str(
        excinfo.value.detail
    )


@pytest.mark.parametrize(
    ("container", "control"),
    [
        ("top", {"after_seconds": 0}),
        ("nested", {"feedback_keys": []}),
    ],
)
async def test_direct_run_create_allows_inert_scheduling_controls(
    container: str,
    control: dict[str, Any],
) -> None:
    value: Any = {
        "assistant_id": "pipeline",
        "thread_id": "thread-1",
        "kwargs": {
            "input": {"title": "bounded run", "request": "run it"},
            "config": {"configurable": {}},
        },
    }
    if container == "nested":
        value["kwargs"].update(control)
    else:
        value.update(control)

    assert await on_threads_create_run(
        ctx=make_ctx(make_identity(Role.ADMIN), action="create_run"),
        value=value,
    ) == {_DIRECT_RUNS_ALLOWED_METADATA_KEY: {"$eq": True}}


@pytest.mark.parametrize("selector", ["script_refs", "test_id", "test_instance_id"])
async def test_direct_run_create_rejects_provider_workload_selectors(selector: str) -> None:
    identity = make_identity(Role.OPERATOR, [ScopeRef(project_id="p1")])
    value: Any = {
        "assistant_id": "pipeline",
        "thread_id": "thread-1",
        "kwargs": {
            "input": {"title": "unsafe", "request": "run it"},
            "config": {
                "configurable": {
                    "project_id": "p1",
                    "load_test": {selector: [] if selector == "script_refs" else 42},
                }
            },
        },
    }

    with pytest.raises(Auth.exceptions.HTTPException) as excinfo:
        await on_threads_create_run(ctx=make_ctx(identity, action="create_run"), value=value)

    assert excinfo.value.status_code == 422
    assert "provider workload selectors" in str(excinfo.value.detail)


async def test_threads_read_and_search_return_scope_filter() -> None:
    identity = make_identity(Role.VIEWER, [ScopeRef(project_id="p1")])
    expected = {"project_id": {"$eq": "p1"}}
    assert await on_threads_read(ctx=make_ctx(identity, action="read"), value={}) == expected
    search: dict[str, Any] = {}
    assert (
        await on_threads_search(ctx=make_ctx(identity, action="search"), value=cast(Any, search))
        == expected
    )
    assert "select" not in search
    admin = make_identity(Role.ADMIN)
    assert await on_threads_read(ctx=make_ctx(admin, action="read"), value={}) is None
    assert await on_threads_search(ctx=make_ctx(admin, action="search"), value={}) is None


async def test_threads_count_auth_shape_allows_runtime_zero_limit_sentinel() -> None:
    value = cast(
        Any,
        Auth.types.ThreadsSearch(metadata={}, values={}, limit=0, offset=0),
    )

    assert (
        await on_threads_search(
            ctx=make_ctx(make_identity(Role.ADMIN), action="search"),
            value=value,
        )
        is None
    )


async def test_trusted_loopback_thread_search_preserves_bounded_projection_extract() -> None:
    value: dict[str, Any] = {
        "limit": 100,
        "offset": 0,
        "select": ["thread_id", "metadata", "status"],
        "extract": {"title": "values.title"},
    }

    await on_threads_search(
        ctx=make_ctx(
            make_identity(Role.ADMIN),
            action="search",
            trusted_loopback=True,
        ),
        value=cast(Any, value),
    )

    assert value["select"] == ["thread_id", "metadata", "status"]
    assert value["extract"] == {"title": "values.title"}


@pytest.mark.parametrize("handler", [on_threads_update, on_threads_delete])
async def test_thread_mutations_require_operator(handler: Any) -> None:
    with pytest.raises(Auth.exceptions.HTTPException) as excinfo:
        await handler(ctx=make_ctx(make_identity(Role.VIEWER), action="update"), value={})
    assert excinfo.value.status_code == 403


async def test_thread_update_returns_scope_filter() -> None:
    identity = make_identity(Role.OPERATOR, [ScopeRef(project_id="p1")])
    assert await on_threads_update(
        ctx=make_ctx(identity, action="update"), value={"metadata": {}}
    ) == {"project_id": {"$eq": "p1"}}


@pytest.mark.parametrize("run_action", ["interrupt", "rollback"])
async def test_native_run_cancel_is_denied_without_facade_capability(run_action: str) -> None:
    identity = make_identity(Role.OPERATOR, [ScopeRef(project_id="p1")])
    with pytest.raises(Auth.exceptions.HTTPException) as excinfo:
        await on_threads_update(
            ctx=make_ctx(identity, action="update"),
            value=cast(
                "Auth.types.ThreadsUpdate",
                {
                    "thread_id": "thread-with-live-engine",
                    "action": run_action,
                    "metadata": {"run_ids": ["run-1"]},
                },
            ),
        )
    assert excinfo.value.status_code == 403
    assert "/v1/pipelines/{thread_id}/abort" in str(excinfo.value.detail)


async def test_facade_capability_allows_scoped_run_cancel_after_cleanup() -> None:
    identity = make_identity(Role.OPERATOR, [ScopeRef(project_id="p1")])
    result = await on_threads_update(
        ctx=make_ctx(identity, action="update", trusted_loopback=True),
        value=cast(
            "Auth.types.ThreadsUpdate",
            {
                "thread_id": "thread-with-killed-engine",
                "action": "interrupt",
                "metadata": {"run_ids": ["run-1"]},
            },
        ),
    )
    assert result == {"project_id": {"$eq": "p1"}}


@pytest.mark.parametrize("role", [Role.OPERATOR, Role.ADMIN])
async def test_native_update_state_bare_auth_shape_is_denied(role: Role) -> None:
    identity = make_identity(role, [ScopeRef(project_id="p1")])
    # LangGraph update_state(values, as_node=...) and update_state(values) both
    # reach auth with this exact bare payload; neither values nor as_node is
    # available to the handler for field-level validation.
    value = Auth.types.ThreadsUpdate(thread_id=cast(Any, "thread-with-checkpoint"))
    assert set(value) == {"thread_id"}

    with pytest.raises(Auth.exceptions.HTTPException) as excinfo:
        await on_threads_update(ctx=make_ctx(identity, action="update"), value=value)

    assert excinfo.value.status_code == 403
    assert "Native thread state updates are disabled" in str(excinfo.value.detail)


async def test_facade_capability_allows_validated_update_state() -> None:
    identity = make_identity(Role.OPERATOR, [ScopeRef(project_id="p1", app_id="a1")])
    value = Auth.types.ThreadsUpdate(thread_id=cast(Any, "thread-with-checkpoint"))

    assert await on_threads_update(
        ctx=make_ctx(identity, action="update", trusted_loopback=True),
        value=value,
    ) == {"project_id": {"$eq": "p1"}, "app_id": {"$eq": "a1"}}


async def test_native_thread_delete_is_denied() -> None:
    identity = make_identity(Role.OPERATOR, [ScopeRef(project_id="p1")])
    with pytest.raises(Auth.exceptions.HTTPException) as excinfo:
        await on_threads_delete(ctx=make_ctx(identity, action="delete"), value={})
    assert excinfo.value.status_code == 403
    assert "external engine cleanup handle" in str(excinfo.value.detail)


@pytest.mark.parametrize(
    "metadata",
    [
        {"project_id": "p2"},
        {"project_id": "p1", "app_id": "a2"},
        {"project_id": "p1", "app_id": None},
        {"created_by": "forged"},
        {"graph_id": "context"},
        {"launch_idempotency_fingerprint": "forged"},
        {LAUNCH_ROOT_FINGERPRINT_METADATA_KEY: "forged"},
        {RERUN_CLAIM_METADATA_KEY: "forged"},
        {RERUN_FINGERPRINT_METADATA_KEY: "forged"},
        {_DIRECT_RUNS_ALLOWED_METADATA_KEY: True},
    ],
)
async def test_thread_update_rejects_scoped_ownership_mutation(
    metadata: dict[str, Any],
) -> None:
    identity = make_identity(Role.OPERATOR, [ScopeRef(project_id="p1", app_id="a1")])
    with pytest.raises(Auth.exceptions.HTTPException) as excinfo:
        await on_threads_update(
            ctx=make_ctx(identity, action="update"), value={"metadata": metadata}
        )
    assert excinfo.value.status_code == 403


async def test_thread_update_rejects_global_admin_attribution_mutation() -> None:
    with pytest.raises(Auth.exceptions.HTTPException) as excinfo:
        await on_threads_update(
            ctx=make_ctx(make_identity(Role.ADMIN), action="update"),
            value={"metadata": {"created_by": "forged"}},
        )

    assert excinfo.value.status_code == 403


async def test_thread_update_allows_scoped_non_ownership_metadata() -> None:
    identity = make_identity(Role.OPERATOR, [ScopeRef(project_id="p1", app_id="a1")])
    assert await on_threads_update(
        ctx=make_ctx(identity, action="update"), value={"metadata": {"title": "renamed"}}
    ) == {"project_id": {"$eq": "p1"}, "app_id": {"$eq": "a1"}}


async def test_thread_update_rejects_nul_non_ownership_metadata() -> None:
    identity = make_identity(Role.OPERATOR, [ScopeRef(project_id="p1", app_id="a1")])
    with pytest.raises(Auth.exceptions.HTTPException) as excinfo:
        await on_threads_update(
            ctx=make_ctx(identity, action="update"),
            value={"metadata": {"title": "bad\x00title"}},
        )

    assert excinfo.value.status_code == 422
    assert "U+0000" in str(excinfo.value.detail)


@pytest.mark.parametrize("role", [Role.VIEWER, Role.OPERATOR, Role.ADMIN])
async def test_anything_else_fallback_denies_non_global_admin(role: Role) -> None:
    identity = make_identity(role, [ScopeRef(project_id="p1")])
    with pytest.raises(Auth.exceptions.HTTPException) as excinfo:
        await on_anything_else(
            ctx=make_ctx(identity, resource="future-resource", action="future-action"), value={}
        )
    assert excinfo.value.status_code == 403


async def test_anything_else_fallback_allows_unscoped_admin() -> None:
    identity = make_identity(Role.ADMIN)
    assert (
        await on_anything_else(
            ctx=make_ctx(identity, resource="future-resource", action="future-action"), value={}
        )
        is None
    )


@pytest.mark.parametrize("role", [Role.VIEWER, Role.OPERATOR])
async def test_assistants_write_rejects_non_admin(role: Role) -> None:
    ctx = make_ctx(make_identity(role), resource="assistants", action="create")
    with pytest.raises(Auth.exceptions.HTTPException) as excinfo:
        await on_assistants_write(ctx=ctx, value={})
    assert excinfo.value.status_code == 403


async def test_assistants_write_allows_admin() -> None:
    ctx = make_ctx(make_identity(Role.ADMIN), resource="assistants", action="delete")
    assert await on_assistants_write(ctx=ctx, value={}) is None


@pytest.mark.parametrize(
    "value",
    [
        {"name": "bad\x00name"},
        {"config": {"tag": "bad\x00tag"}},
        {"context": {"bad\x00key": "value"}},
        {"metadata": {"note": "bad\x00note"}},
    ],
)
async def test_assistants_write_rejects_nul_persisted_values(value: dict[str, Any]) -> None:
    ctx = make_ctx(make_identity(Role.ADMIN), resource="assistants", action="create")
    with pytest.raises(Auth.exceptions.HTTPException) as excinfo:
        await on_assistants_write(ctx=ctx, value=value)

    assert excinfo.value.status_code == 422
    assert "U+0000" in str(excinfo.value.detail)


@pytest.mark.parametrize(
    "value",
    [
        {"config": {"configurable": {"api_key": "assistant-secret-canary"}}},
        {"context": {"nested": {"password": "assistant-secret-canary"}}},
        {"metadata": {"note": "Authorization: Bearer assistant-secret-canary"}},
        {
            "config": {
                "endpoint": ("https://provider.test/run?X-Amz-Signature=assistant-secret-canary")
            }
        },
        {
            "config": {
                "configurable": {
                    "cookie": "assistant-secret-canary",
                    "set-cookie": "assistant-secret-canary",
                    "passphrase": "assistant-secret-canary",
                    "private_key": "assistant-secret-canary",
                    "privateKey": "assistant-secret-canary",
                    "signing_key": "assistant-secret-canary",
                    "encryption_key": "assistant-secret-canary",
                    "connection_string": "assistant-secret-canary",
                    "connectionString": "assistant-secret-canary",
                    "database_url": "assistant-secret-canary",
                    "database_uri": "assistant-secret-canary",
                    "dsn": "assistant-secret-canary",
                    "stripeApiKey": "assistant-secret-canary",
                    "serviceAccountPrivateKey": "assistant-secret-canary",
                    "databasePassword": "assistant-secret-canary",
                    "oauthRefreshToken": "assistant-secret-canary",
                    "sessionCookie": "assistant-secret-canary",
                    "cookieJar": "assistant-secret-canary",
                }
            }
        },
    ],
)
async def test_assistants_write_rejects_credential_material_without_reflection(
    value: dict[str, Any],
) -> None:
    ctx = make_ctx(make_identity(Role.ADMIN), resource="assistants", action="create")

    with pytest.raises(Auth.exceptions.HTTPException) as excinfo:
        await on_assistants_write(ctx=ctx, value=value)

    assert excinfo.value.status_code == 422
    assert "assistant-secret-canary" not in str(excinfo.value.detail)
    assert "credential material" in str(excinfo.value.detail)


async def test_assistants_write_rejects_scoped_admin() -> None:
    identity = make_identity(Role.ADMIN, [ScopeRef(project_id="p1")])
    value: dict[str, Any] = {}
    ctx = make_ctx(identity, resource="assistants", action="create")

    with pytest.raises(Auth.exceptions.HTTPException) as excinfo:
        await on_assistants_write(ctx=ctx, value=value)
    assert excinfo.value.status_code == 403


async def test_assistants_write_rejects_app_scoped_admin() -> None:
    identity = make_identity(Role.ADMIN, [ScopeRef(project_id="p1", app_id="a1")])
    value: dict[str, Any] = {}
    ctx = make_ctx(identity, resource="assistants", action="create")

    with pytest.raises(Auth.exceptions.HTTPException) as excinfo:
        await on_assistants_write(ctx=ctx, value=value)
    assert excinfo.value.status_code == 403


async def test_assistants_update_rejects_scoped_admin() -> None:
    identity = make_identity(Role.ADMIN, [ScopeRef(project_id="p1", app_id="a1")])
    value: dict[str, Any] = {"assistant_id": "sibling-id", "metadata": {}}
    ctx = make_ctx(identity, resource="assistants", action="update")

    with pytest.raises(Auth.exceptions.HTTPException) as excinfo:
        await on_assistants_write(ctx=ctx, value=value)
    assert excinfo.value.status_code == 403


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
    assert "metadata" not in value


async def test_assistants_read_supports_multi_project_identity_without_selector() -> None:
    identity = make_identity(
        Role.VIEWER,
        [ScopeRef(project_id="p1"), ScopeRef(project_id="p2")],
    )

    assert await on_assistants_read(
        ctx=make_ctx(identity, resource="assistants", action="search"), value={}
    ) == {"$or": [{"project_id": {"$eq": "p1"}}, {"project_id": {"$eq": "p2"}}]}


async def test_assistants_count_auth_shape_allows_runtime_zero_limit_sentinel() -> None:
    value = cast(
        Any,
        Auth.types.AssistantsSearch(graph_id="pipeline", metadata={}, limit=0, offset=0),
    )

    assert (
        await on_assistants_read(
            ctx=make_ctx(
                make_identity(Role.ADMIN),
                resource="assistants",
                action="search",
            ),
            value=value,
        )
        is None
    )


async def test_assistants_read_rejects_out_of_scope_metadata() -> None:
    identity = make_identity(Role.VIEWER, [ScopeRef(project_id="p1")])
    with pytest.raises(Auth.exceptions.HTTPException) as excinfo:
        await on_assistants_read(
            ctx=make_ctx(identity, resource="assistants", action="search"),
            value={"metadata": {"project_id": "p9"}},
        )
    assert excinfo.value.status_code == 403


async def test_assistants_read_rejects_public_metadata_equality_oracle() -> None:
    identity = make_identity(Role.VIEWER, [ScopeRef(project_id="p1")])

    with pytest.raises(Auth.exceptions.HTTPException) as excinfo:
        await on_assistants_read(
            ctx=make_ctx(identity, resource="assistants", action="search"),
            value={"metadata": {"api_key": "assistant-secret-guess"}},
        )

    assert excinfo.value.status_code == 403
    assert "metadata filters" in str(excinfo.value.detail)


@pytest.mark.parametrize(
    "value",
    [
        {"limit": 101},
        {"offset": 10_001},
        {"metadata": {"note": "bad\x00filter"}},
        {"metadata": {"bad\x00key": "value"}},
        {"graph_id": "x" * 256},
        {"name": "x" * 256},
    ],
)
async def test_assistants_read_rejects_unbounded_or_invalid_search(
    value: dict[str, Any],
) -> None:
    with pytest.raises(Auth.exceptions.HTTPException) as excinfo:
        await on_assistants_read(
            ctx=make_ctx(make_identity(Role.VIEWER), resource="assistants", action="search"),
            value=value,
        )

    assert excinfo.value.status_code == 422


async def test_crons_write_requires_operator() -> None:
    with pytest.raises(Auth.exceptions.HTTPException) as excinfo:
        await on_crons_write(
            ctx=make_ctx(make_identity(Role.VIEWER), resource="crons", action="create"), value={}
        )
    assert excinfo.value.status_code == 403


@pytest.mark.parametrize(
    "value",
    [
        {"schedule": "bad\x00schedule"},
        {"payload": {"input": {"title": "bad\x00title"}}},
    ],
)
async def test_crons_write_is_disabled_for_unscoped_admin(value: dict[str, Any]) -> None:
    with pytest.raises(Auth.exceptions.HTTPException) as excinfo:
        await on_crons_write(
            ctx=make_ctx(make_identity(Role.ADMIN), resource="crons", action="create"),
            value=value,
        )

    assert excinfo.value.status_code == 403
    assert "Native scheduled runs are disabled" in str(excinfo.value.detail)


async def test_crons_read_requires_unscoped_admin_for_existing_crons() -> None:
    identity = make_identity(Role.VIEWER, [ScopeRef(project_id="p1")])
    with pytest.raises(Auth.exceptions.HTTPException) as excinfo:
        await on_crons_read(ctx=make_ctx(identity, resource="crons", action="search"), value={})
    assert excinfo.value.status_code == 403


async def test_crons_read_is_disabled_for_unscoped_admin() -> None:
    with pytest.raises(Auth.exceptions.HTTPException) as excinfo:
        await on_crons_read(
            ctx=make_ctx(make_identity(Role.ADMIN), resource="crons", action="search"), value={}
        )

    assert excinfo.value.status_code == 403
    assert "Native scheduled runs are disabled" in str(excinfo.value.detail)


async def test_store_write_requires_operator() -> None:
    with pytest.raises(Auth.exceptions.HTTPException) as excinfo:
        await on_store_write(
            ctx=make_ctx(make_identity(Role.VIEWER), resource="store", action="put"), value={}
        )
    assert excinfo.value.status_code == 403


@pytest.mark.parametrize(
    "value",
    [
        {"namespace": ("mem\x00ories",), "key": "k1"},
        {"namespace": ("memories",), "key": "bad\x00key"},
        {"namespace": ("memories",), "key": "k1", "value": {"note": "bad\x00note"}},
        {"namespace": ("memories",), "key": "k1", "index": ["bad\x00path"]},
    ],
)
async def test_store_write_is_disabled_before_parsing(value: dict[str, Any]) -> None:
    with pytest.raises(Auth.exceptions.HTTPException) as excinfo:
        await on_store_write(
            ctx=make_ctx(make_identity(Role.OPERATOR), resource="store", action="put"),
            value=value,
        )

    assert excinfo.value.status_code == 403
    assert "store access is disabled" in str(excinfo.value.detail)


async def test_store_read_is_disabled_for_project_consumer() -> None:
    identity = make_identity(Role.VIEWER, [ScopeRef(project_id="p1")])
    value: dict[str, Any] = {"namespace": ("memories",), "key": "k1"}
    with pytest.raises(Auth.exceptions.HTTPException) as excinfo:
        await on_store_read(ctx=make_ctx(identity, resource="store", action="get"), value=value)

    assert excinfo.value.status_code == 403


async def test_store_read_is_disabled_for_app_consumer() -> None:
    identity = make_identity(Role.VIEWER, [ScopeRef(project_id="p1", app_id="a1")])
    value: dict[str, Any] = {"namespace": ("memories",), "key": "k1"}

    with pytest.raises(Auth.exceptions.HTTPException) as excinfo:
        await on_store_read(ctx=make_ctx(identity, resource="store", action="get"), value=value)

    assert excinfo.value.status_code == 403


async def test_store_write_is_disabled_for_multi_project_consumer() -> None:
    identity = make_identity(Role.OPERATOR, [ScopeRef(project_id="p1"), ScopeRef(project_id="p2")])
    value: dict[str, Any] = {"namespace": ("apex", "project", "p2", "memories"), "key": "k1"}
    with pytest.raises(Auth.exceptions.HTTPException) as excinfo:
        await on_store_write(ctx=make_ctx(identity, resource="store", action="put"), value=value)

    assert excinfo.value.status_code == 403


async def test_store_write_rejects_out_of_scope_project_namespace() -> None:
    identity = make_identity(Role.OPERATOR, [ScopeRef(project_id="p1")])
    with pytest.raises(Auth.exceptions.HTTPException) as excinfo:
        await on_store_write(
            ctx=make_ctx(identity, resource="store", action="put"),
            value={"namespace": ("apex", "project", "p9", "memories"), "key": "k1"},
        )
    assert excinfo.value.status_code == 403


async def test_store_write_rejects_sibling_app_and_project_wide_namespaces() -> None:
    identity = make_identity(Role.OPERATOR, [ScopeRef(project_id="p1", app_id="a1")])

    for namespace in [
        ("apex", "project", "p1", "memories"),
        ("apex", "project", "p1", "app", "a2", "memories"),
    ]:
        with pytest.raises(Auth.exceptions.HTTPException) as excinfo:
            await on_store_write(
                ctx=make_ctx(identity, resource="store", action="put"),
                value={"namespace": namespace, "key": "k1"},
            )
        assert excinfo.value.status_code == 403


async def test_store_write_is_disabled_for_multi_app_identity() -> None:
    identity = make_identity(
        Role.OPERATOR,
        [ScopeRef(project_id="p1", app_id="a1"), ScopeRef(project_id="p1", app_id="a2")],
    )
    value: dict[str, Any] = {
        "namespace": ("apex", "project", "p1", "app", "a2", "memories"),
        "key": "k1",
    }

    with pytest.raises(Auth.exceptions.HTTPException) as excinfo:
        await on_store_write(ctx=make_ctx(identity, resource="store", action="put"), value=value)

    assert excinfo.value.status_code == 403


async def test_store_write_requires_app_prefix_for_multi_app_identity() -> None:
    identity = make_identity(
        Role.OPERATOR,
        [ScopeRef(project_id="p1", app_id="a1"), ScopeRef(project_id="p1", app_id="a2")],
    )

    with pytest.raises(Auth.exceptions.HTTPException) as excinfo:
        await on_store_write(
            ctx=make_ctx(identity, resource="store", action="put"),
            value={"namespace": ("memories",), "key": "k1"},
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


@pytest.mark.parametrize(
    "value",
    [
        {"namespace": ("memories",), "limit": 101},
        {"namespace": ("memories",), "offset": 10_001},
        {"namespace": ("memories",), "query": "bad\x00query"},
        {"namespace": ("memories",), "filter": {"note": "bad\x00filter"}},
        {"namespace": ("x" * 256,)},
        {"namespace": tuple(f"part-{index}" for index in range(33))},
    ],
)
async def test_store_search_rejects_unbounded_or_postgres_incompatible_input(
    value: dict[str, Any],
) -> None:
    with pytest.raises(Auth.exceptions.HTTPException) as excinfo:
        await on_store_read(
            ctx=make_ctx(make_identity(Role.VIEWER), resource="store", action="search"),
            value=value,
        )

    assert excinfo.value.status_code == 403


async def test_store_list_namespaces_is_disabled() -> None:
    identity = make_identity(Role.VIEWER, [ScopeRef(project_id="p1")])
    value: dict[str, Any] = {
        "namespace": None,
        "suffix": ("memories",),
        "limit": 100,
        "offset": 0,
        "max_depth": 8,
    }

    with pytest.raises(Auth.exceptions.HTTPException) as excinfo:
        await on_store_read(
            ctx=make_ctx(identity, resource="store", action="list_namespaces"),
            value=value,
        )

    assert excinfo.value.status_code == 403


@pytest.mark.parametrize(
    "value",
    [
        {"namespace": ("memories",), "suffix": ("x" * 256,)},
        {"namespace": ("memories",), "limit": 101},
        {"namespace": ("memories",), "max_depth": 33},
    ],
)
async def test_store_list_namespaces_rejects_unbounded_input(value: dict[str, Any]) -> None:
    with pytest.raises(Auth.exceptions.HTTPException) as excinfo:
        await on_store_read(
            ctx=make_ctx(
                make_identity(Role.ADMIN),
                resource="store",
                action="list_namespaces",
            ),
            value=value,
        )

    assert excinfo.value.status_code == 403


@pytest.mark.parametrize(
    "value",
    [
        {"namespace": ("memories",), "key": "k" * 256, "value": {}},
        {"namespace": ("memories",), "key": "k1", "value": {}, "ttl": float("inf")},
        {
            "namespace": ("memories",),
            "key": "k1",
            "value": {},
            "index": [f"path-{index}" for index in range(33)],
        },
    ],
)
async def test_store_write_rejects_unbounded_key_ttl_and_index(value: dict[str, Any]) -> None:
    with pytest.raises(Auth.exceptions.HTTPException) as excinfo:
        await on_store_write(
            ctx=make_ctx(make_identity(Role.OPERATOR), resource="store", action="put"),
            value=value,
        )

    assert excinfo.value.status_code == 403


@pytest.mark.parametrize(
    "value",
    [
        {"limit": 101, "offset": 0},
        {"limit": 10, "offset": 10_001},
        {"limit": 10, "offset": 0, "metadata": {"title": "bad\x00filter"}},
    ],
)
async def test_threads_search_rejects_unbounded_or_invalid_filters(
    value: dict[str, Any],
) -> None:
    with pytest.raises(Auth.exceptions.HTTPException) as excinfo:
        await on_threads_search(
            ctx=make_ctx(make_identity(Role.VIEWER), resource="threads", action="search"),
            value=cast(Any, value),
        )

    assert excinfo.value.status_code == 422


async def test_threads_search_rejects_public_value_equality_oracle() -> None:
    identity = make_identity(Role.VIEWER, [ScopeRef(project_id="p1")])

    with pytest.raises(Auth.exceptions.HTTPException) as excinfo:
        await on_threads_search(
            ctx=make_ctx(identity, resource="threads", action="search"),
            value=cast(Any, {"values": {"engine_handle": {"provider_token": "guess"}}}),
        )

    assert excinfo.value.status_code == 403
    assert "value filters" in str(excinfo.value.detail)


async def test_threads_search_rejects_public_metadata_equality_oracle() -> None:
    identity = make_identity(Role.VIEWER, [ScopeRef(project_id="p1")])

    with pytest.raises(Auth.exceptions.HTTPException) as excinfo:
        await on_threads_search(
            ctx=make_ctx(identity, resource="threads", action="search"),
            value=cast(Any, {"metadata": {"api_key": "thread-secret-guess"}}),
        )

    assert excinfo.value.status_code == 403
    assert "metadata filters" in str(excinfo.value.detail)


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
    assert event.reason == (
        "Native scheduled runs are disabled; use an operator-controlled scheduler"
    )
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

"""Deterministic unit coverage for usage aggregation and normalization helpers."""

from collections import deque
from datetime import UTC, datetime, timedelta
from typing import Any, cast

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from apex.auth.identity import ConsumerIdentity, ConsumerType, Role, ScopeRef
from apex.persistence.models import UsageEvent
from apex.services import usage


class _Rows:
    def __init__(self, rows: list[tuple[Any, ...]]) -> None:
        self._rows = rows

    def all(self) -> list[tuple[Any, ...]]:
        return self._rows

    def one(self) -> tuple[Any, ...]:
        assert len(self._rows) == 1
        return self._rows[0]


class _Session:
    def __init__(self, rows: list[list[tuple[Any, ...]]]) -> None:
        self._rows = deque(rows)
        self.statements: list[Any] = []

    async def execute(self, statement: Any) -> _Rows:
        self.statements.append(statement)
        return _Rows(self._rows.popleft())


@pytest.mark.asyncio
async def test_usage_aggregate_shapes_surfaces_buckets_actions_and_runs() -> None:
    start = datetime(2026, 7, 1, tzinfo=UTC)
    session = _Session(
        [
            [("v1", 7, 2), ("graph", 3, None)],
            [(start, 6, 1), (start + timedelta(hours=1), 4, None)],
            [("getRuns", 5), ("phase:execution:succeeded", 3)],
            [(2, None)],
        ]
    )
    repository = usage.UsageAnalyticsRepository(cast(AsyncSession, session))

    result = await repository.aggregate(
        window_from=start,
        window_to=start + timedelta(days=1),
        bucket="hour",
        project_id="project-a",
        visible_scopes=(ScopeRef(project_id="project-a", app_id="app-a"),),
    )

    assert result == {
        "totals": {"events": 10, "errors": 2, "by_surface": {"v1": 7, "graph": 3}},
        "buckets": [
            {"bucket_start": start, "events": 6, "errors": 1},
            {
                "bucket_start": start + timedelta(hours=1),
                "events": 4,
                "errors": 0,
            },
        ],
        "top_actions": [
            {"action": "getRuns", "count": 5},
            {"action": "phase:execution:succeeded", "count": 3},
        ],
        "runs": {"phases_succeeded": 2, "phases_failed": 0},
    }
    assert not session._rows
    sql = "\n".join(str(statement) for statement in session.statements)
    assert "usage_events.project_id" in sql
    assert "usage_events.app_id" in sql
    assert "date_trunc" in sql
    assert any(
        "phase:%:succeeded" in str(statement.compile(compile_kwargs={"literal_binds": True}))
        for statement in session.statements
    )


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (
            {
                "input_tokens": "12",
                "output_tokens": 3,
                "input_token_details": {"cache_read_input_tokens": 4, "cache_write": 2},
                "output_token_details": {"reasoning_output_tokens": 1},
            },
            {
                "input_tokens": 12,
                "output_tokens": 3,
                "total_tokens": 15,
                "cache_read_tokens": 4,
                "cache_creation_tokens": 2,
                "reasoning_tokens": 1,
            },
        ),
        (
            {
                "input_tokens": 7,
                "output_tokens": 2,
                "total_tokens": 20,
                "input_token_details": "malformed",
                "output_token_details": object(),
            },
            {
                "input_tokens": 7,
                "output_tokens": 2,
                "total_tokens": 9,
                "cache_read_tokens": 0,
                "cache_creation_tokens": 0,
                "reasoning_tokens": 0,
            },
        ),
        (
            None,
            {
                "input_tokens": 0,
                "output_tokens": 0,
                "total_tokens": 0,
                "cache_read_tokens": 0,
                "cache_creation_tokens": 0,
                "reasoning_tokens": 0,
            },
        ),
    ],
)
def test_usage_metadata_normalization_handles_aliases_and_malformed_details(
    raw: dict[str, Any] | None,
    expected: dict[str, int],
) -> None:
    assert usage.normalize_usage_metadata(raw) == expected


@pytest.mark.parametrize(
    ("model", "metadata", "expected"),
    [
        ("ignored", {"provider": "vendor\x00name"}, "vendor\\0name"),
        ("ignored", {"ls_provider": "langchain"}, "langchain"),
        ("azure:gpt", None, "azure"),
        ("bedrock/claude", None, "bedrock"),
        ("claude-3", None, "anthropic"),
        ("gpt-5", None, "openai"),
        ("custom", None, None),
    ],
)
def test_provider_inference_is_bounded_and_predictable(
    model: str | None,
    metadata: dict[str, Any] | None,
    expected: str | None,
) -> None:
    assert usage._provider_from(model, metadata) == expected


def test_bounded_event_label_rejects_non_text_empty_and_nul() -> None:
    assert usage._bounded_event_label(cast(Any, 42), 10) is None
    assert usage._bounded_event_label("", 10) is None
    assert usage._bounded_event_label("a\x00b-too-long", 5) == r"a\0b-"


def test_provider_labels_never_execute_string_subclass_hooks() -> None:
    class HostileText(str):
        called = False

        def replace(self, *_args: object, **_kwargs: object) -> str:
            type(self).called = True
            raise AssertionError("provider string replacement must not execute")

        def __contains__(self, _value: object) -> bool:
            type(self).called = True
            raise AssertionError("provider string membership must not execute")

    raw = HostileText("vendor")

    assert usage._bounded_event_label(raw, 10) is None
    assert usage._provider_from(raw, {"provider": raw}) is None
    assert raw.called is False


def test_provider_fallback_never_executes_hostile_scalar_truthiness() -> None:
    class HostileProvider:
        called = False

        def __bool__(self) -> bool:
            self.called = True
            raise AssertionError("provider scalar truthiness must not execute")

    raw = HostileProvider()

    assert usage._provider_from("ignored", {"provider": raw, "ls_provider": "fallback"}) == (
        "fallback"
    )
    assert raw.called is False


def _identity(*scopes: ScopeRef, role: Role = Role.VIEWER) -> ConsumerIdentity:
    return ConsumerIdentity(
        consumer_id="consumer",
        name="consumer",
        consumer_type=ConsumerType.HEADLESS,
        role=role,
        scopes=list(scopes),
    )


@pytest.mark.parametrize(
    ("identity", "path_params", "query_string", "expected"),
    [
        (None, {}, b"project=p", (None, None)),
        (_identity(ScopeRef(project_id="p", app_id="a")), {"app_id": "a"}, b"", (None, None)),
        (
            _identity(ScopeRef(project_id="p", app_id="a")),
            {"project_id": "p", "app_id": "a"},
            b"",
            ("p", "a"),
        ),
        (
            _identity(ScopeRef(project_id="p", app_id="a")),
            {"project_id": "p", "app_id": "other"},
            b"",
            (None, None),
        ),
        (
            _identity(ScopeRef(project_id="p", app_id="a")),
            {"project_id": "p"},
            b"",
            ("p", "a"),
        ),
        (
            _identity(ScopeRef(project_id="p", app_id="a"), ScopeRef(project_id="p", app_id="b")),
            {"project_id": "p"},
            b"",
            (None, None),
        ),
        (
            _identity(role=Role.ADMIN),
            {"project_id": "anything"},
            b"",
            ("anything", None),
        ),
        (
            _identity(ScopeRef(project_id="p", app_id="a")),
            {},
            b"project=p&app=a",
            ("p", "a"),
        ),
    ],
)
def test_request_scope_never_widens_identity_access(
    identity: ConsumerIdentity | None,
    path_params: dict[str, str],
    query_string: bytes,
    expected: tuple[str | None, str | None],
) -> None:
    state = {"identity": identity} if identity is not None else {}
    scope = {"state": state, "path_params": path_params, "query_string": query_string}
    assert usage._request_scope(scope) == expected
    assert usage._request_project_id(scope) == expected[0]


def test_request_scope_bounds_query_parsing_and_rejects_hostile_path_scalars() -> None:
    identity = _identity(role=Role.ADMIN)
    calls: list[str] = []

    class HostileText(str):
        def __str__(self) -> str:
            calls.append("str")
            raise AssertionError("hostile request scalar hook ran")

        def strip(self, *_args: object, **_kwargs: object) -> str:
            calls.append("strip")
            raise AssertionError("hostile request scalar hook ran")

    assert usage._request_scope(
        {
            "state": {"identity": identity},
            "path_params": {"project_id": HostileText("project-secret")},
            "query_string": b"",
        }
    ) == (None, None)
    assert usage._request_scope(
        {
            "state": {"identity": identity},
            "path_params": {},
            "query_string": b"&".join(b"project=p" for _ in range(257)),
        }
    ) == (None, None)
    assert calls == []


def test_idempotent_insert_supports_postgres_and_requires_event_key() -> None:
    values = {
        "event_key": "stable-key",
        "consumer_name": "graph",
        "surface": "graph",
        "action": "phase:execution:succeeded",
        "status": "ok",
        "extra": {},
    }
    assert usage._idempotent_insert(UsageEvent, values, "postgresql") is not None
    assert usage._idempotent_insert(UsageEvent, values, "unknown") is None
    assert usage._idempotent_insert(UsageEvent, values | {"event_key": None}, "sqlite") is None

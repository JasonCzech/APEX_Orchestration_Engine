"""Systematic role x endpoint authorization matrix for every /v1 route.

The route inventory is derived from the live FastAPI app (apex.app.http:app), so
a brand-new endpoint FAILS the matrix until it is classified in
tests/unit/authz_matrix_data.py — completeness is structural, not curated.
(/v1/docs and /v1/openapi.json are plain Starlette routes, not APIRoutes; they
are FastAPI's unauthenticated docs surface and are deliberately outside the
matrix.)

For each route x principal in (no_key, viewer, operator, admin):
- no key             -> 401 (authentication precedes everything else)
- below minimum role -> 403 exactly (a 5xx here would mean fallible IO ran
  BEFORE require_role — an ordering bug the matrix exists to catch)
- at/above minimum   -> anything but 401/403; 404/409/415/422/5xx are all fine
  evidence: the hermetic unreachable DB and the exploding loopback stub only
  fail AFTER the authorization decision.

Hermetic: no Postgres (conftest points the DB at an unreachable port), no
LangGraph server (loopback_client is monkeypatched to raise), identities come
from a resolver whose DB lookup is replaced by an in-memory key map (the
documented seam — see apex.auth.service.get_default_resolver). The admin
principal exercises the real dev-key path via APEX_AUTH__DEV_API_KEY.
"""

import re
from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

import apex.auth.service as auth_service
import apex.routers.context as context_router
import apex.routers.engines as engines_router
import apex.routers.pipelines as pipelines_router
import apex.routers.prompts as prompts_router
from apex.app.http import app
from apex.auth.identity import ConsumerIdentity, ConsumerType, Role, ScopeRef
from apex.auth.service import IdentityResolver
from apex.settings import get_settings
from tests.unit.authz_matrix_data import BODY_OVERRIDES, MIN_ROLE, PATH_PARAM_OVERRIDES, SCOPE

# ── Principals ───────────────────────────────────────────────────────────────

DEV_ADMIN_KEY = "matrix-dev-admin-key"  # resolved via the real dev-key path
VIEWER_KEY = "matrix-viewer-key"
OPERATOR_KEY = "matrix-operator-key"

API_KEYS: dict[str, str] = {
    "viewer": VIEWER_KEY,
    "operator": OPERATOR_KEY,
    "admin": DEV_ADMIN_KEY,
}
RANK: dict[str, int] = {"viewer": 0, "operator": 1, "admin": 2}
PRINCIPALS: tuple[str, ...] = ("no_key", "viewer", "operator", "admin")


def _identity(role: Role) -> ConsumerIdentity:
    return ConsumerIdentity(
        consumer_id=f"matrix-{role.value}",
        name=f"matrix-{role.value}",
        consumer_type=ConsumerType.DASHBOARD,
        role=role,
        scopes=[ScopeRef(project_id="proj-matrix")],
    )


class MatrixResolver(IdentityResolver):
    """Real resolve() pipeline (auth toggle + dev key), in-memory 'DB' lookup."""

    _by_key: dict[str, ConsumerIdentity] = {
        VIEWER_KEY: _identity(Role.VIEWER),
        OPERATOR_KEY: _identity(Role.OPERATOR),
    }

    async def _resolve_from_db(self, api_key: str) -> ConsumerIdentity | None:
        return self._by_key.get(api_key)


def _exploding_loopback(api_key: str | None = None) -> Any:
    """Stand-in for loopback_client: any loopback-bound route fails fast post-authz."""
    raise RuntimeError("authz matrix: loopback LangGraph API is unavailable in unit tests")


# Modules that imported loopback_client by value; patch each reference.
_LOOPBACK_MODULES = (pipelines_router, context_router, engines_router, prompts_router)


@pytest.fixture(autouse=True)
def authz_world(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("APEX_AUTH__ENABLED", "true")
    monkeypatch.setenv("APEX_AUTH__DEV_API_KEY", DEV_ADMIN_KEY)
    get_settings.cache_clear()
    monkeypatch.setattr(auth_service, "default_resolver", MatrixResolver())
    for module in _LOOPBACK_MODULES:
        monkeypatch.setattr(module, "loopback_client", _exploding_loopback)
    yield
    get_settings.cache_clear()


# ── Live route inventory ─────────────────────────────────────────────────────


def _live_inventory() -> list[tuple[str, str, str]]:
    """(method, path, operation_id) for every /v1 APIRoute in the composed app."""
    rows: list[tuple[str, str, str]] = []
    for route in app.routes:
        if not isinstance(route, APIRoute) or not route.path.startswith("/v1/"):
            continue
        operation_id = route.operation_id or f"<no operation_id: {route.name}>"
        rows.extend(
            (method, route.path, operation_id)
            for method in sorted(route.methods - {"HEAD", "OPTIONS"})
        )
    return sorted(rows, key=lambda row: (row[1], row[0]))


INVENTORY = _live_inventory()

_PATH_PARAM = re.compile(r"{(\w+)(?::[^}]*)?}")
SYNTHETIC_ID = "x" * 32


def _fill_path(path: str, operation_id: str) -> str:
    overrides = PATH_PARAM_OVERRIDES.get(operation_id, {})
    return _PATH_PARAM.sub(lambda m: overrides.get(m.group(1), SYNTHETIC_ID), path)


# ── Structural completeness ──────────────────────────────────────────────────


def test_every_v1_route_is_classified() -> None:
    """New endpoints must be classified; removed endpoints must leave the table."""
    live = {operation_id for _, _, operation_id in INVENTORY}
    expected = set(MIN_ROLE)
    unclassified = live - expected
    stale = expected - live
    assert not unclassified, (
        "unclassified operation_id(s) — add them to tests/unit/authz_matrix_data.py "
        f"MIN_ROLE with an explicit minimum role: {sorted(unclassified)}"
    )
    assert not stale, f"MIN_ROLE entries with no matching live route (remove them): {sorted(stale)}"


def test_every_v1_route_has_scope_classification() -> None:
    """New endpoints must also declare their tenant-scope behavior."""
    live = {operation_id for _, _, operation_id in INVENTORY}
    expected = set(SCOPE)
    unclassified = live - expected
    stale = expected - live
    allowed = {"none", "project", "project_app", "provider_project", "admin_scope"}
    invalid = {operation_id: scope for operation_id, scope in SCOPE.items() if scope not in allowed}

    assert not unclassified, (
        "unclassified scope operation_id(s) — add them to tests/unit/authz_matrix_data.py "
        f"SCOPE with an explicit scope mode: {sorted(unclassified)}"
    )
    assert not stale, f"SCOPE entries with no matching live route (remove them): {sorted(stale)}"
    assert not invalid, f"SCOPE entries with invalid mode: {invalid}"


def test_inventory_matches_published_surface() -> None:
    """Sanity: the matrix saw a one-method-per-route /v1 surface of real size."""
    assert len(INVENTORY) == len({(m, p) for m, p, _ in INVENTORY})
    assert len(INVENTORY) >= 72  # 72 operations at M6; grows with the API


# ── The matrix ───────────────────────────────────────────────────────────────


@pytest.mark.parametrize("principal", PRINCIPALS)
@pytest.mark.parametrize(
    ("method", "path", "operation_id"),
    INVENTORY,
    ids=[f"{method} {path}" for method, path, _ in INVENTORY],
)
def test_authz_matrix(method: str, path: str, operation_id: str, principal: str) -> None:
    if operation_id not in MIN_ROLE:
        pytest.fail(
            f"unclassified operation_id {operation_id!r} for {method} {path} — "
            "classify it in tests/unit/authz_matrix_data.py"
        )
    minimum = MIN_ROLE[operation_id]

    client = TestClient(app, raise_server_exceptions=False)
    headers = {} if principal == "no_key" else {"x-api-key": API_KEYS[principal]}
    body = BODY_OVERRIDES.get(operation_id, {}) if method in {"PATCH", "POST", "PUT"} else None
    response = client.request(method, _fill_path(path, operation_id), headers=headers, json=body)

    if principal == "no_key":
        assert response.status_code == 401, (
            f"{method} {path} ({operation_id}): unauthenticated requests must 401, "
            f"got {response.status_code}"
        )
    elif RANK[principal] < RANK[minimum]:
        assert response.status_code == 403, (
            f"{method} {path} ({operation_id}): role '{principal}' is below minimum "
            f"'{minimum}' and must 403, got {response.status_code} — anything else "
            "(especially a 5xx) means IO or other handling ran before require_role"
        )
    else:
        assert response.status_code not in (401, 403), (
            f"{method} {path} ({operation_id}): role '{principal}' meets minimum "
            f"'{minimum}' but was rejected with {response.status_code}"
        )

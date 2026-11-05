"""/v1/admin/aifx: onboarding organizations and application repo mappings."""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from apex.adapters.stubs.source_control import StubSourceControlAdapter
from apex.app.dependencies import get_current_identity
from apex.auth.identity import ConsumerIdentity, ConsumerType, Role, ScopeRef
from apex.persistence.models import AifxApplicationSource, AifxOrganization
from apex.persistence.repositories.aifx import AifxInUseError
from apex.routers.aifx import (
    get_aifx_catalog_repository,
    get_aifx_connections_repository,
    get_aifx_repository,
    router,
)
from apex.services.connections import get_connection_resolver
from apex.settings import get_settings

NOW = datetime(2026, 11, 4, tzinfo=UTC)


class FakeAifxRepo:
    def __init__(self) -> None:
        self.orgs: dict[str, AifxOrganization] = {}
        self.sources: dict[str, AifxApplicationSource] = {}
        self.batched_lookups: list[list[str]] = []

    async def list_organizations(self) -> list[AifxOrganization]:
        return list(self.orgs.values())

    async def get_organization(self, organization_id: str) -> AifxOrganization | None:
        return self.orgs.get(organization_id)

    async def create_organization(self, fields: dict[str, Any]) -> AifxOrganization:
        row = AifxOrganization(
            id=f"org-{len(self.orgs) + 1}", created_at=NOW, updated_at=NOW, **fields
        )
        self.orgs[row.id] = row
        return row

    async def update_organization(
        self, row: AifxOrganization, changes: dict[str, Any]
    ) -> AifxOrganization:
        for key, value in changes.items():
            setattr(row, key, value)
        return row

    async def delete_organization(self, row: AifxOrganization) -> None:
        if any(source.organization_id == row.id for source in self.sources.values()):
            raise AifxInUseError("remove its application mappings first")
        del self.orgs[row.id]

    async def list_sources(
        self,
        application_ids: Sequence[str] | None = None,
        *,
        limit: int | None = None,
        offset: int = 0,
    ) -> list[AifxApplicationSource]:
        rows = sorted(self.sources.values(), key=lambda row: row.apm_id)
        if application_ids is not None:
            rows = [row for row in rows if row.application_id in application_ids]
        return rows[offset : None if limit is None else offset + limit]

    async def applications_by_id(self, application_ids: Sequence[str]) -> dict[str, Any]:
        self.batched_lookups.append(list(application_ids))
        return {
            app_id: FakeCatalog.apps[app_id]
            for app_id in application_ids
            if app_id in FakeCatalog.apps
        }

    async def get_source(self, application_id: str) -> AifxApplicationSource | None:
        return self.sources.get(application_id)

    async def apm_taken(self, apm_id: str, *, except_application_id: str) -> bool:
        return any(
            source.apm_id.lower() == apm_id.lower() and app_id != except_application_id
            for app_id, source in self.sources.items()
        )

    async def put_source(
        self, application_id: str, fields: dict[str, Any]
    ) -> AifxApplicationSource:
        row = AifxApplicationSource(application_id=application_id, updated_at=NOW, **fields)
        self.sources[application_id] = row
        return row

    async def delete_source(self, row: AifxApplicationSource) -> None:
        del self.sources[row.application_id]


class FakeCatalog:
    apps = {
        "app-a": SimpleNamespace(id="app-a", name="Storefront", project_ids=["proj-1"]),
        "app-b": SimpleNamespace(id="app-b", name="Payments", project_ids=["proj-1", "proj-2"]),
    }

    async def get_application(self, application_id: str) -> Any:
        return self.apps.get(application_id)


class FakeConnections:
    rows = {
        "conn-ghe": SimpleNamespace(kind="source_control", project_id=None),
        "conn-scoped": SimpleNamespace(kind="source_control", project_id="proj-1"),
        "conn-jira": SimpleNamespace(kind="work_tracking", project_id=None),
    }

    async def get(self, connection_id: str) -> Any:
        return self.rows.get(connection_id)


class FakeResolver:
    async def resolve_metadata(self, kind: Any, connection_id: str, project_id: Any) -> Any:
        return SimpleNamespace(
            internal_project_id=None,
            config=SimpleNamespace(
                provider="github_enterprise",
                options={"base_url": "https://ghe.example.test/api/v3"},
            ),
        )

    async def build_from_metadata(self, metadata: Any) -> Any:
        return StubSourceControlAdapter()


def who(role: Role = Role.ADMIN, scopes: list[ScopeRef] | None = None) -> ConsumerIdentity:
    return ConsumerIdentity(
        consumer_id="c-test",
        name="test",
        consumer_type=ConsumerType.INTERNAL,
        role=role,
        scopes=scopes or [],
    )


@pytest.fixture(autouse=True)
def aifx_enabled(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("APEX_AIFX_ENABLED", "true")
    get_settings.cache_clear()
    yield
    # Tests override APEX_AIFX__* budgets; never leak a cached copy to other files.
    get_settings.cache_clear()


@pytest.fixture
def repo() -> FakeAifxRepo:
    return FakeAifxRepo()


def client(repo: FakeAifxRepo, identity: ConsumerIdentity) -> TestClient:
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_aifx_repository] = lambda: repo
    app.dependency_overrides[get_aifx_catalog_repository] = FakeCatalog
    app.dependency_overrides[get_aifx_connections_repository] = FakeConnections
    app.dependency_overrides[get_connection_resolver] = FakeResolver
    app.dependency_overrides[get_current_identity] = lambda: identity
    return TestClient(app)


ORG = {"name": "Consumer Mobility", "connection_id": "conn-ghe", "ghe_owner": "aifx"}


def test_disabled_flag_hides_every_route(
    repo: FakeAifxRepo, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("APEX_AIFX_ENABLED", "false")
    get_settings.cache_clear()
    assert client(repo, who()).get("/admin/aifx/organizations").status_code == 404


def test_only_admins_reach_aifx(repo: FakeAifxRepo) -> None:
    assert client(repo, who(Role.OPERATOR)).get("/admin/aifx/organizations").status_code == 403


def test_create_organization_applies_conventions(repo: FakeAifxRepo) -> None:
    response = client(repo, who()).post("/admin/aifx/organizations", json=ORG)
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["global_rules_repo"] == "global-rules"
    assert body["team_documents_repo_pattern"] == "{apm}-team-documents"
    assert body["acb_repo_pattern"] == "{apm}-acb"
    assert body["default_ref"] == "main"


@pytest.mark.parametrize(
    ("overrides", "status"),
    [
        ({"connection_id": "conn-scoped"}, 422),
        ({"connection_id": "conn-jira"}, 422),
        ({"connection_id": "missing"}, 422),
        ({"ghe_owner": "bad owner"}, 422),
        ({"acb_repo_pattern": "acb-without-placeholder"}, 422),
        ({"default_ref": "../main"}, 422),
    ],
)
def test_create_organization_validates(
    repo: FakeAifxRepo, overrides: dict[str, Any], status: int
) -> None:
    assert (
        client(repo, who()).post("/admin/aifx/organizations", json={**ORG, **overrides}).status_code
        == status
    )


def test_scoped_admin_cannot_change_organizations(repo: FakeAifxRepo) -> None:
    scoped = client(repo, who(scopes=[ScopeRef(project_id="proj-1")]))
    assert scoped.post("/admin/aifx/organizations", json=ORG).status_code == 403


def _onboard(api: TestClient, app_id: str = "app-a", apm: str = "APM0012") -> Any:
    org_id = api.post("/admin/aifx/organizations", json=ORG).json()["id"]
    suggestion = api.get(
        f"/admin/aifx/applications/{app_id}/suggest",
        params={"organization_id": org_id, "apm_id": apm},
    ).json()
    response = api.put(
        f"/admin/aifx/applications/{app_id}",
        json={
            "organization_id": org_id,
            "apm_id": apm,
            "team_documents_repo": suggestion["team_documents_repo"],
            "acb_repo": suggestion["acb_repo"],
        },
    )
    return org_id, suggestion, response


def test_suggest_then_confirm_an_application_mapping(repo: FakeAifxRepo) -> None:
    api = client(repo, who())
    _org_id, suggestion, response = _onboard(api)
    assert suggestion["team_documents_repo"] == "apm0012-team-documents"
    assert suggestion["acb_repo"] == "apm0012-acb"
    assert response.status_code == 200, response.text
    assert response.json()["application_name"] == "Storefront"
    listed = api.get("/admin/aifx/applications").json()
    assert [row["apm_id"] for row in listed] == ["APM0012"]


def test_apm_ids_are_unique_case_insensitively(repo: FakeAifxRepo) -> None:
    api = client(repo, who())
    org_id, _suggestion, _response = _onboard(api)
    duplicate = api.put(
        "/admin/aifx/applications/app-b", json={"organization_id": org_id, "apm_id": "apm0012"}
    )
    assert duplicate.status_code == 409


def test_organization_in_use_cannot_be_deleted(repo: FakeAifxRepo) -> None:
    api = client(repo, who())
    org_id, _suggestion, _response = _onboard(api)
    assert api.delete(f"/admin/aifx/organizations/{org_id}").status_code == 409
    assert api.delete("/admin/aifx/applications/app-a").status_code == 204
    assert api.delete(f"/admin/aifx/organizations/{org_id}").status_code == 204


def test_scoped_admin_needs_every_project_to_onboard(repo: FakeAifxRepo) -> None:
    org_id = client(repo, who()).post("/admin/aifx/organizations", json=ORG).json()["id"]
    scoped = client(repo, who(scopes=[ScopeRef(project_id="proj-1")]))
    body = {"organization_id": org_id, "apm_id": "APM9"}
    assert scoped.put("/admin/aifx/applications/app-a", json=body).status_code == 200
    # app-b is also used by proj-2, which this admin cannot see.
    assert (
        scoped.put("/admin/aifx/applications/app-b", json=body | {"apm_id": "APM8"}).status_code
        == 403
    )


def test_preview_pins_every_source_without_returning_content(repo: FakeAifxRepo) -> None:
    api = client(repo, who())
    _onboard(api)
    response = api.post("/admin/aifx/applications/app-a/preview")
    assert response.status_code == 200, response.text
    preview = response.json()
    assert preview["ok"] is True
    assert [source["role"] for source in preview["sources"]] == [
        "global-rules",
        "team-documents",
        "application-context",
    ]
    for source in preview["sources"]:
        assert len(source["commit_sha"]) == 40
        assert source["web_url"].startswith("https://ghe.example.test/aifx/")
        assert source["entries"] and all("content" not in entry for entry in source["entries"])
    assert preview["total_chars"] > 0


def test_preview_checks_a_draft_before_it_is_saved(repo: FakeAifxRepo) -> None:
    api = client(repo, who())
    org_id = api.post("/admin/aifx/organizations", json=ORG).json()["id"]
    draft = {"organization_id": org_id, "apm_id": "APM1", "acb_repo": "apm1-acb"}
    preview = api.post("/admin/aifx/applications/app-a/preview", json=draft).json()
    assert [source["role"] for source in preview["sources"]] == [
        "global-rules",
        "application-context",
    ]
    assert repo.sources == {}


def test_unknown_application_is_not_found(repo: FakeAifxRepo) -> None:
    assert client(repo, who()).get("/admin/aifx/applications/nope").status_code == 404


# ── regression: review fixes ─────────────────────────────────────────────────


SCOPED = [ScopeRef(project_id="proj-1")]


def _org(api: TestClient) -> str:
    return api.post("/admin/aifx/organizations", json=ORG).json()["id"]


@pytest.mark.parametrize(
    "overrides",
    [
        {"team_documents_repo": "other-team-documents"},
        {"acb_repo": "someone-elses-acb"},
        {"team_documents_repo": "apm0012-team-documents", "acb_repo": "apm0012-ACB-x"},
    ],
)
def test_scoped_admin_cannot_map_repos_outside_the_conventions(
    repo: FakeAifxRepo, overrides: dict[str, Any]
) -> None:
    org_id = _org(client(repo, who()))
    scoped = client(repo, who(scopes=SCOPED))
    body = {"organization_id": org_id, "apm_id": "APM0012", **overrides}
    put = scoped.put("/admin/aifx/applications/app-a", json=body)
    assert put.status_code == 403, put.text
    assert "unscoped platform admin" in put.json()["detail"]
    preview = scoped.post("/admin/aifx/applications/app-a/preview", json=body)
    assert preview.status_code == 403, preview.text
    assert repo.sources == {}


@pytest.mark.parametrize(
    "overrides",
    [
        {},
        {"team_documents_repo": None, "acb_repo": None},
        {"team_documents_repo": "apm0012-team-documents", "acb_repo": "apm0012-acb"},
        {"acb_repo": "apm0012-acb"},
    ],
)
def test_scoped_admin_may_use_convention_names_or_none(
    repo: FakeAifxRepo, overrides: dict[str, Any]
) -> None:
    org_id = _org(client(repo, who()))
    scoped = client(repo, who(scopes=SCOPED))
    body = {"organization_id": org_id, "apm_id": "APM0012", **overrides}
    assert scoped.post("/admin/aifx/applications/app-a/preview", json=body).status_code == 200
    assert scoped.put("/admin/aifx/applications/app-a", json=body).status_code == 200


def test_unscoped_admin_can_map_any_repository(repo: FakeAifxRepo) -> None:
    api = client(repo, who())
    body = {
        "organization_id": _org(api),
        "apm_id": "APM0012",
        "team_documents_repo": "other-team-documents",
        "acb_repo": "someone-elses-acb",
    }
    assert api.post("/admin/aifx/applications/app-a/preview", json=body).status_code == 200
    response = api.put("/admin/aifx/applications/app-a", json=body)
    assert response.status_code == 200, response.text
    assert response.json()["acb_repo"] == "someone-elses-acb"


def test_draft_preview_requires_every_project(repo: FakeAifxRepo) -> None:
    org_id = _org(client(repo, who()))
    scoped = client(repo, who(scopes=SCOPED))
    # app-b is linked to proj-1 and proj-2; this admin only holds proj-1.
    response = scoped.post(
        "/admin/aifx/applications/app-b/preview",
        json={"organization_id": org_id, "apm_id": "APM8"},
    )
    assert response.status_code == 403, response.text


def test_draft_preview_rejects_an_apm_id_mapped_elsewhere(repo: FakeAifxRepo) -> None:
    api = client(repo, who())
    org_id, _suggestion, _response = _onboard(api)
    response = api.post(
        "/admin/aifx/applications/app-b/preview",
        json={"organization_id": org_id, "apm_id": "apm0012"},
    )
    assert response.status_code == 409, response.text
    # The application already holding the APM ID can preview its own draft.
    own = api.post(
        "/admin/aifx/applications/app-a/preview",
        json={"organization_id": org_id, "apm_id": "APM0012"},
    )
    assert own.status_code == 200, own.text


def test_saved_mapping_preview_skips_draft_checks(repo: FakeAifxRepo) -> None:
    api = client(repo, who())
    org_id = _org(api)
    saved = api.put(
        "/admin/aifx/applications/app-b",
        json={"organization_id": org_id, "apm_id": "APM7", "acb_repo": "custom-acb"},
    )
    assert saved.status_code == 200, saved.text
    # A scoped admin who can see (but not change everywhere) app-b, with a
    # non-convention repository saved, can still preview the saved mapping.
    scoped = client(repo, who(scopes=SCOPED))
    response = scoped.post("/admin/aifx/applications/app-b/preview")
    assert response.status_code == 200, response.text


def test_preview_releases_read_transactions_before_reading_github(
    repo: FakeAifxRepo, monkeypatch: pytest.MonkeyPatch
) -> None:
    import apex.routers.aifx as aifx_router

    calls: list[str] = []
    released: list[tuple[Any, ...]] = []
    real_open = aifx_router.open_adapters
    real_read = aifx_router.read_sources

    async def fake_release(*owners: Any) -> None:
        calls.append("release")
        released.append(owners)

    async def recording_open(*args: Any, **kwargs: Any) -> Any:
        calls.append("open_adapters")
        return await real_open(*args, **kwargs)

    async def recording_read(*args: Any, **kwargs: Any) -> Any:
        calls.append("read_sources")
        return await real_read(*args, **kwargs)

    monkeypatch.setattr(aifx_router, "release_read_transactions", fake_release)
    monkeypatch.setattr(aifx_router, "open_adapters", recording_open)
    monkeypatch.setattr(aifx_router, "read_sources", recording_read)

    api = client(repo, who())
    _onboard(api)
    response = api.post("/admin/aifx/applications/app-a/preview")
    assert response.status_code == 200, response.text
    assert calls == ["release", "open_adapters", "read_sources"]
    (owners,) = released
    assert owners[0] is repo
    assert isinstance(owners[1], FakeCatalog)


def test_preview_reports_source_count_limit(
    repo: FakeAifxRepo, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("APEX_AIFX__MAX_SOURCES", "1")
    get_settings.cache_clear()
    api = client(repo, who())
    _onboard(api)
    response = api.post("/admin/aifx/applications/app-a/preview")
    assert response.status_code == 200, response.text
    preview = response.json()
    assert preview["ok"] is False
    assert preview["sources"] == []
    assert any("APEX_AIFX__MAX_SOURCES" in issue["message"] for issue in preview["issues"])


def test_preview_reports_phase_budget_overflow(
    repo: FakeAifxRepo, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("APEX_AIFX__MAX_PHASE_SYSTEM_CHARS", "1000")
    get_settings.cache_clear()
    api = client(repo, who())
    _onboard(api)
    response = api.post("/admin/aifx/applications/app-a/preview")
    assert response.status_code == 200, response.text
    preview = response.json()
    assert preview["ok"] is False
    # Every source still read fine; only the launch budget check fails.
    assert all(source["ok"] for source in preview["sources"])
    assert any("per-phase limit is 1,000" in issue["message"] for issue in preview["issues"])


def test_preview_reports_seal_snapshot_issues(
    repo: FakeAifxRepo, monkeypatch: pytest.MonkeyPatch
) -> None:
    import apex.routers.aifx as aifx_router
    from apex.services.aifx_context import AifxIssue, AifxUnavailableError

    seen: list[bool] = []

    def failing_seal(reads: Any, *, settings: Any, require_all: bool) -> Any:
        seen.append(require_all)
        raise AifxUnavailableError(
            [AifxIssue(repo="AIFx context", message="snapshot limit exceeded")]
        )

    monkeypatch.setattr(aifx_router, "seal_snapshot", failing_seal)
    api = client(repo, who())
    _onboard(api)
    preview = api.post("/admin/aifx/applications/app-a/preview").json()
    assert seen == [False]
    assert preview["ok"] is False
    assert [issue["message"] for issue in preview["issues"]] == ["snapshot limit exceeded"]


@pytest.mark.parametrize("name", ["", "   ", "Consumer\x00Mobility"])
def test_organization_update_rejects_blank_or_nul_names(repo: FakeAifxRepo, name: str) -> None:
    api = client(repo, who())
    org_id = _org(api)
    response = api.patch(f"/admin/aifx/organizations/{org_id}", json={"name": name})
    assert response.status_code == 422, response.text
    assert repo.orgs[org_id].name == ORG["name"]


def test_organization_update_strips_name(repo: FakeAifxRepo) -> None:
    api = client(repo, who())
    org_id = _org(api)
    response = api.patch(f"/admin/aifx/organizations/{org_id}", json={"name": "  Wireline  "})
    assert response.status_code == 200, response.text
    assert response.json()["name"] == "Wireline"


def _onboard_two(api: TestClient) -> None:
    org_id = _org(api)
    for app_id, apm in (("app-a", "APM0012"), ("app-b", "APM0013")):
        response = api.put(
            f"/admin/aifx/applications/{app_id}", json={"organization_id": org_id, "apm_id": apm}
        )
        assert response.status_code == 200, response.text


def test_list_applications_uses_one_batched_lookup(repo: FakeAifxRepo) -> None:
    api = client(repo, who())
    _onboard_two(api)
    listed = api.get("/admin/aifx/applications")
    assert listed.status_code == 200, listed.text
    assert [row["application_name"] for row in listed.json()] == ["Storefront", "Payments"]
    assert repo.batched_lookups == [["app-a", "app-b"]]


def test_list_applications_pages_with_limit_and_offset(repo: FakeAifxRepo) -> None:
    api = client(repo, who())
    _onboard_two(api)
    page = api.get("/admin/aifx/applications", params={"limit": 1, "offset": 1})
    assert page.status_code == 200, page.text
    assert [row["apm_id"] for row in page.json()] == ["APM0013"]
    assert repo.batched_lookups == [["app-b"]]
    first = api.get("/admin/aifx/applications", params={"limit": 1}).json()
    assert [row["apm_id"] for row in first] == ["APM0012"]


@pytest.mark.parametrize(
    "params", [{"limit": 0}, {"limit": 1001}, {"offset": -1}, {"limit": "many"}]
)
def test_list_applications_rejects_out_of_range_paging(
    repo: FakeAifxRepo, params: dict[str, Any]
) -> None:
    response = client(repo, who()).get("/admin/aifx/applications", params=params)
    assert response.status_code == 422, response.text


def test_list_applications_default_limit_is_500(
    repo: FakeAifxRepo, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[tuple[int | None, int]] = []
    real = repo.list_sources

    async def recording(
        application_ids: Sequence[str] | None = None, *, limit: int | None = None, offset: int = 0
    ) -> list[AifxApplicationSource]:
        seen.append((limit, offset))
        return await real(application_ids, limit=limit, offset=offset)

    monkeypatch.setattr(repo, "list_sources", recording)
    client(repo, who()).get("/admin/aifx/applications")
    assert seen == [(500, 0)]


def test_application_source_write_upper_cases_apm_id(repo: FakeAifxRepo) -> None:
    from apex.routers.aifx import ApplicationSourceWrite

    assert ApplicationSourceWrite(organization_id="org-1", apm_id="apm0012x").apm_id == "APM0012X"
    api = client(repo, who())
    response = api.put(
        "/admin/aifx/applications/app-a",
        json={"organization_id": _org(api), "apm_id": "apm0012"},
    )
    assert response.status_code == 200, response.text
    assert response.json()["apm_id"] == "APM0012"
    assert repo.sources["app-a"].apm_id == "APM0012"


# ── repository conflict messages ─────────────────────────────────────────────


class _PgDiag:
    def __init__(self, constraint_name: str | None) -> None:
        self.constraint_name = constraint_name


class _PgError(Exception):
    def __init__(self, message: str, constraint_name: str | None = None) -> None:
        super().__init__(message)
        self.diag = _PgDiag(constraint_name)


def _integrity(orig: Any) -> Any:
    from sqlalchemy.exc import IntegrityError

    return IntegrityError("INSERT ...", {}, orig)


ORG_TAKEN = "an AIFx organization with this name already exists"
APM_TAKEN = "this APM ID is already mapped to another application"
GENERIC = "AIFx onboarding changed while saving; reload and retry"


@pytest.mark.parametrize(
    ("orig", "expected"),
    [
        (_PgError("conflict", "uq_aifx_organizations_name"), ORG_TAKEN),
        (_PgError("conflict", "uq_aifx_application_sources_apm_id"), APM_TAKEN),
        (
            Exception(
                'duplicate key value violates unique constraint "uq_aifx_organizations_name"'
            ),
            ORG_TAKEN,
        ),
        (
            Exception(
                "duplicate key value violates unique constraint "
                '"uq_aifx_application_sources_apm_id"'
            ),
            APM_TAKEN,
        ),
        (Exception("UNIQUE constraint failed: aifx_organizations.name"), ORG_TAKEN),
        (Exception("UNIQUE constraint failed: aifx_application_sources.apm_id"), APM_TAKEN),
        (_PgError("insert or update violates foreign key", "fk_something"), GENERIC),
        (Exception("UNIQUE constraint failed: aifx_application_sources.application_id"), GENERIC),
        (Exception(), GENERIC),
        (None, GENERIC),
    ],
)
def test_conflict_message_always_returns_a_safe_message(orig: Any, expected: str) -> None:
    from apex.persistence.repositories.aifx import (
        _conflict_message,  # pyright: ignore[reportPrivateUsage]
    )

    assert _conflict_message(_integrity(orig)) == expected


# ── naming-convention length and shape (String(120) column) ─────────────────

# Ten placeholders save 20 characters once "{apm}" is swapped for "apm", so the
# substituted name is exactly 100 characters: the longest valid GitHub repo name.
PATTERN_120 = "{apm}" * 10 + "x" * 70
PATTERN_121 = PATTERN_120 + "y"
PATTERN_FIELDS = ["team_documents_repo_pattern", "acb_repo_pattern"]


def test_pattern_fixtures_have_the_intended_lengths() -> None:
    assert len(PATTERN_120) == 120
    assert len(PATTERN_121) == 121
    assert len(PATTERN_120.replace("{apm}", "apm")) == 100


@pytest.mark.parametrize("field", PATTERN_FIELDS)
def test_create_organization_rejects_a_pattern_longer_than_120(
    repo: FakeAifxRepo, field: str
) -> None:
    response = client(repo, who()).post(
        "/admin/aifx/organizations", json={**ORG, field: PATTERN_121}
    )
    assert response.status_code == 422, response.text
    assert "at most 120 characters" in response.text
    assert repo.orgs == {}


@pytest.mark.parametrize("field", PATTERN_FIELDS)
def test_create_organization_accepts_a_pattern_of_exactly_120(
    repo: FakeAifxRepo, field: str
) -> None:
    response = client(repo, who()).post(
        "/admin/aifx/organizations", json={**ORG, field: PATTERN_120}
    )
    assert response.status_code == 201, response.text
    assert response.json()[field] == PATTERN_120


@pytest.mark.parametrize("field", PATTERN_FIELDS)
def test_update_organization_rejects_a_pattern_longer_than_120(
    repo: FakeAifxRepo, field: str
) -> None:
    api = client(repo, who())
    org_id = _org(api)
    before = getattr(repo.orgs[org_id], field)
    response = api.patch(f"/admin/aifx/organizations/{org_id}", json={field: PATTERN_121})
    assert response.status_code == 422, response.text
    assert "at most 120 characters" in response.text
    assert getattr(repo.orgs[org_id], field) == before


@pytest.mark.parametrize("field", PATTERN_FIELDS)
def test_update_organization_accepts_a_pattern_of_exactly_120(
    repo: FakeAifxRepo, field: str
) -> None:
    api = client(repo, who())
    org_id = _org(api)
    response = api.patch(f"/admin/aifx/organizations/{org_id}", json={field: PATTERN_120})
    assert response.status_code == 200, response.text
    assert response.json()[field] == PATTERN_120
    assert getattr(repo.orgs[org_id], field) == PATTERN_120


@pytest.mark.parametrize("field", PATTERN_FIELDS)
def test_pattern_within_120_still_needs_a_valid_repo_name(repo: FakeAifxRepo, field: str) -> None:
    # 103 chars with one placeholder -> 101 after substitution: over GitHub's 100.
    too_long_name = "{apm}" + "x" * 98
    assert len(too_long_name) <= 120
    response = client(repo, who()).post(
        "/admin/aifx/organizations", json={**ORG, field: too_long_name}
    )
    assert response.status_code == 422, response.text


@pytest.mark.parametrize(
    "pattern",
    [
        "{apm} team documents",  # spaces are not valid in repo names
        "{apm}/acb",  # no nested paths
        "{APM}-acb",  # placeholder is case-sensitive
        "{ apm }-acb",
    ],
)
@pytest.mark.parametrize("method", ["post", "patch"])
def test_pattern_shape_is_validated_on_create_and_update(
    repo: FakeAifxRepo, pattern: str, method: str
) -> None:
    api = client(repo, who())
    if method == "post":
        response = api.post("/admin/aifx/organizations", json={**ORG, "acb_repo_pattern": pattern})
        assert response.status_code == 422, response.text
        assert repo.orgs == {}
    else:
        org_id = _org(api)
        response = api.patch(
            f"/admin/aifx/organizations/{org_id}", json={"acb_repo_pattern": pattern}
        )
        assert response.status_code == 422, response.text
        assert repo.orgs[org_id].acb_repo_pattern == "{apm}-acb"


@pytest.mark.parametrize("pattern", ["{apm}", "acb.{apm}_v2", "{apm}-{apm}"])
def test_minimal_and_multi_placeholder_patterns_are_accepted(
    repo: FakeAifxRepo, pattern: str
) -> None:
    response = client(repo, who()).post(
        "/admin/aifx/organizations", json={**ORG, "acb_repo_pattern": pattern}
    )
    assert response.status_code == 201, response.text
    assert response.json()["acb_repo_pattern"] == pattern


@pytest.mark.parametrize("field", PATTERN_FIELDS)
def test_empty_pattern_clears_the_convention(repo: FakeAifxRepo, field: str) -> None:
    api = client(repo, who())
    created = api.post("/admin/aifx/organizations", json={**ORG, field: ""})
    assert created.status_code == 201, created.text
    assert created.json()[field] is None
    org_id = _org(api)
    patched = api.patch(f"/admin/aifx/organizations/{org_id}", json={field: ""})
    assert patched.status_code == 200, patched.text
    assert patched.json()[field] is None
    assert getattr(repo.orgs[org_id], field) is None


@pytest.mark.parametrize("name", ["", "   ", "Consumer\x00Mobility", "x" * 256])
def test_create_organization_rejects_blank_nul_or_oversized_names(
    repo: FakeAifxRepo, name: str
) -> None:
    response = client(repo, who()).post("/admin/aifx/organizations", json={**ORG, "name": name})
    assert response.status_code == 422, response.text
    assert repo.orgs == {}


def test_create_organization_strips_name(repo: FakeAifxRepo) -> None:
    response = client(repo, who()).post(
        "/admin/aifx/organizations", json={**ORG, "name": "  Wireline  "}
    )
    assert response.status_code == 201, response.text
    assert response.json()["name"] == "Wireline"


def test_organization_name_of_255_characters_is_accepted(repo: FakeAifxRepo) -> None:
    api = client(repo, who())
    created = api.post("/admin/aifx/organizations", json={**ORG, "name": "n" * 255})
    assert created.status_code == 201, created.text
    org_id = created.json()["id"]
    too_long = api.patch(f"/admin/aifx/organizations/{org_id}", json={"name": "n" * 256})
    assert too_long.status_code == 422, too_long.text
    assert repo.orgs[org_id].name == "n" * 255

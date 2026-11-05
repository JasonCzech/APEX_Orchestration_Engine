"""AIFx manifests, snapshot integrity, source planning and pinned reads."""

from __future__ import annotations

from typing import Any

import pytest

from apex.adapters.stubs.source_control import StubSourceControlAdapter
from apex.domain.aifx import (
    AifxEntryType,
    AifxManifestError,
    AifxRole,
    AifxSnapshotStatus,
    AifxSnapshotV1,
    parse_manifest,
    suggest_repo,
    text_sha256,
)
from apex.domain.integrations import FileContent, RepoRef
from apex.domain.pipeline import Phase
from apex.persistence.models import AifxApplicationSource, AifxOrganization
from apex.services.aifx_context import (
    AifxUnavailableError,
    SourcePlan,
    credential_lines,
    plan_sources,
    read_source,
    read_sources,
    seal_snapshot,
    web_base_for,
)
from apex.settings import AifxSettings

SETTINGS = AifxSettings()


def org(**overrides: Any) -> AifxOrganization:
    fields: dict[str, Any] = {
        "id": "org-1",
        "name": "Consumer Mobility",
        "connection_id": "conn-ghe",
        "ghe_owner": "aifx",
        "global_rules_repo": "global-rules",
        "default_ref": "main",
        "enabled": True,
    }
    return AifxOrganization(**{**fields, **overrides})


def mapping(app_id: str, apm: str, **overrides: Any) -> AifxApplicationSource:
    fields: dict[str, Any] = {
        "application_id": app_id,
        "organization_id": "org-1",
        "apm_id": apm,
        "team_documents_repo": f"{apm}-team-documents",
        "acb_repo": f"{apm}-acb",
        "ref": None,
        "enabled": True,
    }
    return AifxApplicationSource(**{**fields, **overrides})


# --- manifest ---------------------------------------------------------------------


def test_manifest_parses_phases_and_requires_skill_descriptions() -> None:
    manifest = parse_manifest(
        "schema: aifx/v1\nrole: global-rules\nentries:\n"
        "  - {id: r, type: rule, path: rules/r.md, title: R}\n"
        "  - {id: s, type: skill, path: skills/s.md, phases: [test_planning], title: S,"
        " description: does s}\n",
        expected_role=AifxRole.GLOBAL_RULES,
    )
    assert [entry.type for entry in manifest.entries] == [AifxEntryType.RULE, AifxEntryType.SKILL]
    assert manifest.entries[0].phases == "all"
    assert manifest.entries[1].phases == [Phase.TEST_PLANNING]
    with pytest.raises(AifxManifestError, match="description"):
        parse_manifest(
            "schema: aifx/v1\nrole: global-rules\nentries:\n"
            "  - {id: s, type: skill, path: s.md, title: S}\n"
        )


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ("schema: aifx/v2\nrole: global-rules\n", "schema"),
        ("schema: aifx/v1\nrole: environment-context\n", "not supported yet"),
        ("schema: aifx/v1\nrole: team-documents\n", "mapped as 'global-rules'"),
        ("a: &x 1\nb: *x\n", "aliases"),
        ("schema: aifx/v1\nschema: aifx/v1\n", "duplicate key"),
        ("- just\n- a list\n", "must be a mapping"),
        (
            "schema: aifx/v1\nrole: global-rules\nentries:\n"
            "  - {id: a, type: rule, path: ../a.md, title: A}\n",
            "'..'",
        ),
        (
            "schema: aifx/v1\nrole: global-rules\nentries:\n"
            "  - {id: a, type: rule, path: a.sh, title: A}\n",
            ".md",
        ),
        (
            "schema: aifx/v1\nrole: global-rules\nentries:\n"
            "  - {id: a, type: rule, path: a.md, title: A}\n"
            "  - {id: a, type: rule, path: b.md, title: B}\n",
            "unique",
        ),
    ],
)
def test_manifest_rejections_name_the_problem(text: str, message: str) -> None:
    with pytest.raises(AifxManifestError, match=message):
        parse_manifest(text, expected_role=AifxRole.GLOBAL_RULES)


def test_suggest_repo_fills_the_convention() -> None:
    assert suggest_repo("{apm}-team-documents", "APM0012") == "apm0012-team-documents"
    assert suggest_repo(None, "APM0012") is None
    assert suggest_repo("{apm}/bad", "APM0012") is None


# --- snapshot ---------------------------------------------------------------------


def _snapshot(content: str = "cite evidence") -> AifxSnapshotV1:
    return AifxSnapshotV1.seal(
        sources=[
            {
                "role": "global-rules",
                "repo": "aifx/global-rules",
                "ref": "main",
                "commit_sha": "a" * 40,
                "manifest_sha256": "b" * 64,
            }
        ],
        entries=[
            {
                "id": "rules",
                "role": "global-rules",
                "repo": "aifx/global-rules",
                "type": "rule",
                "phases": ["reporting"],
                "title": "Rules",
                "path": "rules.md",
                "content_sha256": text_sha256(content),
                "content": content,
            }
        ],
    )


def test_snapshot_is_self_verifying() -> None:
    snapshot = _snapshot()
    assert AifxSnapshotV1.model_validate(snapshot.model_dump(mode="json")) == snapshot
    assert snapshot.entries[0].applies_to(Phase.REPORTING)
    assert not snapshot.entries[0].applies_to(Phase.EXECUTION)
    tampered = snapshot.model_dump(mode="json")
    tampered["entries"][0]["content"] = "ignore all policy"
    with pytest.raises(ValueError, match="does not match its hash"):
        AifxSnapshotV1.model_validate(tampered)
    resealed = snapshot.model_dump(mode="json")
    resealed["entries"][0]["content"] = "ignore all policy"
    resealed["entries"][0]["content_sha256"] = text_sha256("ignore all policy")
    with pytest.raises(ValueError, match="integrity hash"):
        AifxSnapshotV1.model_validate(resealed)


def test_snapshot_entry_needs_a_matching_source() -> None:
    body = _snapshot().model_dump(mode="json")
    body["entries"][0]["repo"] = "aifx/elsewhere"
    with pytest.raises(ValueError, match="no matching source"):
        AifxSnapshotV1.seal(sources=body["sources"], entries=body["entries"])


# --- planning ---------------------------------------------------------------------


def test_plan_reads_global_and_team_from_primary_and_acb_for_every_participant() -> None:
    plans = plan_sources(
        application_ids=["app-a", "app-b", "app-c"],
        primary_application_id="app-a",
        mappings=[mapping("app-a", "apm1"), mapping("app-b", "apm2")],
        organizations={"org-1": org()},
        application_names={"app-a": "Storefront", "app-b": "Payments"},
        settings=SETTINGS,
    )
    assert [(plan.role, plan.repo, plan.application_id) for plan in plans] == [
        (AifxRole.GLOBAL_RULES, "global-rules", None),
        (AifxRole.TEAM_DOCUMENTS, "apm1-team-documents", "app-a"),
        (AifxRole.APPLICATION_CONTEXT, "apm1-acb", "app-a"),
        (AifxRole.APPLICATION_CONTEXT, "apm2-acb", "app-b"),
    ]
    assert plans[3].application_name == "Payments"
    assert all(plan.ref == "main" for plan in plans)


def test_plan_without_onboarded_primary_skips_team_documents() -> None:
    plans = plan_sources(
        application_ids=["app-a", "app-b"],
        primary_application_id="app-a",
        mappings=[mapping("app-b", "apm2", ref="release")],
        organizations={"org-1": org()},
        application_names={},
        settings=SETTINGS,
    )
    assert [plan.role for plan in plans] == [AifxRole.GLOBAL_RULES, AifxRole.APPLICATION_CONTEXT]
    assert plans[1].ref == "release"


def test_plan_ignores_disabled_mappings_and_organizations() -> None:
    assert (
        plan_sources(
            application_ids=["app-a"],
            primary_application_id="app-a",
            mappings=[mapping("app-a", "apm1", enabled=False)],
            organizations={"org-1": org()},
            application_names={},
            settings=SETTINGS,
        )
        == []
    )
    assert (
        plan_sources(
            application_ids=["app-a"],
            primary_application_id="app-a",
            mappings=[mapping("app-a", "apm1")],
            organizations={"org-1": org(enabled=False)},
            application_names={},
            settings=SETTINGS,
        )
        == []
    )


# --- reading ----------------------------------------------------------------------


def plan(role: AifxRole, repo: str, app_id: str | None = None) -> SourcePlan:
    return SourcePlan(
        role=role, owner="aifx", repo=repo, ref="main", connection_id="conn", application_id=app_id
    )


async def test_read_source_pins_a_commit_and_reads_declared_entries() -> None:
    read = await read_source(
        StubSourceControlAdapter(),
        plan(AifxRole.GLOBAL_RULES, "global-rules"),
        settings=SETTINGS,
        web_base="https://ghe.example.test",
    )
    assert read.ok
    assert read.source is not None
    assert len(read.source.commit_sha) == 40
    assert (
        read.source.web_url
        == f"https://ghe.example.test/aifx/global-rules/tree/{read.source.commit_sha}"
    )
    assert {entry.type for entry in read.entries} == {
        AifxEntryType.RULE,
        AifxEntryType.SKILL,
        AifxEntryType.MEMORY,
    }
    assert all(entry.content_sha256 == text_sha256(entry.content) for entry in read.entries)


class FakeRepo:
    """Source control serving a hand-written manifest and files."""

    def __init__(self, files: dict[str, str]) -> None:
        self.files = files
        self.reads: list[tuple[str, str]] = []

    async def resolve_commit(self, repo: RepoRef, ref: str = "HEAD") -> str:
        return "c" * 40

    async def get_file(self, repo: RepoRef, path: str, ref: str = "HEAD") -> FileContent:
        self.reads.append((path, ref))
        if path not in self.files:
            raise KeyError(f"aifx/{repo.name}:{path}@{ref} is unavailable to the GitHub App")
        return FileContent(path=path, ref=ref, text=self.files[path])


MANIFEST = (
    "schema: aifx/v1\nrole: application-context\nentries:\n"
    "  - {id: arch, type: context, path: context/arch.md, title: Architecture}\n"
    "  - {id: ops, type: memory, path: memories/ops.md, title: Ops}\n"
)


async def test_reads_happen_at_the_pinned_commit_and_credentials_are_reported_by_line() -> None:
    adapter = FakeRepo(
        {
            "aifx.yaml": MANIFEST,
            "context/arch.md": "# Architecture\n\nWeb tier, then services.\n",
            "memories/ops.md": "# Ops\nrotate keys monthly\npassword = hunter2\n",
        }
    )
    read = await read_source(
        adapter, plan(AifxRole.APPLICATION_CONTEXT, "apm1-acb", "app-a"), settings=SETTINGS
    )
    assert all(ref == "c" * 40 for _path, ref in adapter.reads)
    assert [entry.id for entry in read.entries] == ["arch"]
    [issue] = read.issues
    assert issue.path == "memories/ops.md"
    assert issue.lines == (3,)
    assert "hunter2" not in issue.text()
    assert "credential" in issue.message


async def test_missing_manifest_role_mismatch_and_oversize_files_are_issues() -> None:
    missing = await read_source(
        FakeRepo({}), plan(AifxRole.APPLICATION_CONTEXT, "apm1-acb"), settings=SETTINGS
    )
    assert missing.source is None and "aifx.yaml" in missing.issues[0].message

    mismatch = await read_source(
        FakeRepo({"aifx.yaml": MANIFEST}),
        plan(AifxRole.GLOBAL_RULES, "global-rules"),
        settings=SETTINGS,
    )
    assert "mapped as 'global-rules'" in mismatch.issues[0].message

    small = AifxSettings(max_file_chars=1_000)
    big = await read_source(
        FakeRepo({"aifx.yaml": MANIFEST, "context/arch.md": "x" * 1_001, "memories/ops.md": "ok"}),
        plan(AifxRole.APPLICATION_CONTEXT, "apm1-acb"),
        settings=small,
    )
    assert "exceeds 1,000 characters" in big.issues[0].message
    assert [entry.id for entry in big.entries] == ["ops"]


def test_credential_lines_find_multiline_key_blocks() -> None:
    text = "intro\n-----BEGIN PRIVATE KEY-----\nMIIBVQ\n-----END PRIVATE KEY-----\n"
    assert credential_lines(text) == (2,)
    assert credential_lines("all clear\n") == ()


async def test_read_sources_reports_unavailable_connections() -> None:
    reads = await read_sources(
        [plan(AifxRole.GLOBAL_RULES, "global-rules")],
        {},
        settings=SETTINGS,
        connection_failures={"conn": "its connection could not be opened: denied"},
    )
    assert reads[0].issues[0].message.endswith("denied")


# --- sealing ----------------------------------------------------------------------


async def _stub_reads() -> list[Any]:
    adapter = StubSourceControlAdapter()
    return await read_sources(
        [
            plan(AifxRole.GLOBAL_RULES, "global-rules"),
            plan(AifxRole.APPLICATION_CONTEXT, "apm1-acb", "app-a"),
        ],
        {"conn": adapter},
        settings=SETTINGS,
    )


async def test_seal_pins_every_source() -> None:
    snapshot = seal_snapshot(await _stub_reads(), settings=SETTINGS, require_all=True)
    assert snapshot is not None
    assert snapshot.status is AifxSnapshotStatus.PINNED
    assert len(snapshot.sources) == 2
    assert snapshot.application_ids() == {"app-a"}


async def test_seal_refuses_partial_context_when_required_and_degrades_otherwise() -> None:
    broken = await read_sources(
        [plan(AifxRole.GLOBAL_RULES, "global-rules"), plan(AifxRole.APPLICATION_CONTEXT, "x-acb")],
        {"conn": FakeRepo({})},
        settings=SETTINGS,
    )
    with pytest.raises(AifxUnavailableError, match="aifx/global-rules"):
        seal_snapshot(broken, settings=SETTINGS, require_all=True)
    degraded = seal_snapshot(broken, settings=SETTINGS, require_all=False)
    assert degraded is not None
    assert degraded.status is AifxSnapshotStatus.DEGRADED
    assert degraded.sources == [] and len(degraded.issues) == 2


async def test_seal_enforces_the_snapshot_budget() -> None:
    # The fixture is far below the 10,000-character settings floor, so bypass
    # validation to put the limit under it.
    tiny = SETTINGS.model_copy(update={"max_snapshot_chars": 100})
    with pytest.raises(AifxUnavailableError, match="snapshot limit"):
        seal_snapshot(await _stub_reads(), settings=tiny, require_all=True)


def test_seal_with_nothing_mapped_returns_none() -> None:
    assert seal_snapshot([], settings=SETTINGS, require_all=True) is None


def test_web_base_for_github_hosts() -> None:
    assert web_base_for("github_enterprise", {"base_url": "https://ghe.example.test/api/v3"}) == (
        "https://ghe.example.test"
    )
    assert web_base_for("github_enterprise", {"base_url": "https://api.github.com"}) == (
        "https://github.com"
    )
    assert web_base_for("stub", {}) is None


# --- launch -----------------------------------------------------------------------


class StubResolver:
    def __init__(self) -> None:
        self.opened = 0

    async def resolve_metadata(self, kind: Any, connection_id: str, project_id: Any) -> Any:
        from types import SimpleNamespace

        return SimpleNamespace(
            internal_project_id=None, config=SimpleNamespace(provider="stub", options={})
        )

    async def build_from_metadata(self, metadata: Any) -> Any:
        self.opened += 1
        return StubSourceControlAdapter()


@pytest.fixture
def onboarded(monkeypatch: pytest.MonkeyPatch) -> StubResolver:
    from apex.services import aifx_context, connections
    from apex.settings import get_settings

    monkeypatch.setenv("APEX_AIFX_ENABLED", "true")
    get_settings.cache_clear()

    async def load(application_ids: Any) -> Any:
        rows = [mapping("app-a", "apm1")] if "app-a" in application_ids else []
        return rows, {"org-1": org()}, {"app-a": "Storefront"}

    resolver = StubResolver()
    monkeypatch.setattr(aifx_context, "load_onboarding", load)
    monkeypatch.setattr(connections, "get_connection_resolver", lambda: resolver)
    return resolver


async def test_launch_pins_a_snapshot_the_run_contract_accepts(onboarded: StubResolver) -> None:
    from apex.graphs.pipeline.configurable import PipelineConfigurable
    from apex.services.aifx_context import prepare_aifx_snapshot

    configurable: dict[str, Any] = {"project_id": "proj-1", "app_id": "app-a"}
    await prepare_aifx_snapshot(configurable)

    snapshot = AifxSnapshotV1.model_validate(configurable["aifx_snapshot"])
    assert snapshot.status is AifxSnapshotStatus.PINNED
    assert [source.role for source in snapshot.sources] == [
        AifxRole.GLOBAL_RULES,
        AifxRole.TEAM_DOCUMENTS,
        AifxRole.APPLICATION_CONTEXT,
    ]
    assert snapshot.sources[2].application_name == "Storefront"
    cfg = PipelineConfigurable.model_validate(configurable)
    assert cfg.aifx_snapshot == snapshot
    assert onboarded.opened == 1  # one adapter per connection, not per source


async def test_launch_without_onboarded_applications_pins_nothing(onboarded: StubResolver) -> None:
    from apex.services.aifx_context import prepare_aifx_snapshot

    configurable: dict[str, Any] = {"project_id": "proj-1", "app_id": "app-z"}
    await prepare_aifx_snapshot(configurable)
    assert "aifx_snapshot" not in configurable
    assert onboarded.opened == 0


async def test_launch_is_a_noop_when_aifx_is_disabled(
    onboarded: StubResolver, monkeypatch: pytest.MonkeyPatch
) -> None:
    from apex.services.aifx_context import prepare_aifx_snapshot
    from apex.settings import get_settings

    monkeypatch.setenv("APEX_AIFX_ENABLED", "false")
    get_settings.cache_clear()
    configurable: dict[str, Any] = {"project_id": "proj-1", "app_id": "app-a"}
    await prepare_aifx_snapshot(configurable)
    assert "aifx_snapshot" not in configurable


async def test_locked_environments_refuse_to_launch_on_partial_context(
    onboarded: StubResolver, monkeypatch: pytest.MonkeyPatch
) -> None:
    from apex.services import aifx_context
    from apex.settings import ApexSettings

    async def broken(application_ids: Any) -> Any:
        return [mapping("app-a", "apm1", acb_repo="missing-acb")], {"org-1": org()}, {}

    class MissingFiles(StubSourceControlAdapter):
        async def get_file(self, repo: RepoRef, path: str, ref: str = "HEAD") -> FileContent:
            if repo.name == "missing-acb":
                raise KeyError(f"aifx/{repo.name}:{path}@{ref} is unavailable to the GitHub App")
            return await super().get_file(repo, path, ref)

    async def build(metadata: Any) -> Any:
        return MissingFiles()

    monkeypatch.setattr(aifx_context, "load_onboarding", broken)
    monkeypatch.setattr(onboarded, "build_from_metadata", build)
    monkeypatch.setattr(ApexSettings, "is_locked_down", property(lambda self: True))

    configurable: dict[str, Any] = {"project_id": "proj-1", "app_id": "app-a"}
    with pytest.raises(AifxUnavailableError, match="aifx/missing-acb"):
        await aifx_context.prepare_aifx_snapshot(configurable)
    assert "aifx_snapshot" not in configurable

    monkeypatch.setattr(ApexSettings, "is_locked_down", property(lambda self: False))
    await aifx_context.prepare_aifx_snapshot(configurable)
    degraded = AifxSnapshotV1.model_validate(configurable["aifx_snapshot"])
    assert degraded.status is AifxSnapshotStatus.DEGRADED
    assert "missing-acb" in degraded.issues[0]


def test_run_contract_rejects_snapshots_for_other_applications() -> None:
    from apex.graphs.pipeline.configurable import PipelineConfigurable

    body = _snapshot().model_dump(mode="json")
    body["sources"][0]["application_id"] = "app-other"
    body["entries"][0]["application_id"] = "app-other"
    foreign = AifxSnapshotV1.seal(sources=body["sources"], entries=body["entries"])
    with pytest.raises(ValueError, match="outside this test"):
        PipelineConfigurable.model_validate(
            {"project_id": "proj-1", "app_id": "app-a", "aifx_snapshot": foreign.model_dump()}
        )


def test_launch_requests_cannot_supply_a_snapshot() -> None:
    from apex.routers.pipelines import StartPipelineRequest

    with pytest.raises(ValueError, match="server-owned"):
        StartPipelineRequest.model_validate(
            {"title": "t", "configurable": {"aifx_snapshot": _snapshot().model_dump(mode="json")}}
        )


def test_oversized_launch_configuration_gets_a_clear_error() -> None:
    from apex.services.aifx_context import check_configurable_size

    with pytest.raises(AifxUnavailableError, match="character"):
        check_configurable_size({"pre_execution_context": ["x" * 950_000]})
    check_configurable_size({"pre_execution_context": ["x" * 1_000]})


# --- review fixes: limits, budgets, gate metadata and public projection ----------


def test_plan_reads_an_acb_shared_by_two_participants_once() -> None:
    plans = plan_sources(
        application_ids=["app-a", "app-b", "app-c"],
        primary_application_id="app-a",
        mappings=[
            mapping("app-a", "apm1", acb_repo="shared-acb"),
            mapping("app-b", "apm2", acb_repo="Shared-ACB"),  # same repo, other case
            mapping("app-c", "apm3", acb_repo="shared-acb", ref="release"),  # other ref
        ],
        organizations={"org-1": org()},
        application_names={},
        settings=SETTINGS,
    )
    acb = [p for p in plans if p.role is AifxRole.APPLICATION_CONTEXT]
    # One read per repository, at the first participant's ref: entry ids
    # ("owner/repo:id") must stay unique in the audit and at the gate.
    assert [(p.repo, p.ref, p.application_id) for p in acb] == [("shared-acb", "main", "app-a")]


def test_plan_no_longer_truncates_to_max_sources() -> None:
    few = AifxSettings(max_sources=2)
    plans = plan_sources(
        application_ids=["app-a", "app-b", "app-c"],
        primary_application_id="app-a",
        mappings=[mapping("app-a", "apm1"), mapping("app-b", "apm2"), mapping("app-c", "apm3")],
        organizations={"org-1": org()},
        application_names={},
        settings=few,
    )
    assert len(plans) == 5


def test_check_source_count_refuses_past_the_limit_and_names_the_setting() -> None:
    from apex.services.aifx_context import check_source_count

    plans = [plan(AifxRole.APPLICATION_CONTEXT, f"apm{i}-acb", f"app-{i}") for i in range(3)]
    check_source_count(plans, AifxSettings(max_sources=3))
    with pytest.raises(AifxUnavailableError, match="APEX_AIFX__MAX_SOURCES") as info:
        check_source_count(plans, AifxSettings(max_sources=2))
    assert "3 repositories" in str(info.value)


async def test_launch_checks_the_source_count_before_reading(
    onboarded: StubResolver, monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest
) -> None:
    from apex.services import aifx_context
    from apex.settings import get_settings

    monkeypatch.setenv("APEX_AIFX__MAX_SOURCES", "2")
    get_settings.cache_clear()
    request.addfinalizer(get_settings.cache_clear)
    configurable: dict[str, Any] = {"project_id": "proj-1", "app_id": "app-a"}
    with pytest.raises(AifxUnavailableError, match="APEX_AIFX__MAX_SOURCES"):
        await aifx_context.prepare_aifx_snapshot(configurable)
    assert onboarded.opened == 0
    assert "aifx_snapshot" not in configurable


def _source(repo: str, role: str = "application-context", app_id: str | None = None) -> Any:
    from apex.domain.aifx import AifxSource

    return AifxSource.model_validate(
        {
            "role": role,
            "application_id": app_id,
            "repo": repo,
            "ref": "main",
            "commit_sha": "a" * 40,
            "manifest_sha256": "b" * 64,
        }
    )


def _entry(
    repo: str,
    entry_id: str,
    *,
    role: str = "application-context",
    app_id: str | None = None,
    entry_type: str = "context",
    phases: Any = "all",
    content: str = "x",
) -> Any:
    from apex.domain.aifx import AifxEntry

    return AifxEntry.model_validate(
        {
            "id": entry_id,
            "role": role,
            "application_id": app_id,
            "repo": repo,
            "type": entry_type,
            "phases": phases,
            "title": entry_id,
            "description": "does it" if entry_type == "skill" else None,
            "path": f"{entry_id}.md",
            "content_sha256": text_sha256(content),
            "content": content,
        }
    )


def _read(repo: str, count: int, app_id: str) -> Any:
    from apex.services.aifx_context import SourceRead

    return SourceRead(
        plan=plan(AifxRole.APPLICATION_CONTEXT, repo.split("/")[1], app_id),
        source=_source(repo, app_id=app_id),
        entries=[_entry(repo, f"e{i}", app_id=app_id) for i in range(count)],
    )


def test_seal_refuses_too_many_entries_with_an_aifx_error() -> None:
    from apex.domain.aifx import MAX_SNAPSHOT_ENTRIES

    reads = [_read(f"aifx/apm{i}-acb", 50, f"app-{i}") for i in range(9)]
    assert sum(len(r.entries) for r in reads) > MAX_SNAPSHOT_ENTRIES
    with pytest.raises(AifxUnavailableError, match="400-entry snapshot limit"):
        seal_snapshot(reads, settings=SETTINGS, require_all=True)
    # Exactly at the limit still seals.
    at_limit = [_read(f"aifx/apm{i}-acb", 50, f"app-{i}") for i in range(8)]
    snapshot = seal_snapshot(at_limit, settings=SETTINGS, require_all=True)
    assert snapshot is not None and len(snapshot.entries) == MAX_SNAPSHOT_ENTRIES


def test_seal_wraps_snapshot_validation_errors() -> None:
    # Two reads pinning the same source key make AifxSnapshotV1 reject the body.
    twice = [_read("aifx/apm1-acb", 1, "app-a"), _read("aifx/apm1-acb", 0, "app-a")]
    with pytest.raises(AifxUnavailableError, match="could not be sealed") as info:
        seal_snapshot(twice, settings=SETTINGS, require_all=True)
    assert info.value.__cause__ is None
    assert not isinstance(info.value, ValueError)


def _seal(sources: list[Any], entries: list[Any], **fields: Any) -> AifxSnapshotV1:
    return AifxSnapshotV1.seal(sources=sources, entries=entries, **fields)


def test_check_phase_budgets_checks_every_phase_by_default() -> None:
    from apex.services.aifx_context import MAX_PHASE_ENTRIES, check_phase_budgets

    repos = [f"aifx/apm{i}-acb" for i in range(2)]
    sources = [_source(repo, app_id=f"app-{i}") for i, repo in enumerate(repos)]
    entries = [
        _entry(repo, f"e{n}", app_id=f"app-{i}", phases=[Phase.POSTMORTEM.value])
        for i, repo in enumerate(repos)
        for n in range(150)
    ]
    assert len(entries) > MAX_PHASE_ENTRIES
    snapshot = _seal(sources, entries)
    check_phase_budgets(snapshot, [Phase.STORY_ANALYSIS, Phase.EXECUTION])
    with pytest.raises(AifxUnavailableError, match="300 entries apply to postmortem"):
        check_phase_budgets(snapshot)


def test_check_phase_budgets_refuses_a_phase_over_its_character_budget() -> None:
    from apex.services.aifx_context import check_phase_budgets
    from apex.settings import get_settings

    get_settings.cache_clear()
    limit = get_settings().aifx.max_phase_system_chars
    repo = "aifx/apm1-acb"
    snapshot = _seal(
        [_source(repo, app_id="app-a")],
        [_entry(repo, "big", app_id="app-a", phases=[Phase.REPORTING.value], content="y" * limit)],
    )
    check_phase_budgets(snapshot, [Phase.EXECUTION])
    with pytest.raises(AifxUnavailableError, match="AIFx instructions for reporting"):
        check_phase_budgets(snapshot)


def test_gate_metadata_caps_lists_but_reports_totals() -> None:
    from apex.services.aifx_context import gate_metadata

    repo = "aifx/apm1-acb"
    snapshot = _seal(
        [_source(repo, app_id="app-a")],
        [_entry(repo, f"e{n}", app_id="app-a") for n in range(70)],
        status=AifxSnapshotStatus.DEGRADED,
        issues=[f"issue {n}" for n in range(10)],
    )
    meta = gate_metadata(snapshot, Phase.EXECUTION)
    assert meta is not None
    assert len(meta["entries"]) == 64
    assert len(meta["issues"]) == 8
    assert meta["entry_count"] == 70
    assert meta["issue_count"] == 10
    assert all("content" not in entry for entry in meta["entries"])
    assert gate_metadata(None, Phase.EXECUTION) is None


def test_render_leads_acb_only_phases_with_the_preamble() -> None:
    from apex.domain.aifx import render_aifx

    repo = "aifx/apm1-acb"
    snapshot = _seal(
        [_source(repo, app_id="app-a")],
        [
            _entry(repo, "arch", app_id="app-a", content="web tier"),
            _entry(
                repo,
                "ops",
                app_id="app-a",
                entry_type="memory",
                phases=[Phase.REPORTING.value],
                content="ops memory",
            ),
        ],
    )
    rendered = render_aifx(
        snapshot,
        Phase.EXECUTION,
        preamble="PREAMBLE TEXT",
        max_system_chars=60_000,
        max_memory_chars=30_000,
    )
    assert rendered is not None
    assert rendered.system_head == "=== AIFX CONTEXT SERVICES ===\nPREAMBLE TEXT"
    assert "web tier" in rendered.system_application
    assert "AIFX APPLICATION CONTEXT (ACB)" in rendered.system_application

    # A phase where only a memory applies has no system content and no header.
    memory_only = _seal(
        [_source(repo, app_id="app-a")],
        [_entry(repo, "ops", app_id="app-a", entry_type="memory", content="ops memory")],
    )
    rendered = render_aifx(
        memory_only,
        Phase.EXECUTION,
        preamble="PREAMBLE TEXT",
        max_system_chars=60_000,
        max_memory_chars=30_000,
    )
    assert rendered is not None
    assert rendered.system_head == ""
    assert rendered.system_application == ""
    assert len(rendered.memories) == 1


def _gate_payload() -> dict[str, Any]:
    from apex.services.aifx_context import gate_metadata

    repo = "aifx/apm1-acb"
    snapshot = _seal(
        [_source(repo, app_id="app-a")],
        [_entry(repo, f"e{n}", app_id="app-a") for n in range(70)],
        status=AifxSnapshotStatus.DEGRADED,
        issues=[f"issue {n}" for n in range(10)],
    )
    meta = gate_metadata(snapshot, Phase.EXECUTION)
    assert meta is not None
    return meta


def test_public_aifx_projects_totals() -> None:
    from apex.services.pipeline_public import _public_aifx  # pyright: ignore[reportPrivateUsage]

    projected = _public_aifx(_gate_payload())
    assert projected is not None
    assert projected["entry_count"] == 70
    assert projected["issue_count"] == 10
    assert len(projected["entries"]) == 64

    legacy = _gate_payload()
    del legacy["entry_count"], legacy["issue_count"]
    projected = _public_aifx(legacy)
    assert projected is not None
    assert "entry_count" not in projected and "issue_count" not in projected

    exact = _gate_payload()
    exact["entry_count"], exact["issue_count"] = 64, 8
    assert _public_aifx(exact) is not None
    at_max = _gate_payload()
    at_max["entry_count"] = 10_000
    assert _public_aifx(at_max) is not None


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("entry_count", "70"),
        ("entry_count", 70.0),
        ("entry_count", True),
        ("issue_count", False),
        ("entry_count", -1),
        ("entry_count", 63),  # fewer than the 64 entries shown
        ("issue_count", 7),  # fewer than the 8 issues shown
        ("entry_count", 10_001),
    ],
)
def test_public_aifx_drops_the_block_on_invalid_totals(key: str, value: Any) -> None:
    from apex.services.pipeline_public import _public_aifx  # pyright: ignore[reportPrivateUsage]

    payload = _gate_payload()
    payload[key] = value
    assert _public_aifx(payload) is None


# --- connection failures never leak resolver text ---------------------------------

_LEAKY = (
    "secret env:APEX_INTEGRATION_GHE_KEY unresolved: APEX_INTEGRATION_GHE_KEY is not set "
    "for project proj-secret-42"
)
_LEAK_MARKERS = ("env:", "APEX_INTEGRATION_GHE_KEY", "proj-secret-42", "unresolved")
_GENERIC = "its connection could not be opened; test it on the Connections page"


class LeakyResolver:
    """A resolver whose errors name a secret_ref, an env var and a project id."""

    def __init__(self, *, fail_in: str = "", project_id: str | None = None) -> None:
        self.fail_in = fail_in
        self.project_id = project_id
        self.built = 0

    async def resolve_metadata(self, kind: Any, connection_id: str, project_id: Any) -> Any:
        from types import SimpleNamespace

        if self.fail_in == "resolve":
            raise RuntimeError(_LEAKY)
        return SimpleNamespace(
            internal_project_id=self.project_id,
            config=SimpleNamespace(provider="stub", options={}),
        )

    async def build_from_metadata(self, metadata: Any) -> Any:
        if self.fail_in == "build":
            raise KeyError(_LEAKY)
        self.built += 1
        return StubSourceControlAdapter()


def _assert_no_leak(text: str) -> None:
    for marker in _LEAK_MARKERS:
        assert marker not in text, f"{marker!r} leaked into {text!r}"


@pytest.mark.parametrize(
    "resolver",
    [
        pytest.param(LeakyResolver(fail_in="resolve"), id="resolve_metadata-raises"),
        pytest.param(LeakyResolver(fail_in="build"), id="build_from_metadata-raises"),
        pytest.param(LeakyResolver(project_id="proj-secret-42"), id="project-scoped"),
    ],
)
async def test_open_adapters_replaces_resolver_errors_with_a_fixed_message(
    resolver: LeakyResolver,
) -> None:
    from apex.services.aifx_context import open_adapters

    adapters, web_bases, failures = await open_adapters(resolver, ["conn", "conn"])
    assert adapters == {} and web_bases == {}
    assert failures == {"conn": _GENERIC}
    assert resolver.built == 0
    _assert_no_leak(failures["conn"])

    plans = [plan(AifxRole.GLOBAL_RULES, "global-rules")]
    reads = await read_sources(plans, adapters, settings=SETTINGS, connection_failures=failures)
    [issue] = reads[0].issues
    assert issue.message == _GENERIC
    _assert_no_leak(issue.text())

    degraded = seal_snapshot(reads, settings=SETTINGS, require_all=False)
    assert degraded is not None
    assert degraded.status is AifxSnapshotStatus.DEGRADED
    assert degraded.issues and all(_GENERIC in text for text in degraded.issues)
    for text in degraded.issues:
        _assert_no_leak(text)
    _assert_no_leak(degraded.model_dump_json())

    with pytest.raises(AifxUnavailableError) as info:
        seal_snapshot(reads, settings=SETTINGS, require_all=True)
    assert _GENERIC in str(info.value)
    _assert_no_leak(str(info.value))
    for raised in info.value.issues:
        _assert_no_leak(raised.text())


async def test_open_adapters_opens_healthy_connections_beside_failed_ones() -> None:
    from apex.services.aifx_context import open_adapters

    class Mixed(LeakyResolver):
        async def resolve_metadata(self, kind: Any, connection_id: str, project_id: Any) -> Any:
            self.fail_in = "resolve" if connection_id == "conn-bad" else ""
            return await super().resolve_metadata(kind, connection_id, project_id)

    resolver = Mixed()
    adapters, _web_bases, failures = await open_adapters(resolver, ["conn-ok", "conn-bad"])
    assert set(adapters) == {"conn-ok"}
    assert failures == {"conn-bad": _GENERIC}


async def test_launch_with_a_leaky_resolver_records_only_the_generic_message(
    onboarded: StubResolver, monkeypatch: pytest.MonkeyPatch
) -> None:
    from apex.services import aifx_context, connections
    from apex.settings import ApexSettings

    monkeypatch.setattr(
        connections, "get_connection_resolver", lambda: LeakyResolver(fail_in="build")
    )
    monkeypatch.setattr(ApexSettings, "is_locked_down", property(lambda self: True))
    configurable: dict[str, Any] = {"project_id": "proj-1", "app_id": "app-a"}
    with pytest.raises(AifxUnavailableError) as info:
        await aifx_context.prepare_aifx_snapshot(configurable)
    _assert_no_leak(str(info.value))
    assert "aifx_snapshot" not in configurable

    monkeypatch.setattr(ApexSettings, "is_locked_down", property(lambda self: False))
    await aifx_context.prepare_aifx_snapshot(configurable)
    degraded = AifxSnapshotV1.model_validate(configurable["aifx_snapshot"])
    assert degraded.status is AifxSnapshotStatus.DEGRADED
    assert degraded.issues and all(_GENERIC in text for text in degraded.issues)
    _assert_no_leak(degraded.model_dump_json())


# --- a shared ACB seals with unique entry ids -------------------------------------


def _shared_acb_plans(application_ids: list[str], primary: str) -> list[SourcePlan]:
    return plan_sources(
        application_ids=application_ids,
        primary_application_id=primary,
        mappings=[
            mapping("app-a", "apm1", acb_repo="shared-acb", ref="release"),
            mapping("app-b", "apm2", acb_repo="Shared-ACB", ref="develop"),
        ],
        organizations={"org-1": org()},
        application_names={"app-a": "Storefront", "app-b": "Payments"},
        settings=SETTINGS,
    )


async def test_shared_acb_at_different_refs_seals_unique_entry_ids() -> None:
    plans = _shared_acb_plans(["app-a", "app-b"], primary="app-a")
    reads = await read_sources(plans, {"conn-ghe": StubSourceControlAdapter()}, settings=SETTINGS)
    snapshot = seal_snapshot(reads, settings=SETTINGS, require_all=True)
    assert snapshot is not None
    assert snapshot.status is AifxSnapshotStatus.PINNED

    acb_sources = [s for s in snapshot.sources if s.role is AifxRole.APPLICATION_CONTEXT]
    assert [(s.repo, s.ref, s.application_id) for s in acb_sources] == [
        ("aifx/shared-acb", "release", "app-a")
    ]
    acb_entries = [e for e in snapshot.entries if e.role is AifxRole.APPLICATION_CONTEXT]
    assert acb_entries
    assert {e.application_id for e in acb_entries} == {"app-a"}
    qualified = [f"{entry.repo}:{entry.id}" for entry in snapshot.entries]
    assert len(qualified) == len(set(qualified))
    # The sealed snapshot round-trips through the run contract's validator.
    assert AifxSnapshotV1.model_validate(snapshot.model_dump(mode="json")) == snapshot


def test_shared_acb_uses_the_primary_participants_ref_when_primary_is_not_first() -> None:
    plans = _shared_acb_plans(["app-a", "app-b"], primary="app-b")
    acb = [p for p in plans if p.role is AifxRole.APPLICATION_CONTEXT]
    assert [(p.repo, p.ref, p.application_id) for p in acb] == [("Shared-ACB", "develop", "app-b")]

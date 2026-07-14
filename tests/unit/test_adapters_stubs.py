"""Stub adapter behavior: canned fixtures, determinism (same call -> same ids), errors."""

from collections.abc import Iterator

import pytest

from apex.adapters.stubs import (
    EnvSecretsAdapter,
    MemoryArtifactStore,
    StubClusterInventoryAdapter,
    StubDocumentsAdapter,
    StubLogSearchAdapter,
    StubObservabilityAdapter,
    StubSourceControlAdapter,
    StubWorkTrackingAdapter,
)
from apex.domain.integrations import (
    DocRef,
    DocScope,
    Enrichment,
    EnvRef,
    LogQuery,
    MetricQuery,
    Page,
    QueryContext,
    RepoRef,
    TimeWindow,
    WorkItemDraft,
    WorkItemFilters,
)


@pytest.fixture(autouse=True)
def _clean_artifact_store() -> Iterator[None]:
    MemoryArtifactStore.clear()
    yield
    MemoryArtifactStore.clear()


# --- work tracking -----------------------------------------------------------


async def test_work_tracking_contains_demo_story() -> None:
    adapter = StubWorkTrackingAdapter()
    item = await adapter.get_item("PHX-241")
    assert item.title.startswith("Checkout latency p95 regression")
    assert item.status == "open"
    assert item.url is not None and item.url.endswith("/PHX-241")


async def test_work_tracking_get_unknown_key_raises() -> None:
    with pytest.raises(KeyError, match="PHX-999"):
        await StubWorkTrackingAdapter().get_item("PHX-999")


async def test_work_tracking_translate_and_execute() -> None:
    adapter = StubWorkTrackingAdapter()
    translated = await adapter.translate_query("open perf stories", context=QueryContext())
    assert translated.provider == "stub"
    assert "open perf stories" in translated.query
    assert 0 < translated.confidence <= 1

    result = await adapter.execute_query(translated, page=Page(limit=2))
    assert result.total == 3
    assert len(result.items) == 2


async def test_work_tracking_list_items_filters() -> None:
    adapter = StubWorkTrackingAdapter()
    open_stories = await adapter.list_items(
        WorkItemFilters(status="open", kind="story"), page=Page()
    )
    assert {i.key for i in open_stories.items} == {"PHX-241", "PHX-302"}

    text_hits = await adapter.list_items(WorkItemFilters(text="checkout"), page=Page())
    assert "PHX-241" in {i.key for i in text_hits.items}


async def test_work_tracking_is_deterministic_across_calls() -> None:
    adapter = StubWorkTrackingAdapter()
    first = await adapter.list_items(WorkItemFilters(), page=Page())
    second = await adapter.list_items(WorkItemFilters(), page=Page())
    assert [i.key for i in first.items] == [i.key for i in second.items]

    draft = WorkItemDraft(title="New soak test for search-svc")
    created_a = await adapter.create_item(draft)
    created_b = await adapter.create_item(draft)
    assert created_a.key == created_b.key  # key derived from title, not a counter


async def test_work_tracking_enrich_appends_comment() -> None:
    adapter = StubWorkTrackingAdapter()
    enriched = await adapter.enrich_item(
        "PHX-241", Enrichment(comment="Baseline attached", fields={"status": "in_progress"})
    )
    assert "[enrichment] Baseline attached" in enriched.description
    assert enriched.status == "in_progress"


# --- log search ----------------------------------------------------------------


async def test_log_search_returns_three_canned_entries() -> None:
    adapter = StubLogSearchAdapter()
    result = await adapter.search(LogQuery(query="checkout"), window=TimeWindow(), page=Page())
    assert result.total == 3
    assert len(result.entries) == 3
    assert {e.service for e in result.entries} == {"payment-svc", "checkout-api"}

    again = await adapter.search(LogQuery(query="checkout"), window=TimeWindow(), page=Page())
    assert [e.message for e in again.entries] == [e.message for e in result.entries]


# --- observability ---------------------------------------------------------------


async def test_observability_series_and_health() -> None:
    adapter = StubObservabilityAdapter()
    series = await adapter.query_metrics(MetricQuery(query="p95_ms"), window=TimeWindow())
    assert series.name == "p95_ms"
    assert len(series.points) == 5
    assert [p.value for p in series.points] == [212.0, 218.0, 231.0, 224.0, 219.0]

    health = await adapter.get_service_health("checkout-api", window=TimeWindow())
    assert health.healthy is True
    assert health.service == "checkout-api"
    assert "p95_ms" in health.indicators


# --- documents -------------------------------------------------------------------


async def test_documents_search_and_fetch() -> None:
    adapter = StubDocumentsAdapter()
    hits = await adapter.search("checkout", scope=DocScope())
    assert len(hits) == 2
    assert hits[0].ref.id == "doc-checkout-runbook"

    content = await adapter.fetch(hits[0].ref)
    assert "runbook" in content.text.lower()
    assert content.media_type == "text/markdown"

    limited = await adapter.search("checkout", scope=DocScope(), k=1)
    assert len(limited) == 1


async def test_documents_fetch_unknown_ref_raises() -> None:
    with pytest.raises(KeyError, match="doc-nope"):
        await StubDocumentsAdapter().fetch(DocRef(id="doc-nope"))


# --- cluster inventory ---------------------------------------------------------


async def test_inventory_snapshot_has_three_services() -> None:
    adapter = StubClusterInventoryAdapter()
    snapshot = await adapter.scan_environment(EnvRef(id="env-staging"))
    assert [s.name for s in snapshot.services] == ["checkout-api", "payment-svc", "cart-svc"]
    assert all(s.replicas >= 1 and s.image for s in snapshot.services)

    again = await adapter.scan_environment(EnvRef(id="env-staging"))
    assert again.scanned_at == snapshot.scanned_at  # pinned timestamp: fully deterministic


# --- source control ----------------------------------------------------------------


async def test_source_control_returns_fixture_content() -> None:
    adapter = StubSourceControlAdapter()
    content = await adapter.get_file(RepoRef(name="perf-scripts"), "scenarios/checkout.yaml")
    assert content.path == "scenarios/checkout.yaml"
    assert content.ref == "HEAD"
    assert "perf-scripts:scenarios/checkout.yaml@HEAD" in content.text
    assert "checkout-baseline" in content.text


# --- secrets ----------------------------------------------------------------------


async def test_env_secrets_resolves_from_environ(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("APEX_INTEGRATION_TEST_SECRET", "hunter2")
    secret = await EnvSecretsAdapter().resolve("env:APEX_INTEGRATION_TEST_SECRET")
    assert secret.value == "hunter2"
    assert "hunter2" not in repr(secret)


async def test_env_secrets_missing_var_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("APEX_INTEGRATION_TEST_MISSING", raising=False)
    with pytest.raises(KeyError, match="APEX_INTEGRATION_TEST_MISSING"):
        await EnvSecretsAdapter().resolve("env:APEX_INTEGRATION_TEST_MISSING")


async def test_env_secrets_rejects_disallowed_env_var(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PATH", "/bin")
    with pytest.raises(ValueError, match="env_secret_prefixes"):
        await EnvSecretsAdapter().resolve("env:PATH")


async def test_env_secrets_cannot_resolve_platform_apex_settings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("APEX_AUTH__API_KEY_HASH_PEPPER", "platform-secret")
    monkeypatch.setenv("APEX_DATABASE__URI", "postgresql://platform-secret")

    for name in ("APEX_AUTH__API_KEY_HASH_PEPPER", "APEX_DATABASE__URI"):
        with pytest.raises(ValueError, match="env_secret_prefixes"):
            await EnvSecretsAdapter().resolve(f"env:{name}")


async def test_env_secrets_rejects_unknown_scheme() -> None:
    with pytest.raises(ValueError, match="vault:path#key"):
        await EnvSecretsAdapter().resolve("vault:path#key")


# --- artifact store ----------------------------------------------------------------


async def test_artifact_store_put_get_roundtrip() -> None:
    store = MemoryArtifactStore()
    stored = await store.put(
        "runs/r1/results.json", b'{"ok": true}', content_type="application/json"
    )
    assert stored.uri == "memory://runs/r1/results.json"
    assert stored.size == len(b'{"ok": true}')
    assert await store.get("runs/r1/results.json") == b'{"ok": true}'
    assert await store.get_url("runs/r1/results.json") == "memory://runs/r1/results.json"


async def test_artifact_store_is_shared_across_instances() -> None:
    await MemoryArtifactStore().put("shared/key", b"data", content_type="text/plain")
    assert await MemoryArtifactStore().get("shared/key") == b"data"


async def test_artifact_store_unknown_key_raises() -> None:
    store = MemoryArtifactStore()
    with pytest.raises(KeyError, match="missing/key"):
        await store.get("missing/key")
    with pytest.raises(KeyError, match="missing/key"):
        await store.get_url("missing/key")

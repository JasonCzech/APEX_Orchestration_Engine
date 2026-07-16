"""Phase prompt resolution order: run override > catalog > builtin, with the
DB-down path falling through to builtins silently (no Postgres required)."""

from types import SimpleNamespace

import pytest

from apex.domain.pipeline import PHASE_ORDER, Phase
from apex.graphs.pipeline.configurable import PipelineConfigurable
from apex.persistence.models import PromptVersion
from apex.services.prompts import (
    DEFAULT_PHASE_PROMPTS,
    PromptCatalogUnavailableError,
    PromptCredentialMaterialError,
    PromptResolver,
    ResolvedPhasePrompt,
    prompt_review_from_resolved,
    render_template,
    resolve_phase_prompt,
    resolve_phase_prompt_sync,
    resolved_from_prompt_review,
)

VARS = {"title": "Demo", "request": "Load test checkout"}


class FakeStore:
    """ActiveVersionReader backed by a {(namespace, key): PromptVersion} dict."""

    def __init__(self, rows: dict[tuple[str, str], PromptVersion] | None = None) -> None:
        self.rows = rows or {}

    async def get_active_version(
        self,
        namespace: str,
        key: str,
        *,
        allow_application: bool = False,
    ) -> PromptVersion | None:
        if namespace == "application" and not allow_application:
            return None
        return self.rows.get((namespace, key))


def version(content: str, number: int = 1) -> PromptVersion:
    return PromptVersion(id=f"v{number}-{content[:8]}", version=number, content=content)


def cfg(**configurable: object) -> PipelineConfigurable:
    return PipelineConfigurable.model_validate(configurable)


async def test_builtin_when_catalog_empty() -> None:
    resolved = await resolve_phase_prompt(
        Phase.STORY_ANALYSIS, cfg(), variables=VARS, store=FakeStore()
    )
    assert resolved["system"] == DEFAULT_PHASE_PROMPTS["story_analysis/system"]
    assert resolved["user"] == "Title: Demo\nRequest: Load test checkout"
    assert resolved["application"] is None
    assert resolved["source"] == {
        "origin": "catalog",
        "ref": "phase/story_analysis@builtin",
        "editor": None,
    }


async def test_catalog_beats_builtin_with_version_refs() -> None:
    store = FakeStore(
        {
            ("phase", "story_analysis/system"): version("Catalog system for {title}.", 3),
            ("phase", "story_analysis/user"): version("Do: {request}", 2),
        }
    )
    resolved = await resolve_phase_prompt(Phase.STORY_ANALYSIS, cfg(), variables=VARS, store=store)
    assert resolved["system"] == "Catalog system for Demo."
    assert resolved["user"] == "Do: Load test checkout"
    assert resolved["source"]["origin"] == "catalog"
    assert resolved["source"]["ref"] == (
        "phase/story_analysis/system@v3,phase/story_analysis/user@v2"
    )


async def test_legacy_catalog_credentials_cannot_enter_resolved_prompt() -> None:
    store = FakeStore(
        {
            ("phase", "story_analysis/system"): version(
                "Authorization: Bearer legacy-catalog-secret-canary",
                3,
            )
        }
    )

    with pytest.raises(PromptCredentialMaterialError) as excinfo:
        await resolve_phase_prompt(
            Phase.STORY_ANALYSIS,
            cfg(),
            variables=VARS,
            store=store,
        )

    assert "legacy-catalog-secret-canary" not in str(excinfo.value)


async def test_rendered_prompt_cannot_assemble_credential_assignment() -> None:
    store = FakeStore({("phase", "story_analysis/system"): version("password={title}", 3)})

    with pytest.raises(PromptCredentialMaterialError):
        await resolve_phase_prompt(
            Phase.STORY_ANALYSIS,
            cfg(),
            variables={"title": "assembled-secret", "request": "safe"},
            store=store,
        )


def test_legacy_prompt_review_credentials_cannot_be_replayed_or_recheckpointed() -> None:
    with pytest.raises(PromptCredentialMaterialError):
        resolved_from_prompt_review(
            {
                "system": "safe",
                "phase_prompt": "safe",
                "additional_context": "private_key=legacy-review-secret",
            }
        )

    safe_resolved: ResolvedPhasePrompt = {
        "system": "safe",
        "user": "safe",
        "application": None,
        "source": {"origin": "builtin", "ref": "safe", "editor": None},
    }
    with pytest.raises(PromptCredentialMaterialError):
        prompt_review_from_resolved(
            safe_resolved,
            additional_context="Authorization: Bearer legacy-review-secret",
        )


async def test_application_prompt_loaded_for_selected_app() -> None:
    store = FakeStore(
        {
            ("phase", "story_analysis/system"): version("Catalog system.", 3),
            ("application", "app-checkout"): version("Checkout requires p95 under 300ms.", 4),
        }
    )
    resolved = await resolve_phase_prompt(
        Phase.STORY_ANALYSIS,
        cfg(app_id="app-checkout"),
        variables=VARS,
        store=store,
    )
    assert resolved["application"] == "Checkout requires p95 under 300ms."
    assert resolved["source"]["origin"] == "catalog"
    assert resolved["source"]["ref"] == (
        "phase/story_analysis/system@v3,application/app-checkout@v4"
    )


async def test_application_override_beats_catalog() -> None:
    store = FakeStore({("application", "app-checkout"): version("Catalog app requirements.", 4)})
    resolved = await resolve_phase_prompt(
        Phase.STORY_ANALYSIS,
        cfg(
            app_id="app-checkout",
            prompt_overrides={"application/app-checkout": {"content": "Run-specific app needs."}},
        ),
        variables=VARS,
        store=store,
    )
    assert resolved["application"] == "Run-specific app needs."
    assert resolved["source"]["origin"] == "run_override"
    assert resolved["source"]["ref"] == "application/app-checkout@override"


async def test_partial_catalog_falls_back_per_part() -> None:
    store = FakeStore({("phase", "story_analysis/system"): version("Only system.", 5)})
    resolved = await resolve_phase_prompt(Phase.STORY_ANALYSIS, cfg(), variables=VARS, store=store)
    assert resolved["system"] == "Only system."
    assert resolved["user"] == "Title: Demo\nRequest: Load test checkout"  # builtin user
    assert resolved["source"]["ref"] == "phase/story_analysis/system@v5"


async def test_run_override_beats_catalog() -> None:
    store = FakeStore({("phase", "story_analysis/system"): version("Catalog system.", 9)})
    configurable = cfg(prompt_overrides={"phase/story_analysis": {"content": "Override system."}})
    resolved = await resolve_phase_prompt(
        Phase.STORY_ANALYSIS, configurable, variables=VARS, store=store
    )
    assert resolved["system"] == "Override system."
    assert resolved["user"].startswith("Title: Demo")
    assert resolved["source"]["origin"] == "run_override"
    assert resolved["source"]["ref"] == "phase/story_analysis@override"


async def test_run_user_override_beats_catalog() -> None:
    store = FakeStore({("phase", "story_analysis/user"): version("Catalog user.", 4)})
    configurable = cfg(
        prompt_overrides={"phase/story_analysis/user": {"content": "Override user {title}."}}
    )
    resolved = await resolve_phase_prompt(
        Phase.STORY_ANALYSIS, configurable, variables=VARS, store=store
    )
    assert resolved["user"] == "Override user Demo."
    assert resolved["source"]["origin"] == "run_override"
    assert resolved["source"]["ref"] == "phase/story_analysis/user@override"


async def test_run_override_version_id_used_as_ref() -> None:
    configurable = cfg(
        prompt_overrides={"phase/story_analysis": {"content": "X", "version_id": "abc123"}}
    )
    resolved = await resolve_phase_prompt(Phase.STORY_ANALYSIS, configurable, variables=VARS)
    assert resolved["source"]["ref"] == "abc123"


async def test_db_down_falls_through_to_builtin(monkeypatch: pytest.MonkeyPatch) -> None:
    # No injected store -> the resolver builds a throwaway engine from settings;
    # point it at a closed port so the lookup fails fast and silently.
    monkeypatch.setenv("APEX_DATABASE__URI", "postgresql+asyncpg://x:x@127.0.0.1:1/x")
    resolved = await PromptResolver().resolve_phase_prompt(
        Phase.TEST_PLANNING, cfg(), variables=VARS
    )
    assert resolved["system"] == DEFAULT_PHASE_PROMPTS["test_planning/system"]
    assert resolved["source"]["ref"] == "phase/test_planning@builtin"


async def test_catalog_failure_fails_closed_in_locked_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    canary = "password=locked-prompt-catalog-secret-canary"

    class FailingStore:
        async def get_active_version(
            self,
            namespace: str,
            key: str,
            *,
            allow_application: bool = False,
        ) -> PromptVersion | None:
            raise RuntimeError(canary)

    monkeypatch.setattr(
        "apex.services.prompts.get_settings",
        lambda: SimpleNamespace(is_locked_down=True),
    )

    with pytest.raises(PromptCatalogUnavailableError, match="catalog is unavailable") as excinfo:
        await PromptResolver(store=FailingStore()).resolve_phase_prompt(
            Phase.TEST_PLANNING, cfg(), variables=VARS
        )

    assert excinfo.value.__cause__ is None
    assert excinfo.value.__context__ is None
    assert canary not in repr(excinfo.value)


def test_sync_bridge_resolves_without_running_loop(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("APEX_DATABASE__URI", "postgresql+asyncpg://x:x@127.0.0.1:1/x")
    resolved = resolve_phase_prompt_sync(Phase.EXECUTION, cfg(), variables=VARS)
    assert resolved["source"]["origin"] == "catalog"
    assert resolved["source"]["ref"] == "phase/execution@builtin"
    assert resolved["user"] == "Title: Demo\nRequest: Load test checkout"


async def test_sync_bridge_inside_running_loop_skips_catalog_io() -> None:
    # Defensive path: called from a thread that already runs a loop -> no IO,
    # override still wins, builtin otherwise.
    configurable = cfg(prompt_overrides={"phase/reporting": {"content": "O"}})
    resolved = resolve_phase_prompt_sync(Phase.REPORTING, configurable, variables=VARS)
    assert resolved["system"] == "O"
    assert resolved["source"]["origin"] == "run_override"
    resolved = resolve_phase_prompt_sync(Phase.REPORTING, cfg(), variables=VARS)
    assert resolved["source"]["ref"] == "phase/reporting@builtin"


def test_default_phase_prompts_cover_all_phases() -> None:
    assert len(DEFAULT_PHASE_PROMPTS) == 14
    for phase in PHASE_ORDER:
        assert f"{phase.value}/system" in DEFAULT_PHASE_PROMPTS
        assert f"{phase.value}/user" in DEFAULT_PHASE_PROMPTS


def test_render_template_is_safe() -> None:
    assert render_template("Hi {name}", {"name": "Ada"}) == "Hi Ada"
    assert render_template("Hi {missing}", {}) == "Hi {missing}"  # unknown stays literal
    assert render_template('{"json": true}', {}) == '{"json": true}'  # malformed passes through
    assert render_template('{"timeout": 1000000000}', {}) == '{"timeout": 1000000000}'


@pytest.mark.parametrize(
    "template",
    [
        "{title:>1000000000}",
        "{title:.1000000000f}",
        "{title:{width}}",
        "{record.secret}",
    ],
)
def test_render_template_rejects_unbounded_format_expansion(template: str) -> None:
    with pytest.raises(ValueError, match="prompt|rendered"):
        render_template(template, {"title": "Demo", "width": 1})


def test_render_template_bounds_aggregate_repeated_expansion(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "apex.services.prompts.get_settings",
        lambda: SimpleNamespace(runs=SimpleNamespace(max_model_input_chars=10_000)),
    )

    with pytest.raises(ValueError, match="rendered prompt exceeds"):
        render_template("{title}" * 101, {"title": "x" * 100})


async def test_run_override_rejects_format_width_amplification() -> None:
    configurable = cfg(
        prompt_overrides={
            "phase/reporting": {"content": "{title:>1000000000}"},
        }
    )

    with pytest.raises(ValueError, match="format width/precision"):
        await resolve_phase_prompt(
            Phase.REPORTING,
            configurable,
            variables=VARS,
            store=FakeStore(),
        )


async def test_catalog_prompt_rejects_format_width_amplification() -> None:
    store = FakeStore(
        {
            ("phase", "reporting/system"): version("{title:>1000000000}"),
        }
    )

    with pytest.raises(ValueError, match="format width/precision"):
        await resolve_phase_prompt(Phase.REPORTING, cfg(), variables=VARS, store=store)

"""Authorize and freeze catalog selections at the run boundary."""

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from apex.services.prompts import ResolvedPhasePrompt

from fastapi import HTTPException

from apex.auth.identity import ConsumerIdentity
from apex.domain.agent_deliverables import output_contract
from apex.domain.diagnostics import contains_credential_material
from apex.domain.pipeline import Phase
from apex.domain.prompt_catalog import PLATFORM_POLICY
from apex.domain.prompt_selection import PromptManifestEntry, PromptManifestV1
from apex.graphs.pipeline.configurable import PipelineConfigurable
from apex.persistence.db import get_sessionmaker
from apex.persistence.repositories.prompts import PromptRepository
from apex.settings import get_settings


async def prepare_prompt_manifest(
    configurable: dict[str, Any],
    identity: ConsumerIdentity,
    run_input: dict[str, Any] | None = None,
) -> None:
    cfg = PipelineConfigurable.model_validate(
        {
            key: value
            for key, value in configurable.items()
            if key in PipelineConfigurable.model_fields
        }
    )
    from apex.services.prompts import render_catalog_template

    inputs = run_input or {}
    variables = {
        "title": inputs.get("title") or "untitled run",
        "request": inputs.get("request") or "(no request provided)",
    }
    selections = cfg.prompt_selections
    if selections is None:
        return
    async with get_sessionmaker()() as session:
        repo = PromptRepository(session, identity=identity)

        async def freeze(selection, namespace: str, path: list[str | int], phase=None):
            prompt = await repo.get(selection.prompt_id)
            version = await repo.get_version(selection.version_id)
            valid = (
                prompt is not None
                and version is not None
                and version.prompt_id == prompt.id
                and prompt.namespace == namespace
                and prompt.selection_enabled
                and prompt.archived_at is None
                and prompt.active_version_id is not None
            )
            if prompt is None or version is None:
                valid = False
            if valid and prompt is not None and namespace == "phase":
                valid = phase is not None and prompt.phase == phase.value
            if valid and prompt is not None and namespace == "application":
                valid = prompt.is_shared or prompt.application_id in cfg.application_ids
            if valid and prompt is not None and namespace == "environment":
                valid = prompt.environment_id in cfg.environment_ids
            if not valid:
                raise HTTPException(
                    422,
                    [
                        {
                            "loc": ["prompt_selections", *path],
                            "msg": "Selected prompt or version is unavailable for this test",
                            "type": "value_error",
                        }
                    ],
                )
            assert prompt is not None and version is not None
            content = (
                selection.override_content
                if selection.override_content is not None
                else version.content
            )
            if (
                not content.strip()
                or len(content) > get_settings().runs.max_prompt_part_chars
                or contains_credential_material(content)
            ):
                raise HTTPException(
                    422,
                    [
                        {
                            "loc": ["prompt_selections", *path],
                            "msg": "Selected prompt content is invalid",
                            "type": "value_error",
                        }
                    ],
                )
            try:
                rendered = render_catalog_template(content, variables)
                if (
                    not rendered.strip()
                    or len(rendered) > get_settings().runs.max_prompt_part_chars
                ):
                    raise ValueError("Rendered prompt content is invalid")
            except ValueError:
                raise HTTPException(
                    422,
                    [
                        {
                            "loc": ["prompt_selections", *path],
                            "msg": "Selected prompt template is invalid or exceeds input limits",
                            "type": "value_error",
                        }
                    ],
                ) from None
            return PromptManifestEntry(
                namespace=namespace,
                phase=Phase(prompt.phase) if prompt.phase else None,
                application_id=prompt.application_id,
                environment_id=prompt.environment_id,
                is_shared=prompt.is_shared,
                prompt_id=prompt.id,
                version_id=version.id,
                key=prompt.key,
                version=version.version,
                content=content,
                overridden=selection.override_content is not None,
            )

        manifest = PromptManifestV1(
            system=await freeze(selections.system, "system", ["system"]),
            phases={
                phase: await freeze(item, "phase", ["phases", phase.value], phase)
                for phase, item in selections.phases.items()
            },
            application=[
                await freeze(item, "application", ["application", i])
                for i, item in enumerate(selections.application)
            ],
            environment=[
                await freeze(item, "environment", ["environment", i])
                for i, item in enumerate(selections.environment)
            ],
        )
        prepared = cfg.model_copy(update={"prompt_manifest": manifest})
        for phase in manifest.phases:
            resolved = resolve_manifest(phase, prepared, variables)
            system = output_contract(phase) + "\n\n" + compose_selected_system(resolved)
            user = f"Title: {variables['title']}\nRequest: {variables['request']}"
            if len(system) + len(user) > get_settings().runs.max_model_input_chars:
                raise HTTPException(
                    422,
                    [
                        {
                            "loc": ["prompt_selections", "phases", phase.value],
                            "msg": (
                                "Selected prompts and fixed instructions "
                                "exceed the model input limit"
                            ),
                            "type": "value_error",
                        }
                    ],
                )
        configurable["prompt_manifest"] = manifest.model_dump(mode="json")


def resolve_manifest(
    phase, cfg: PipelineConfigurable, variables: dict[str, Any]
) -> "ResolvedPhasePrompt":
    from apex.services.prompts import render_catalog_template

    # Plan seeding and resumed phase nodes must render the same shared system
    # content when an optional operator request was submitted as an empty string.
    variables = {
        **variables,
        "title": variables.get("title") or "untitled run",
        "request": variables.get("request") or "(no request provided)",
    }
    manifest = cfg.prompt_manifest
    if manifest is None or phase not in manifest.phases:
        raise ValueError("This test has no valid persisted prompt manifest for the requested phase")
    entries = [
        manifest.system,
        manifest.phases[phase],
        *manifest.application,
        *manifest.environment,
    ]
    selections = cfg.prompt_selections
    if selections is None:
        raise ValueError("Prompt selections are missing")
    expected = [
        selections.system,
        selections.phases[phase],
        *selections.application,
        *selections.environment,
    ]
    if len(entries) != len(expected) or any(
        (e.prompt_id, e.version_id) != (s.prompt_id, s.version_id)
        for e, s in zip(entries, expected, strict=True)
    ):
        raise ValueError("Persisted prompt manifest does not match the test selections")
    contexts = [
        {
            **entry.model_dump(mode="json"),
            "content": render_catalog_template(entry.content, variables),
        }
        for entry in entries[2:]
    ]
    application = compose_contexts(contexts)
    return {
        "system": render_catalog_template(manifest.system.content, variables),
        "user": render_catalog_template(manifest.phases[phase].content, variables),
        "application": application or None,
        "system_locked": True,
        "contexts": contexts,
        "selections": [_selection_provenance(entry, cfg) for entry in entries],
        "source": {
            "origin": "run_override" if any(e.overridden for e in entries) else "catalog",
            "ref": (
                f"manifest:v1; system/{manifest.system.key}@v{manifest.system.version}; "
                f"phase/{manifest.phases[phase].key}@v{manifest.phases[phase].version}"
            ),
            "editor": None,
        },
    }


def _selection_provenance(entry: PromptManifestEntry, cfg: PipelineConfigurable) -> dict[str, Any]:
    provenance = entry.model_dump(mode="json", exclude={"content"})
    scope = cfg.test_scope
    if scope is None or entry.namespace not in {"application", "environment"}:
        return provenance
    if entry.is_shared:
        provenance["scope_label"] = "Shared across all test participants"
        return provenance
    participants = [
        participant
        for participant in scope.participants
        if (entry.namespace == "application" and participant.application_id == entry.application_id)
        or (entry.namespace == "environment" and participant.environment_id == entry.environment_id)
    ]
    if not participants:
        raise ValueError("Selected context does not apply to a test participant")
    labels = []
    layer_order = {"frontend": 0, "middle": 1, "backend": 2}
    for participant in sorted(participants, key=lambda item: (layer_order[item.layer], item.id)):
        roles: list[str] = [participant.layer]
        if participant.id == scope.primary_participant_id:
            roles.append("primary system")
        if participant.id == scope.traffic_entry_participant_id:
            roles.append("traffic entry")
        labels.append(f"{participant.id} ({', '.join(roles)})")
    provenance["scope_label"] = "Applies to participants: " + "; ".join(labels)
    return provenance


def compose_contexts(contexts: list[dict[str, Any]]) -> str:
    return "\n\n".join(
        f"=== {entry['namespace'].upper()} CONTEXT: {entry['key']} ===\n{entry['content']}"
        for entry in contexts
    )


def compose_selected_system(resolved: Mapping[str, Any]) -> str:
    """Use identical policy and entry boundaries at admission and execution."""
    labels = {
        entry["prompt_id"]: entry["scope_label"]
        for entry in resolved.get("selections", [])
        if "scope_label" in entry
    }
    application = str(resolved.get("application") or "")
    if labels:
        # Add immutable applicability beside each model-facing entry without
        # changing its raw text or the aggregate used by compatibility editors.
        application = "\n\n".join(
            f"=== {entry['namespace'].upper()} CONTEXT: {entry['key']} ===\n"
            + (f"{labels[entry['prompt_id']]}\n" if entry["prompt_id"] in labels else "")
            + entry["content"]
            for entry in resolved.get("contexts", [])
        )
    # Application and environment context frame the phase: they come before
    # its instructions, matching the order the wizard presents them.
    return (
        PLATFORM_POLICY
        + "\n=== SHARED SYSTEM PROMPT ===\n"
        + resolved["system"].strip()
        + (f"\n\n{application}" if application else "")
        + "\n=== PHASE CONTEXT ===\n"
        + resolved["user"]
    )


def validate_manifest_review(
    review: dict[str, Any],
    phase: Phase,
    cfg: PipelineConfigurable,
    variables: dict[str, Any],
) -> None:
    """Keep checkpointed review revisions bound to their immutable selections."""
    if cfg.prompt_selections is None:
        return
    if type(review) is not dict:
        raise ValueError("Checkpointed prompt review is invalid")
    expected = resolve_manifest(phase, cfg, variables)
    metadata = review_metadata(review)
    if (
        not metadata
        or review.get("system") != expected["system"]
        or metadata["selections"] != expected.get("selections")
        or [
            {key: value for key, value in entry.items() if key != "content"}
            for entry in metadata["contexts"]
        ]
        != [
            {key: value for key, value in entry.items() if key != "content"}
            for entry in expected.get("contexts", [])
        ]
        or (review.get("application") or "") != compose_contexts(metadata["contexts"])
        or not isinstance(review.get("phase_prompt"), str)
        or not review["phase_prompt"].strip()
    ):
        raise ValueError("Checkpointed prompt review does not match the persisted manifest")


def review_metadata(value: dict[str, Any]) -> dict[str, Any]:
    """Validate metadata before carrying it through checkpoint/public projections."""
    if value.get("system_locked") is not True:
        return {}
    contexts = value.get("contexts", [])
    selections = value.get("selections", [])
    if (
        not isinstance(contexts, list)
        or len(contexts) > 64
        or not isinstance(selections, list)
        or len(selections) > 66
    ):
        raise ValueError("Invalid prompt manifest review metadata")
    parsed = [PromptManifestEntry.model_validate(item) for item in contexts]
    if any(item.namespace not in {"application", "environment"} for item in parsed):
        raise ValueError("Invalid context category")
    from apex.domain.input_limits import validate_json_object

    validate_json_object(
        {"selections": selections}, label="prompt provenance", max_bytes=1_000_000, max_nodes=3000
    )
    if contains_credential_material({"contexts": contexts, "selections": selections}):
        raise ValueError("Prompt review contains credential material")
    return {
        "system_locked": True,
        "contexts": [item.model_dump(mode="json") for item in parsed],
        "selections": selections,
    }


def edited_review_metadata(
    current: dict[str, Any],
    system: str | None,
    application: str | None,
    context_edits: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    metadata = review_metadata(current)
    if not metadata:
        return {}
    if system != current.get("system"):
        raise ValueError("The shared system prompt is locked after launch")
    contexts = metadata["contexts"]
    if context_edits is not None:
        from apex.domain.prompt_selection import PromptContextEdit

        edits = [PromptContextEdit.model_validate(item) for item in context_edits]
        if [item.prompt_id for item in edits] != [item["prompt_id"] for item in contexts]:
            raise ValueError("Context prompt membership is locked after launch")
        for item, edit in zip(contexts, edits, strict=True):
            if (
                not edit.content.strip()
                or len(edit.content) > get_settings().runs.max_prompt_part_chars
            ):
                raise ValueError("Context prompt content is invalid")
            if contains_credential_material(edit.content):
                raise ValueError("Context prompt contains credential material")
            item["content"] = edit.content
        return metadata
    text = application or ""
    # Approval and phase-only edits must preserve context text byte-for-byte,
    # including text that happens to contain another entry's display heading.
    if text == compose_contexts(contexts):
        return metadata
    # Context editors retain these immutable headers; changing membership or
    # identity through the aggregate compatibility field is rejected.
    for i, item in enumerate(contexts):
        header = f"=== {item['namespace'].upper()} CONTEXT: {item['key']} ===\n"
        if not text.startswith(header):
            raise ValueError("Context prompt membership is locked after launch")
        text = text[len(header) :]
        if i + 1 < len(contexts):
            following = contexts[i + 1]
            delimiter = (
                f"\n\n=== {following['namespace'].upper()} CONTEXT: {following['key']} ===\n"
            )
            content, separator, remaining = text.partition(delimiter)
            if not separator:
                raise ValueError("Context prompt membership is locked after launch")
            text = delimiter[2:] + remaining
        else:
            content, text = text, ""
        if not content.strip() or len(content) > get_settings().runs.max_prompt_part_chars:
            raise ValueError("Context prompt content is invalid")
        if contains_credential_material(content):
            raise ValueError("Context prompt contains credential material")
        item["content"] = content
    if text:
        raise ValueError("Context prompt membership is locked after launch")
    return metadata

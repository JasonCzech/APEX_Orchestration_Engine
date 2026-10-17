"""Pre-acceptance evidence uses the same bounded public projection as history."""

import pytest

from apex.services.pipeline_public import public_phase_evidence


def test_review_evidence_preserves_actual_model_and_omits_internal_fields():
    evidence = public_phase_evidence(
        "story_analysis",
        {
            "model": "claude-sonnet",
            "provenance": "llm",
            "attempt": 1,
            "reasoning_digest": "Checked the requirements",
            "internal": "private",
        },
    )
    assert evidence == {
        "phase": "story_analysis",
        "attempt": 1,
        "model": "claude-sonnet",
        "provenance": "llm",
        "reasoning_digest": "Checked the requirements",
    }


@pytest.mark.parametrize("model", [None, [], "x" * 201, "password=private-secret-canary"])
def test_review_evidence_rejects_invalid_or_credential_model(model):
    assert "model" not in public_phase_evidence("story_analysis", {"model": model})

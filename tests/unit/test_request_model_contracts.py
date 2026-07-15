"""Caller-authored JSON bodies reject ambiguous or misspelled fields."""

from typing import Any

import pytest
from pydantic import BaseModel, ValidationError

from apex.auth.identity import ScopeRef
from apex.domain.integrations import Enrichment, LoadTestSpec, TranslatedQuery, WorkItemDraft
from apex.domain.pipeline import ContextPacket, ExternalResults
from apex.routers.catalog import (
    ApplicationCreate,
    ApplicationUpdate,
    EnvironmentCreate,
    EnvironmentUpdate,
    HostIn,
)
from apex.routers.connections import ConnectionCreate, ConnectionUpdate, HostMappingIn
from apex.routers.consumers import (
    ConsumerCreateRequest,
    ConsumerUpdateRequest,
    RotateConsumerKeyRequest,
)
from apex.routers.context import ContextSummaryRequest
from apex.routers.documents import DocumentArtifactAffinityUpdate
from apex.routers.drafts import DraftCreateRequest, DraftUpdateRequest
from apex.routers.engines import AbortEngineRunRequest
from apex.routers.logs import LogQueryIn, LogSearchRequest, WindowIn
from apex.routers.pipelines import (
    GatePromptEdit,
    PhasePromptReviewUpdate,
    ResumeGateRequest,
    StartPipelineRequest,
)
from apex.routers.prompts import (
    CreatePromptRequest,
    RollbackRequest,
    SaveVersionRequest,
)
from apex.routers.prompts import (
    TestPromptRequest as PromptTestRequest,
)
from apex.routers.work_tracking import (
    ExecuteQueryRequest,
    SavedQueryCreate,
    SavedQueryUpdate,
    TranslateQueryRequest,
)

REQUEST_MODEL_CASES: tuple[tuple[type[BaseModel], dict[str, Any]], ...] = (
    (StartPipelineRequest, {"title": "run"}),
    (GatePromptEdit, {}),
    (ResumeGateRequest, {"action": "approve"}),
    (PhasePromptReviewUpdate, {"system": "s", "phase_prompt": "p"}),
    (DraftCreateRequest, {"title": "draft"}),
    (DraftUpdateRequest, {"title": "draft"}),
    (LogQueryIn, {}),
    (WindowIn, {}),
    (LogSearchRequest, {}),
    (ContextSummaryRequest, {"subject": "checkout"}),
    (TranslateQueryRequest, {"text": "open bugs"}),
    (
        ExecuteQueryRequest,
        {"query": {"provider": "stub", "query": "open bugs"}},
    ),
    (SavedQueryCreate, {"name": "q", "provider": "stub", "query": "open"}),
    (SavedQueryUpdate, {}),
    (
        CreatePromptRequest,
        {"namespace": "phase", "key": "story/system", "content": "prompt"},
    ),
    (SaveVersionRequest, {"content": "prompt"}),
    (RollbackRequest, {"version_id": "a" * 32}),
    (PromptTestRequest, {}),
    (
        ConsumerCreateRequest,
        {"name": "consumer", "consumer_type": "dashboard", "role": "viewer"},
    ),
    (ConsumerUpdateRequest, {}),
    (RotateConsumerKeyRequest, {}),
    (
        ConnectionCreate,
        {"kind": "work_tracking", "provider": "stub", "name": "tracker"},
    ),
    (ConnectionUpdate, {}),
    (HostMappingIn, {"pattern": "*.example.test", "target": "10.0.0.1"}),
    (DocumentArtifactAffinityUpdate, {"connection_id": "a" * 32}),
    (AbortEngineRunRequest, {}),
    (ApplicationCreate, {"project_id": "project", "name": "app"}),
    (ApplicationUpdate, {}),
    (HostIn, {"hostname": "app.example.test"}),
    (EnvironmentCreate, {"application_id": "a" * 32, "name": "staging"}),
    (EnvironmentUpdate, {}),
    (WorkItemDraft, {"title": "story"}),
    (Enrichment, {}),
    (TranslatedQuery, {"provider": "stub", "query": "open"}),
    (
        LoadTestSpec,
        {"title": "load", "vusers": 1, "ramp_s": 0, "duration_s": 1},
    ),
    (ScopeRef, {"project_id": "project"}),
    (ContextPacket, {"source": "request", "title": "context"}),
    (ExternalResults, {"source": "upload"}),
)


@pytest.mark.parametrize(("model", "payload"), REQUEST_MODEL_CASES)
def test_request_models_forbid_unknown_fields(
    model: type[BaseModel], payload: dict[str, Any]
) -> None:
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        model.model_validate(payload | {"unexpected_security_field": "ignored-before"})

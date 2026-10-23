"""Authorization expectations for the /v1 route matrix (see test_authz_matrix).

MIN_ROLE is the single source of truth for "who may call what": every live /v1
operation must appear here with its minimum role, and every entry here must still
exist in the app — test_authz_matrix enforces both directions, so adding an
endpoint without classifying it fails CI with "unclassified operation_id".

Conventions (router style, ADRs): GET -> "viewer" (any authenticated consumer),
mutating verbs -> "operator", everything under /v1/admin/* -> "admin".
Deliberate exceptions to the conventions are commented inline.
"""

from typing import Any

MIN_ROLE: dict[str, str] = {
    "getCycleActors": "viewer",
    "getCycleExecutionDispatch": "viewer",
    "dispatchCycleExecution": "operator",
    "cancelCycleExecutionDispatch": "operator",
    "listProjectLabels": "viewer",
    # Project-wide display names require administrator scope; app-only admins cannot rename.
    "setProjectLabel": "admin",
    "deleteProjectLabel": "admin",
    "getCycleExecutions": "viewer",
    "getCycleResults": "viewer",
    "getCycleSources": "viewer",
    "captureCycleSource": "operator",
    "writeCycleFollowup": "operator",
    "startNextCycle": "operator",
    "freezeCycleReportEvidence": "operator",
    "prepareCycleExecution": "operator",
    "archiveCycleExecutionPreparation": "operator",
    "linkCycleRun": "operator",
    "getCycleScripts": "viewer",
    "storeCycleScript": "operator",
    "downloadCycleScript": "viewer",
    "getCycleScriptValidations": "viewer",
    "validateCycleScript": "operator",
    "getCycleProviderScripts": "viewer",
    "importCycleProviderScript": "operator",
    "listCycles": "viewer",
    "getCycle": "viewer",
    "createCycle": "operator",
    "createCycleVersion": "operator",
    "reviewCycleVersion": "operator",
    # ── system ──────────────────────────────────────────────────────────────
    "getSystemInfo": "viewer",
    # ── auth / principal introspection ──────────────────────────────────────
    "getAuthMe": "viewer",
    # ── pipelines ───────────────────────────────────────────────────────────
    "createPipelineRun": "operator",
    "listPipelines": "viewer",
    "getPipeline": "viewer",
    "getPipelineActors": "viewer",
    "getPipelineSetup": "viewer",
    "listAnalysisConnections": "viewer",
    # Metadata-only POST; does not launch or contact a provider.
    "getPipelineExecutionReadiness": "viewer",
    "getPhasePromptReview": "viewer",
    "patchPhasePromptReview": "operator",
    "rerunPipeline": "operator",
    "resumeGate": "operator",
    "abortPipeline": "operator",
    # ── prompts ─────────────────────────────────────────────────────────────
    "listPrompts": "viewer",
    "createPrompt": "admin",
    "getPrompt": "viewer",
    "savePromptVersion": "admin",
    "listPromptVersions": "viewer",
    "getPromptUsage": "viewer",
    "getPromptVersion": "viewer",
    "rollbackPrompt": "admin",
    "archivePrompt": "admin",
    "setDefaultPrompt": "admin",
    "unarchivePrompt": "admin",
    "testPrompt": "operator",
    # ── catalog ─────────────────────────────────────────────────────────────
    "listApplications": "viewer",
    "createApplication": "operator",
    "getApplication": "viewer",
    "updateApplication": "operator",
    "archiveApplication": "operator",
    "unarchiveApplication": "operator",
    # EXCEPTION: stricter than the mutation convention — deleting an application
    # cascades its environments, so catalog.py gates it with AdminIdentity.
    "deleteApplication": "admin",
    "listEnvironments": "viewer",
    "createEnvironment": "operator",
    "getEnvironment": "viewer",
    "updateEnvironment": "operator",
    # Network target approval requires an unscoped platform administrator.
    "approveEnvironmentTarget": "admin",
    "deleteEnvironment": "operator",
    # ── documents / artifacts ───────────────────────────────────────────────
    "uploadDocument": "operator",
    "listDocuments": "viewer",
    "getDocument": "viewer",
    # One-time repair of durable artifact-store affinity is admin-only.
    "assignDocumentArtifactConnection": "admin",
    "deleteDocument": "operator",
    "getArtifact": "viewer",
    # ── drafts ──────────────────────────────────────────────────────────────
    "listDrafts": "viewer",
    "createDraft": "operator",
    "getDraft": "viewer",
    "updateDraft": "operator",
    "deleteDraft": "operator",
    # ── engines ─────────────────────────────────────────────────────────────
    "listEngineRuns": "viewer",
    "getEngineRuns": "viewer",
    "abortEngineRun": "operator",
    # ── work tracking ───────────────────────────────────────────────────────
    # EXCEPTION: POST verbs with pure read semantics (query translation /
    # execution passthrough) — any authenticated consumer may query.
    "getWorkTrackingBinding": "viewer",
    "translateWorkQuery": "viewer",
    "executeWorkQuery": "viewer",
    "listWorkItems": "viewer",
    "getWorkItem": "viewer",
    "createWorkItem": "operator",
    "enrichWorkItem": "operator",
    "listSavedQueries": "viewer",
    "createSavedQuery": "operator",
    "getSavedQuery": "viewer",
    "updateSavedQuery": "operator",
    "deleteSavedQuery": "operator",
    # ── analytics ───────────────────────────────────────────────────────────
    "getUsageAnalytics": "viewer",
    "getAgentAnalytics": "viewer",  # any authenticated role; cost figures gated by admin+flag
    "getAgentUsageHistory": "viewer",
    "getAgentUsageEvent": "viewer",
    "exportAgentUsage": "viewer",
    "getAgentUsageFacets": "viewer",
    "listModelPricing": "admin",
    "getModelPricing": "admin",
    "getModelPriceHistory": "admin",
    "setModelPrice": "admin",
    "archiveAnalyticsModel": "admin",
    "restoreAnalyticsModel": "admin",
    "previewModelCorrection": "admin",
    "createModelCorrection": "admin",
    "getModelCorrection": "admin",
    "retryModelCorrection": "admin",
    # ── logs ────────────────────────────────────────────────────────────────
    # EXCEPTION: POST with read semantics (search request body) — viewer.
    "searchLogs": "viewer",
    # ── inventory ───────────────────────────────────────────────────────────
    "getEnvironmentInventory": "viewer",
    "rescanEnvironment": "operator",
    # ── context ─────────────────────────────────────────────────────────────
    "createContextSummary": "operator",
    "listContextEvidence": "viewer",
    # ── /admin/consumers (admin-only surface) ───────────────────────────────
    "listConsumers": "admin",
    "createConsumer": "admin",
    "getConsumer": "admin",
    "updateConsumer": "admin",
    "deleteConsumer": "admin",
    "rotateConsumerKey": "admin",
    # ── /admin/connections (admin-only surface, router-level gate) ──────────
    "listConnections": "admin",
    "createConnection": "admin",
    "getConnection": "admin",
    "updateConnection": "admin",
    "deleteConnection": "admin",
    "enableConnection": "admin",
    "disableConnection": "admin",
    "getHostMappings": "admin",
    "putHostMappings": "admin",
    "testConnection": "admin",
    # ── APEX Load native control (operator-only, project-bound) ────────────
    "listApexLoadNativeConnections": "operator",
    "getApexLoadCapabilities": "operator",
    "listApexLoadScenarios": "operator",
    "createApexLoadScenario": "operator",
    "validateApexLoadScenario": "operator",
    "sizeApexLoadScenario": "operator",
    "getApexLoadScenario": "operator",
    "updateApexLoadScenario": "operator",
    "deleteApexLoadScenario": "operator",
    "listApexLoadSchedules": "operator",
    "createApexLoadSchedule": "operator",
    "updateApexLoadSchedule": "operator",
    "deleteApexLoadSchedule": "operator",
    "listApexLoadScheduleOccurrences": "operator",
    "decideApexLoadScheduleApproval": "operator",
    "getApexLoadScheduleHealth": "operator",
    "getApexLoadRunCalendar": "operator",
    "listApexLoadRuns": "operator",
    "listMirroredApexLoadRuns": "operator",
    "getApexLoadRunMetrics": "operator",
    "getApexLoadRunEvidence": "operator",
    "getApexLoadRunConsole": "operator",
    # ── /admin/compliance (admin-only surface) ──────────────────────────────
    "verifyAuditChain": "admin",
    "exportAuditJsonl": "admin",
    "exportAuditCef": "admin",
    "getAuditRetention": "admin",
    "pruneAuditRetention": "admin",
}

SCOPE: dict[str, str] = {
    "getCycleActors": "project_app",
    "getCycleExecutionDispatch": "project_app",
    "dispatchCycleExecution": "project_app",
    "cancelCycleExecutionDispatch": "project_app",
    "listProjectLabels": "project",
    "setProjectLabel": "admin_scope",
    "deleteProjectLabel": "admin_scope",
    "getCycleExecutions": "project_app",
    "getCycleResults": "project_app",
    "getCycleSources": "project_app",
    "captureCycleSource": "project_app",
    "writeCycleFollowup": "project_app",
    "startNextCycle": "project_app",
    "freezeCycleReportEvidence": "project_app",
    "prepareCycleExecution": "project_app",
    "archiveCycleExecutionPreparation": "project_app",
    "linkCycleRun": "project_app",
    "getCycleScripts": "project_app",
    "storeCycleScript": "project_app",
    "downloadCycleScript": "project_app",
    "getCycleScriptValidations": "project_app",
    "validateCycleScript": "project_app",
    "getCycleProviderScripts": "project_app",
    "importCycleProviderScript": "project_app",
    "listCycles": "project_app",
    "getCycle": "project_app",
    "createCycle": "project_app",
    "createCycleVersion": "project_app",
    "reviewCycleVersion": "project_app",
    # ── system ──────────────────────────────────────────────────────────────
    "getSystemInfo": "none",
    # ── auth / principal introspection ──────────────────────────────────────
    "getAuthMe": "none",
    # ── pipelines ───────────────────────────────────────────────────────────
    "createPipelineRun": "project_app",
    "listPipelines": "project",
    "getPipeline": "project",
    "getPipelineActors": "project_app",
    "getPipelineSetup": "project_app",
    "listAnalysisConnections": "project",
    "getPipelineExecutionReadiness": "project_app",
    "getPhasePromptReview": "project",
    "patchPhasePromptReview": "project",
    "rerunPipeline": "project",
    "resumeGate": "project",
    "abortPipeline": "project",
    # ── prompts ─────────────────────────────────────────────────────────────
    "listPrompts": "none",
    "createPrompt": "none",
    "getPrompt": "none",
    "savePromptVersion": "none",
    "listPromptVersions": "none",
    # No project parameter: each usage row is filtered by the caller's scopes.
    "getPromptUsage": "none",
    "getPromptVersion": "none",
    "rollbackPrompt": "none",
    "archivePrompt": "none",
    "setDefaultPrompt": "admin_scope",
    "unarchivePrompt": "none",
    "testPrompt": "none",
    # ── catalog ─────────────────────────────────────────────────────────────
    "listApplications": "project_app",
    "createApplication": "project",
    "getApplication": "project_app",
    "updateApplication": "project_app",
    "archiveApplication": "project_app",
    "unarchiveApplication": "project_app",
    "deleteApplication": "project_app",
    "listEnvironments": "project_app",
    "createEnvironment": "project_app",
    "getEnvironment": "project_app",
    "updateEnvironment": "project_app",
    "approveEnvironmentTarget": "admin_scope",
    "deleteEnvironment": "project_app",
    # ── documents / artifacts ───────────────────────────────────────────────
    "uploadDocument": "project_app",
    "listDocuments": "project",
    "getDocument": "project_app",
    "assignDocumentArtifactConnection": "admin_scope",
    "deleteDocument": "project_app",
    "getArtifact": "project_app",
    # ── drafts ──────────────────────────────────────────────────────────────
    "listDrafts": "project",
    "createDraft": "project",
    "getDraft": "project",
    "updateDraft": "project",
    "deleteDraft": "project",
    # ── engines ─────────────────────────────────────────────────────────────
    "listEngineRuns": "project",
    "getEngineRuns": "project",
    "abortEngineRun": "project",
    # ── work tracking ───────────────────────────────────────────────────────
    "getWorkTrackingBinding": "project",
    "translateWorkQuery": "provider_project",
    "executeWorkQuery": "provider_project",
    "listWorkItems": "provider_project",
    "getWorkItem": "provider_project",
    "createWorkItem": "provider_project",
    "enrichWorkItem": "provider_project",
    "listSavedQueries": "project",
    "createSavedQuery": "project",
    "getSavedQuery": "project",
    "updateSavedQuery": "project",
    "deleteSavedQuery": "project",
    # ── analytics ───────────────────────────────────────────────────────────
    "getUsageAnalytics": "project",
    "getAgentAnalytics": "project",
    "getAgentUsageHistory": "project_app",
    "getAgentUsageEvent": "project_app",
    "exportAgentUsage": "project_app",
    "getAgentUsageFacets": "project_app",
    "listModelPricing": "admin_scope",
    "getModelPricing": "admin_scope",
    "getModelPriceHistory": "admin_scope",
    "setModelPrice": "admin_scope",
    "archiveAnalyticsModel": "admin_scope",
    "restoreAnalyticsModel": "admin_scope",
    "previewModelCorrection": "admin_scope",
    "createModelCorrection": "admin_scope",
    "getModelCorrection": "admin_scope",
    "retryModelCorrection": "admin_scope",
    # ── logs ────────────────────────────────────────────────────────────────
    "searchLogs": "provider_project",
    # ── inventory ───────────────────────────────────────────────────────────
    "getEnvironmentInventory": "project_app",
    "rescanEnvironment": "project_app",
    # ── context ─────────────────────────────────────────────────────────────
    "createContextSummary": "project",
    "listContextEvidence": "project",
    # ── /admin/consumers (admin-only surface) ───────────────────────────────
    "listConsumers": "admin_scope",
    "createConsumer": "admin_scope",
    "getConsumer": "admin_scope",
    "updateConsumer": "admin_scope",
    "deleteConsumer": "admin_scope",
    "rotateConsumerKey": "admin_scope",
    # ── /admin/connections (admin-only surface, router-level gate) ──────────
    "listConnections": "admin_scope",
    "createConnection": "admin_scope",
    "getConnection": "admin_scope",
    "updateConnection": "admin_scope",
    "deleteConnection": "admin_scope",
    "enableConnection": "admin_scope",
    "disableConnection": "admin_scope",
    "getHostMappings": "admin_scope",
    "putHostMappings": "admin_scope",
    "testConnection": "admin_scope",
    # ── APEX Load native control ───────────────────────────────────────────
    "listApexLoadNativeConnections": "project",
    "getApexLoadCapabilities": "project",
    "listApexLoadScenarios": "project",
    "createApexLoadScenario": "project",
    "validateApexLoadScenario": "project",
    "sizeApexLoadScenario": "project",
    "getApexLoadScenario": "project",
    "updateApexLoadScenario": "project",
    "deleteApexLoadScenario": "project",
    "listApexLoadSchedules": "project",
    "createApexLoadSchedule": "project",
    "updateApexLoadSchedule": "project",
    "deleteApexLoadSchedule": "project",
    "listApexLoadScheduleOccurrences": "project",
    "decideApexLoadScheduleApproval": "project",
    "getApexLoadScheduleHealth": "project",
    "getApexLoadRunCalendar": "project",
    "listApexLoadRuns": "project",
    "listMirroredApexLoadRuns": "project",
    "getApexLoadRunMetrics": "project",
    "getApexLoadRunEvidence": "project",
    "getApexLoadRunConsole": "project",
    # ── /admin/compliance (admin-only surface) ──────────────────────────────
    "verifyAuditChain": "admin_scope",
    "exportAuditJsonl": "admin_scope",
    "exportAuditCef": "admin_scope",
    "getAuditRetention": "admin_scope",
    "pruneAuditRetention": "admin_scope",
}

# Per-operation path-parameter values where the generic synthetic id would not
# parse (or where a realistic value keeps the request representative). Params
# not listed here are filled with the 32-char synthetic id.
PATH_PARAM_OVERRIDES: dict[str, dict[str, str]] = {
    "getWorkItem": {"key": "PHX-241"},
    "enrichWorkItem": {"key": "PHX-241"},
    # `{key:path}` segment: include a slash to exercise the path converter.
    "getArtifact": {"key": "transcripts/" + "x" * 32},
}

# Minimal valid-enough JSON bodies per operation (default: {} — a 422 after the
# authz decision is perfectly fine evidence; these overrides exist only where {}
# would not even be the right JSON *shape*).
BODY_OVERRIDES: dict[str, Any] = {
    "getPipelineExecutionReadiness": {"engine": "sim", "phases": ["execution"]},
    "approveEnvironmentTarget": {"expected_target_version": 0},
    "dispatchCycleExecution": {"expected_revision": 1, "idempotency_key": "matrix-dispatch"},
    "cancelCycleExecutionDispatch": {"dispatch_id": "x" * 32},
    "setProjectLabel": {"project_id": "proj-matrix", "name": "Matrix project"},
    # putHostMappings replaces the full mapping list — the body is a JSON array.
    "putHostMappings": [],
    "resumeGate": {"action": "approve"},
    "rerunPipeline": {
        "phases": ["execution"],
        "gates_mode": "inherit",
        "idempotency_key": "matrix-rerun",
    },
    # createPipelineRun requires a non-empty title; provide one so operator/admin
    # reach the handler (the exploding loopback stub then 5xxs — fine post-authz).
    "createPipelineRun": {"title": "matrix-run"},
    "createApexLoadScenario": {"payload": {}},
    "validateApexLoadScenario": {"payload": {}},
    "sizeApexLoadScenario": {"payload": {}},
    "updateApexLoadScenario": {"payload": {}},
    "createApexLoadSchedule": {"payload": {}},
    "updateApexLoadSchedule": {"payload": {}},
    "decideApexLoadScheduleApproval": {"approved": True},
}

OUT_OF_SCOPE_PROJECT = "proj-matrix-other"

# Scope-denial cases for operations where the caller can supply the project in
# the request itself. Resource-owner checks that require loading a row still live
# in router-specific tests and return 404 to avoid existence leaks.
SCOPE_DENIAL_CASES: dict[str, dict[str, Any]] = {
    "listAnalysisConnections": {"params": {"project_id": OUT_OF_SCOPE_PROJECT}},
    "listProjectLabels": {"params": {"project": OUT_OF_SCOPE_PROJECT}},
    "setProjectLabel": {"json": {"project_id": OUT_OF_SCOPE_PROJECT, "name": "Forbidden"}},
    "deleteProjectLabel": {"params": {"project": OUT_OF_SCOPE_PROJECT}},
    "getAgentAnalytics": {"params": {"project": OUT_OF_SCOPE_PROJECT}},
    "getUsageAnalytics": {"params": {"project": OUT_OF_SCOPE_PROJECT}},
    "listApplications": {"params": {"project": OUT_OF_SCOPE_PROJECT}},
    "createApplication": {"json": {"project_id": OUT_OF_SCOPE_PROJECT, "name": "matrix-app"}},
    "listContextEvidence": {"params": {"project": OUT_OF_SCOPE_PROJECT}},
    "createContextSummary": {
        "json": {"subject": "matrix-summary", "project_id": OUT_OF_SCOPE_PROJECT}
    },
    "listDocuments": {"params": {"project": OUT_OF_SCOPE_PROJECT}},
    "uploadDocument": {
        "data": {"project_id": OUT_OF_SCOPE_PROJECT},
        "files": {"file": ("matrix.txt", b"scope matrix", "text/plain")},
    },
    "listDrafts": {"params": {"project": OUT_OF_SCOPE_PROJECT}},
    "createDraft": {"json": {"title": "matrix-draft", "project_id": OUT_OF_SCOPE_PROJECT}},
    "getWorkTrackingBinding": {"params": {"project": OUT_OF_SCOPE_PROJECT}},
    "searchLogs": {"json": {"query": {"filters": {"project_id": OUT_OF_SCOPE_PROJECT}}}},
    "listPipelines": {"params": {"project": OUT_OF_SCOPE_PROJECT}},
    "createPipelineRun": {"json": {"title": "matrix-run", "project_id": OUT_OF_SCOPE_PROJECT}},
    "listSavedQueries": {"params": {"project": OUT_OF_SCOPE_PROJECT}},
    "createSavedQuery": {
        "json": {
            "name": "matrix-query",
            "provider": "jira",
            "query": "project = PHX",
            "project_id": OUT_OF_SCOPE_PROJECT,
        }
    },
    "createConsumer": {
        "json": {
            "name": "matrix-child",
            "consumer_type": "headless",
            "role": "viewer",
            "scopes": [{"project_id": OUT_OF_SCOPE_PROJECT}],
        }
    },
    "createConnection": {
        "json": {
            "kind": "work_tracking",
            "provider": "stub",
            "name": "matrix-connection",
            "project_id": OUT_OF_SCOPE_PROJECT,
        }
    },
}

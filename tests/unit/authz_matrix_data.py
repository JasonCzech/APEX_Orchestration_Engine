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
    # ── system ──────────────────────────────────────────────────────────────
    "getSystemInfo": "viewer",
    # ── auth / principal introspection ──────────────────────────────────────
    "getAuthMe": "viewer",
    # ── pipelines ───────────────────────────────────────────────────────────
    "createPipelineRun": "operator",
    "listPipelines": "viewer",
    "getPipeline": "viewer",
    "getPhasePromptReview": "viewer",
    "patchPhasePromptReview": "operator",
    "resumeGate": "operator",
    "abortPipeline": "operator",
    # ── prompts ─────────────────────────────────────────────────────────────
    "listPrompts": "viewer",
    "createPrompt": "operator",
    "getPrompt": "viewer",
    "savePromptVersion": "operator",
    "listPromptVersions": "viewer",
    "getPromptVersion": "viewer",
    "rollbackPrompt": "operator",
    "archivePrompt": "operator",
    "unarchivePrompt": "operator",
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
    "deleteEnvironment": "operator",
    # ── documents / artifacts ───────────────────────────────────────────────
    "uploadDocument": "operator",
    "listDocuments": "viewer",
    "getDocument": "viewer",
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
}

SCOPE: dict[str, str] = {
    # ── system ──────────────────────────────────────────────────────────────
    "getSystemInfo": "none",
    # ── auth / principal introspection ──────────────────────────────────────
    "getAuthMe": "none",
    # ── pipelines ───────────────────────────────────────────────────────────
    "createPipelineRun": "project_app",
    "listPipelines": "project",
    "getPipeline": "project",
    "getPhasePromptReview": "project",
    "patchPhasePromptReview": "project",
    "resumeGate": "project",
    "abortPipeline": "project",
    # ── prompts ─────────────────────────────────────────────────────────────
    "listPrompts": "none",
    "createPrompt": "none",
    "getPrompt": "none",
    "savePromptVersion": "none",
    "listPromptVersions": "none",
    "getPromptVersion": "none",
    "rollbackPrompt": "none",
    "archivePrompt": "none",
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
    "deleteEnvironment": "project_app",
    # ── documents / artifacts ───────────────────────────────────────────────
    "uploadDocument": "project_app",
    "listDocuments": "project",
    "getDocument": "project_app",
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
    # putHostMappings replaces the full mapping list — the body is a JSON array.
    "putHostMappings": [],
    "resumeGate": {"action": "approve"},
    # createPipelineRun requires a non-empty title; provide one so operator/admin
    # reach the handler (the exploding loopback stub then 5xxs — fine post-authz).
    "createPipelineRun": {"title": "matrix-run"},
}

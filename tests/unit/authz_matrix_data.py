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
    # ── pipelines ───────────────────────────────────────────────────────────
    "listPipelines": "viewer",
    "getPipeline": "viewer",
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
}

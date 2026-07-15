"""Loopback access to this server's own LangGraph API from custom /v1 routes.

Inside a LangGraph deployment (including `langgraph dev`), `get_client()` with no URL
uses the in-process loopback transport (see langgraph_api/server.py). Always forward
the caller's API key so authorization scoping and actor attribution apply to loopback
calls exactly as they would to direct calls.

Every in-process facade call carries a process-local capability header. This lets
the outer HTTP guard distinguish validated `/v1` reads from public direct-runtime
requests whose projection fields are not exposed to LangGraph auth handlers. The
random token never enters graph state or an API response. Destructive operations
remain reachable only from facade code that invokes them after its own checks.
"""

import secrets
from collections.abc import Mapping

from langgraph_sdk import get_client
from langgraph_sdk.client import LangGraphClient

TRUSTED_LOOPBACK_CLAIM = "apex_trusted_loopback"
LAUNCH_ROOT_FINGERPRINT_METADATA_KEY = "apex_launch_root_fingerprint"
RERUN_CLAIM_METADATA_KEY = "apex_rerun_claim"
RERUN_FINGERPRINT_METADATA_KEY = "apex_rerun_fingerprint"
_TRUSTED_LOOPBACK_HEADER = "x-apex-trusted-loopback"
_TRUSTED_LOOPBACK_TOKEN = secrets.token_urlsafe(32)


def is_trusted_loopback(headers: Mapping[bytes, bytes]) -> bool:
    """Return whether ``headers`` carries this process's loopback capability."""

    supplied = headers.get(_TRUSTED_LOOPBACK_HEADER.encode())
    if supplied is None:
        return False
    try:
        value = supplied.decode()
    except UnicodeDecodeError:
        return False
    return secrets.compare_digest(value, _TRUSTED_LOOPBACK_TOKEN)


def loopback_client(
    api_key: str | None = None, *, authorize_destructive: bool = False
) -> LangGraphClient:
    # NB: x-api-key is a RESERVED header in langgraph_sdk — it must flow through the
    # api_key parameter (the SDK sets the header itself); passing it via headers raises.
    # `authorize_destructive` is retained for source compatibility; all loopback
    # calls are process-trusted, while public callers cannot forge the token.
    del authorize_destructive
    headers = {_TRUSTED_LOOPBACK_HEADER: _TRUSTED_LOOPBACK_TOKEN}
    return get_client(api_key=api_key, headers=headers)

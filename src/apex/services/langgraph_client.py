"""Loopback access to this server's own LangGraph API from custom /v1 routes.

Inside a LangGraph deployment (including `langgraph dev`), `get_client()` with no URL
uses the in-process loopback transport (see langgraph_api/server.py). Always forward
the caller's API key so authorization scoping and actor attribution apply to loopback
calls exactly as they would to direct calls.

Destructive graph operations need one additional distinction: a public caller
must not be able to cancel a pipeline run without first executing APEX's external
engine kill switch.  Selected ``/v1`` facade services therefore opt into a
process-local capability header.  The custom LangGraph authenticator converts a
valid header into a boolean claim; the random token itself never enters graph
state or an API response.
"""

import secrets
from collections.abc import Mapping

from langgraph_sdk import get_client
from langgraph_sdk.client import LangGraphClient

TRUSTED_LOOPBACK_CLAIM = "apex_trusted_loopback"
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
    headers = {_TRUSTED_LOOPBACK_HEADER: _TRUSTED_LOOPBACK_TOKEN} if authorize_destructive else None
    return get_client(api_key=api_key, headers=headers)

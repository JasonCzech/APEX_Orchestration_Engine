"""Loopback access to this server's own LangGraph API from custom /v1 routes.

Inside a LangGraph deployment (including `langgraph dev`), `get_client()` with no URL
uses the in-process loopback transport (see langgraph_api/server.py). Always forward
the caller's API key so authorization scoping and actor attribution apply to loopback
calls exactly as they would to direct calls.
"""

from langgraph_sdk import get_client
from langgraph_sdk.client import LangGraphClient


def loopback_client(api_key: str | None = None) -> LangGraphClient:
    headers = {"x-api-key": api_key} if api_key else None
    return get_client(headers=headers)

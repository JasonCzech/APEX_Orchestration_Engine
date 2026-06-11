"""Consumer identity + API-key auth shared by the LangGraph and /v1 surfaces (ADR-0003).

`apex.auth.handlers` (the langgraph.json auth target) is intentionally not imported
here so the /v1 surface never depends on langgraph_sdk.
"""

from apex.auth.identity import ConsumerIdentity, ConsumerType, Role, ScopeRef
from apex.auth.service import (
    IdentityResolver,
    extract_api_key,
    get_default_resolver,
    hash_api_key,
)

__all__ = [
    "ConsumerIdentity",
    "ConsumerType",
    "IdentityResolver",
    "Role",
    "ScopeRef",
    "extract_api_key",
    "get_default_resolver",
    "hash_api_key",
]

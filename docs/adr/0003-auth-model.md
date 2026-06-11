# ADR-0003: API-consumer identity on both surfaces

**Status:** accepted (2026-06-11)

## Decision
Identity = API consumer (hashed `x-api-key`, type, role `viewer|operator|admin`,
explicit project/app scopes), stored in `apex.api_consumers`. LangGraph custom auth
(`@auth.authenticate` / `@auth.on`) enforces it on the built-in surface (stamping and
filtering thread metadata by project scope); the same `IdentityResolver` backs a
FastAPI dependency on `/v1`. CORS/origins are defense-in-depth only — never an
authorization input (legacy origin-bucketing is retired).

## Consequences
- One key works on both surfaces; graph nodes read identity from
  `configurable.langgraph_auth_user` for attribution and connection scoping only.
- Custom auth is an Enterprise self-hosted feature — see ADR-0001 licensing gate.
- Server-side enforcement is canonical; any client-side role gating is UX only.

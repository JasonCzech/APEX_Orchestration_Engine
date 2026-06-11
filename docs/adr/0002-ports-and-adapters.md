# ADR-0002: Ports & adapters with deterministic stub providers

**Status:** accepted (2026-06-11)

## Decision
Every external system (work tracking, log search, observability, documents, cluster
inventory, source control, execution engines, artifact store, secrets) is reached only
through an async `typing.Protocol` port. Adapters are built by an `AdapterRegistry`
keyed on `(port_kind, provider)` from admin-managed connection rows; secrets are
resolved at build time via `SecretsPort` indirection (`env:`, `vault:`, ...).

Every port ships a deterministic `"stub"` provider with canned fixtures. Stubs are
production code: the full pipeline must run end-to-end offline (dev, CI, demos).

## Rationale
Preserves the legacy rule "routers and agents call integration clients, never embed
protocol", makes connection CRUD double as runtime adapter configuration, and lets the
walking skeleton (M1) ship before any real integration exists.

## Consequences
Graph nodes and routers resolve adapters through the same `ConnectionResolver`
(project-scoped), so the API and the graph can never disagree about which external
system a project talks to. Real adapters (Jira/ADO/ELK/k8s, LoadRunner/APEX Load)
land incrementally behind unchanged interfaces.

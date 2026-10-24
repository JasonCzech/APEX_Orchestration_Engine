# Domain API reference

The browser below is generated directly from the committed domain OpenAPI contract. It searches operation paths, IDs, summaries, descriptions, and groups. It does not send requests or need a live backend Swagger endpoint.

## Find an operation

Enter a path, operation ID, or descriptive term in **Search API operations**. Narrow by **API group**. Open an operation to inspect its method, path, parameters, request body, responses, and authentication information. Copy operation link preserves an exact operation query for sharing.

Parameters identify their location, required state, and value schema. Request bodies identify content types and schemas. Response entries use HTTP status codes and identify response body schemas. OpenAPI `$ref` values refer to named component schemas.

## Inspect schemas

Use **Schema name** in Schema lookup to inspect the component named by a reference such as `#/components/schemas/StartPipelineRequest`. Inspect nested references in the same way. Required fields are listed by the containing object; nullable and optional are separate concepts.

Use **Download OpenAPI contract** for a copy of the complete bundled schema, including fields that do not fit a short workflow example. Generated client/types and the contract are maintained together by the repository's drift checks.

## Check authentication and compatibility

Domain calls use `x-api-key` and are subject to server-side role, audience, scope, and operation policy. The generated schema does not encode every authorization rule. Listing an operation here does not grant permission to call it.

Compare the connected system version with the dashboard build when investigating drift. A locked-down deployment can disable `/v1/docs` and `/v1/openapi.json`; this bundled reference remains available. The bundled contract describes its release, not arbitrary changes in a separately deployed backend.

Start with [Authentication & API workflows](api-workflows.md) for safe read examples and retry guidance. Native LangGraph Assistants/Threads/Runs/streams are outside this domain specification; consult the official SDK and deployment-matched native documentation for those APIs.

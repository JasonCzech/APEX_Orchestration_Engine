# @apex/api-client

Generated TypeScript types for the APEX Orchestration Engine domain API (`/v1`).

`src/schema.d.ts` is generated from the committed OpenAPI contract at
`docs/api/apex-v1.openapi.json` via [openapi-typescript](https://openapi-ts.dev/)
and is **committed to git** — the spec and the generated types move in lockstep
with the server code (same principle as the CI drift gate on the spec itself).

## Regenerating

After any API change:

```sh
make openapi                 # re-export docs/api/apex-v1.openapi.json
scripts/generate_sdks.sh     # regenerate this package's src/schema.d.ts
```

or from this directory:

```sh
npm run generate
```

Then commit both the spec and `src/schema.d.ts`.

## Usage

This package ships types only (no runtime). Consumers (e.g. `apps/dashboard`,
which joins the npm workspace in D0) pair it with a typed fetch wrapper such as
`openapi-fetch`:

```ts
import type { paths } from "@apex/api-client";
```

Authentication: every request needs the `x-api-key` header; mutations require an
operator+ key and `/v1/admin/*` routes require admin (see the server's auth docs).

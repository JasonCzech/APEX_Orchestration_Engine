#!/usr/bin/env bash
# Regenerate client SDKs from the committed OpenAPI contract.
#
# Inputs : docs/api/apex-v1.openapi.json   (refresh first with `make openapi`)
# Outputs: packages/api-client/src/schema.d.ts  (COMMIT the result — generated
#          types move in lockstep with the spec, mirroring the CI drift gate)
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SPEC="${REPO_ROOT}/docs/api/apex-v1.openapi.json"

if [[ ! -f "${SPEC}" ]]; then
  echo "generate_sdks: missing ${SPEC}; run 'make openapi' first" >&2
  exit 2
fi

# --- TypeScript types (openapi-typescript) -----------------------------------
if command -v npx >/dev/null 2>&1; then
  echo "generate_sdks: generating packages/api-client/src/schema.d.ts"
  mkdir -p "${REPO_ROOT}/packages/api-client/src"
  (cd "${REPO_ROOT}/packages/api-client" && npx --yes openapi-typescript \
    "${SPEC}" -o src/schema.d.ts)
else
  echo "generate_sdks: npx not found — skipping TypeScript client generation" >&2
  echo "generate_sdks: install Node.js, then re-run this script" >&2
  exit 3
fi

# --- Python client (placeholder) ----------------------------------------------
# openapi-python-client is intentionally NOT a project dependency (pyproject is
# frozen through M2). When a generated Python SDK is needed, run it as an
# isolated tool so it never touches the locked environment, e.g.:
#
#   uvx openapi-python-client generate \
#     --path docs/api/apex-v1.openapi.json \
#     --output-path packages/api-client-py --overwrite
#
# and add a packages/api-client-py workspace entry at that point.

echo "generate_sdks: done"

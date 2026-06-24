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
GENERATOR="${REPO_ROOT}/packages/api-client/node_modules/.bin/openapi-typescript"
if [[ ! -x "${GENERATOR}" ]]; then
  echo "generate_sdks: missing openapi-typescript; run 'npm ci' first" >&2
  exit 3
fi
echo "generate_sdks: generating packages/api-client/src/schema.d.ts"
mkdir -p "${REPO_ROOT}/packages/api-client/src"
(cd "${REPO_ROOT}/packages/api-client" && "${GENERATOR}" "${SPEC}" -o src/schema.d.ts)

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

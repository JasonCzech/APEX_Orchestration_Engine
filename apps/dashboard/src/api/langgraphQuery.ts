/** Adapt the SDK's query serialization to APEX's bounded native read contract.
 * Do not repair invalid or ambiguous input: the server must still reject it. */
export function canonicalNativeLangGraphRequest(
  input: RequestInfo | URL,
  init: RequestInit | undefined,
  nativeOrigin: string,
): RequestInfo | URL {
  const method = (init?.method ?? (input instanceof Request ? input.method : 'GET')).toUpperCase()
  if (method !== 'GET') return input
  const url = new URL(input instanceof Request ? input.url : String(input), nativeOrigin)
  if (url.origin !== new URL(nativeOrigin).origin) return input
  let changed = false

  if (/^\/threads\/[^/]+\/runs\/?$/.test(url.pathname)) {
    const values = url.searchParams.getAll('select')
    if (values.length === 1) {
      try {
        const fields: unknown = JSON.parse(values[0]!)
        if (Array.isArray(fields) && fields.length > 0 && fields.every(field => typeof field === 'string')) {
          url.searchParams.delete('select')
          fields.forEach(field => url.searchParams.append('select', field))
          changed = true
        }
      } catch { /* Already canonical or invalid: leave server validation authoritative. */ }
    }
  }
  if (/^\/threads\/[^/]+\/runs\/[^/]+\/stream\/?$/.test(url.pathname)) {
    const values = url.searchParams.getAll('cancel_on_disconnect')
    if (values.length === 1 && values[0] === '0') {
      url.searchParams.set('cancel_on_disconnect', 'false')
      changed = true
    }
  }
  if (!changed) return input
  // GET requests have no body; preserve Request headers, credentials and abort
  // signal if a future SDK version supplies a Request rather than a URL.
  return input instanceof Request ? new Request(url, input) : url
}

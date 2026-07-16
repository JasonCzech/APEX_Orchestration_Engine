/**
 * Browser fetch follows redirects by default. APEX requests often carry a
 * browser-held API key, so every production transport rejects 3xx responses
 * before a redirected request can inherit custom authentication headers.
 */
export function fetchWithoutRedirects(
  input: RequestInfo | URL,
  init?: RequestInit,
  transport: typeof fetch = globalThis.fetch,
): Promise<Response> {
  return transport(input, { ...init, redirect: 'error' })
}

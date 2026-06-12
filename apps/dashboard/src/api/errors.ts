/** Single error shape shared by both API clients (plan Part 2: "one ApiError shape"). */
export class ApiError extends Error {
  readonly status: number
  readonly body: unknown

  constructor(status: number, message: string, body?: unknown) {
    super(message)
    this.name = 'ApiError'
    this.status = status
    this.body = body
  }
}

export function isApiError(error: unknown): error is ApiError {
  return error instanceof ApiError
}

/** Best-effort extraction of FastAPI-style `{detail: string}` error bodies. */
export function errorMessageOf(body: unknown, fallback: string): string {
  if (
    body !== null &&
    typeof body === 'object' &&
    'detail' in body &&
    typeof (body as { detail?: unknown }).detail === 'string'
  ) {
    return (body as { detail: string }).detail
  }
  return fallback
}

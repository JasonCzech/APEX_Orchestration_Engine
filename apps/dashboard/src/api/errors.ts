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

/** RFC problem titles and FastAPI details share the same visible error path. */
export function errorMessageOf(body: unknown, fallback: string): string {
  if (body === null || typeof body !== 'object') return fallback
  const problem = body as { detail?: unknown; title?: unknown }
  if (typeof problem.detail === 'string' && problem.detail.trim()) return problem.detail
  if (Array.isArray(problem.detail)) {
    const messages = problem.detail.flatMap((item: unknown) => {
      if (!item || typeof item !== 'object' || !('msg' in item) || typeof item.msg !== 'string') return []
      const location = 'loc' in item && Array.isArray(item.loc)
        ? item.loc.filter((part: unknown) => typeof part === 'string' || typeof part === 'number').filter(part => part !== 'body').join('.')
        : ''
      return [location ? `${location}: ${item.msg}` : item.msg]
    })
    if (messages.length) return messages.join('; ')
  }
  if (typeof problem.title === 'string' && problem.title.trim()) return problem.title
  return fallback
}

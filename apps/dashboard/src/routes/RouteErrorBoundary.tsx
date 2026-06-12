import { isRouteErrorResponse, useNavigate, useRouteError } from 'react-router'

import { ProblemCard } from '@/components/ProblemCard'

function describe(error: unknown): { title: string; message: string } {
  if (isRouteErrorResponse(error)) {
    return {
      title: error.status === 404 ? 'Not found' : `Request failed (${error.status})`,
      message:
        typeof error.data === 'string' && error.data
          ? error.data
          : error.statusText || 'The route could not be loaded.',
    }
  }
  if (error instanceof Error) {
    return { title: 'Something went wrong', message: error.message }
  }
  return { title: 'Something went wrong', message: 'An unexpected error occurred.' }
}

/**
 * Route-level error element: APEX Load problem-card with a retry that
 * re-navigates to the current location (re-attempting lazy chunks/loaders).
 */
export function RouteErrorBoundary() {
  const error = useRouteError()
  const navigate = useNavigate()
  const { title, message } = describe(error)

  return (
    <ProblemCard
      title={title}
      message={message}
      onRetry={() => {
        void navigate(`${window.location.pathname}${window.location.search}`, { replace: true })
      }}
    />
  )
}

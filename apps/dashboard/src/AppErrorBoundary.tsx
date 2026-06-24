import { Component, type ErrorInfo, type ReactNode } from 'react'

import { ProblemCard } from '@/components/ProblemCard'

interface AppErrorBoundaryProps {
  children: ReactNode
}

interface AppErrorBoundaryState {
  error: Error | null
}

export class AppErrorBoundary extends Component<AppErrorBoundaryProps, AppErrorBoundaryState> {
  state: AppErrorBoundaryState = { error: null }

  static getDerivedStateFromError(error: Error): AppErrorBoundaryState {
    return { error }
  }

  componentDidCatch(error: Error, info: ErrorInfo): void {
    console.error('[AppErrorBoundary] render failure', { error, componentStack: info.componentStack })
  }

  render() {
    if (!this.state.error) return this.props.children

    return (
      <ProblemCard
        title="Something went wrong"
        message={this.state.error.message || 'The dashboard could not render.'}
        onRetry={() => {
          this.setState({ error: null })
        }}
      />
    )
  }
}

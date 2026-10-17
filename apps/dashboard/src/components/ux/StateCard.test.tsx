import { render, screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import { StateCard, type StateKind } from './StateCard'
import { Skeleton } from './Skeleton'

describe('shared resource states', () => {
  it.each<StateKind>(['empty', 'filtered', 'not-found', 'unauthorized', 'unconfigured', 'stale'])('identifies %s without a live region and provides its next action', kind => {
    render(<StateCard kind={kind} title="Available state" action={<button>Continue</button>}>State details</StateCard>)
    expect(screen.getByRole('region', { name: 'Available state' })).toHaveAttribute('data-state', kind)
    expect(screen.getByRole('button', { name: 'Continue' })).toBeVisible()
    expect(screen.queryByRole('status')).not.toBeInTheDocument()
  })
  it('announces loading once and hides decorative rows', () => {
    render(<Skeleton label="Loading connections" rows={4} />)
    const status = screen.getByRole('status', { name: 'Loading connections' })
    expect(status).toHaveAttribute('aria-busy', 'true')
    expect(status.querySelectorAll('[aria-hidden="true"]')).toHaveLength(4)
    expect(status).toHaveTextContent('Loading connections…')
  })
})

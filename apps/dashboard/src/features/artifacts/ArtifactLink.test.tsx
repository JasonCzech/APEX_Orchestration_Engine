import { describe, expect, it } from 'vitest'
import { render, screen } from '@testing-library/react'
import { MemoryRouter } from 'react-router'
import { ArtifactLink, artifactReturnPath } from './ArtifactLink'

describe('artifact return context', () => {
  it.each(['https://evil.test', '//evil.test', '/\\evil.test', '/settings', '/runs/a\n', undefined, { path: '/approvals' }])('rejects unsafe or unrelated targets: %s', (target) => {
    expect(artifactReturnPath(target, 'run-a')).toBe('/runs/run-a')
  })
  it('permits approval and phase deep links', () => {
    expect(artifactReturnPath('/approvals/run-a/int-a', 'run-a')).toBe('/approvals/run-a/int-a')
    expect(artifactReturnPath('/runs/run-a/phases/reporting?tab=details', 'run-a')).toBe('/runs/run-a/phases/reporting?tab=details')
  })
  it('keeps artifact URLs directly usable', () => {
    render(<MemoryRouter initialEntries={['/runs/a/phases/reporting']}><ArtifactLink to="/runs/a/artifacts/report">Read report</ArtifactLink></MemoryRouter>)
    expect(screen.getByRole('link')).toHaveAttribute('href', '/runs/a/artifacts/report')
  })
})

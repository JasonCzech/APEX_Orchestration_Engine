import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { http, HttpResponse } from 'msw'
import { describe, expect, it } from 'vitest'
import { server } from '@/test/server'
import { CycleActorNames } from '../CycleActorIdentity'
import { ProviderValidation } from '../ProviderValidation'

const record = {
  id: 'operation',
  preparation_id: 'prep',
  request_key: 'original-request',
  status: 'pending',
  issues: [],
  warnings: [],
  created_by: 'operator',
  created_at: '2026-09-07T12:00:00Z',
  finished_at: null,
  current: false,
}
function mount(canEdit = true, archived = false) {
  render(
    <QueryClientProvider
      client={
        new QueryClient({ defaultOptions: { queries: { retry: false } } })
      }
    >
      <CycleActorNames cycleId="cycle" revision={8}><ProviderValidation
        cycleId="cycle"
        preparationId="prep"
        canEdit={canEdit}
        archived={archived}
      /></CycleActorNames>
    </QueryClientProvider>,
  )
}
describe('Provider source validation', () => {
  it('resumes the recorded operation and announces rejection without claiming a test result', async () => {
    const user = userEvent.setup()
    let submitted: unknown
    server.use(
      http.get('*/v1/cycles/cycle/executions/prep/script-validations', () =>
        HttpResponse.json({ revision: 8, blockers: [], validations: [record] }),
      ),
      http.post(
        '*/v1/cycles/cycle/executions/prep/script-validations',
        async ({ request }) => {
          submitted = await request.json()
          return HttpResponse.json({
            ...record,
            status: 'rejected',
            issues: ['Missing HTTP method'],
          })
        },
      ),
    )
    mount()
    await user.click(
      await screen.findByRole('button', { name: 'Resume provider validation' }),
    )
    await waitFor(() =>
      expect(submitted).toEqual({
        expected_revision: 8,
        idempotency_key: 'original-request',
      }),
    )
    expect(
      await screen.findByText(
        'Source rejected by provider. No test was launched.',
      ),
    ).toHaveAttribute('role', 'status')
    expect(
      screen.queryByRole('button', { name: /^Launch/ }),
    ).not.toBeInTheDocument()
  })
  it('blocks unsupported capabilities and keeps old acceptance historical', async () => {
    server.use(
      http.get('*/v1/cycles/cycle/executions/prep/script-validations', () =>
        HttpResponse.json({
          revision: 8,
          blockers: ['LoadRunner source validation is unavailable'],
          validations: [{ ...record, status: 'accepted' }],
        }),
      ),
    )
    mount()
    expect(
      await screen.findByRole('button', { name: 'Validate with APEX Load' }),
    ).toBeDisabled()
    expect(screen.getByText(/Historical acceptance/)).toBeInTheDocument()
    expect(
      screen.getByText('LoadRunner source validation is unavailable'),
    ).toBeInTheDocument()
  })
  it('labels validation actors from the authorized cycle lookup and retains their recorded ID', async () => {
    server.use(http.get('*/v1/cycles/cycle/actors', () => HttpResponse.json({ names: { operator: 'Validation reviewer' }, truncated: false })), http.get('*/v1/cycles/cycle/executions/prep/script-validations', () => HttpResponse.json({ revision: 8, blockers: [], validations: [{ ...record, status: 'accepted' }] })))
    mount(false)
    expect(await screen.findByText('Validation reviewer')).toBeVisible()
    expect(screen.getByText('operator', { selector: 'code' })).toHaveAttribute('title', 'operator')
    expect(screen.getByRole('button', { name: 'Copy Actor ID' })).toBeVisible()
    expect(screen.queryByRole('button', { name: 'Validate with APEX Load' })).not.toBeInTheDocument()
  })

  it('gives viewers history without validation controls', async () => {
    server.use(
      http.get('*/v1/cycles/cycle/executions/prep/script-validations', () =>
        HttpResponse.json({ revision: 8, blockers: [], validations: [] }),
      ),
    )
    mount(false)
    await screen.findByText('No provider validation recorded.')
    expect(
      screen.queryByRole('button', { name: 'Validate with APEX Load' }),
    ).not.toBeInTheDocument()
  })
  it.each([
    ['pending', 'Historical validation remains pending'],
    ['accepted', 'Historical acceptance retained for this archived preparation.'],
  ])('keeps archived %s history readable without suggesting unavailable actions', async (status, expected) => {
    server.use(http.get('*/v1/cycles/cycle/executions/prep/script-validations', () =>
      HttpResponse.json({ revision: 9, blockers: ['Preparation archived'], validations: [{ ...record, status }] }),
    ))
    mount(true, true)
    await screen.findByText(expected)
    expect(screen.queryByRole('button', { name: /Validate with|Resume provider/ })).not.toBeInTheDocument()
    expect(screen.queryByText(/resume to recover|validate again/i)).not.toBeInTheDocument()
  })

})

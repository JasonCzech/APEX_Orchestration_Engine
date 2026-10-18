import { render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router'
import { http, HttpResponse } from 'msw'
import { describe, expect, it } from 'vitest'
import type { components } from '@apex/api-client'
import { server } from '@/test/server'
import { ExecutionDispatch } from '../ExecutionDispatch'
import type { DispatchAttempt, DispatchWorkspace } from '../dispatchApi'

const timestamp = '2026-10-17T12:00:00Z'
const preparation: components['schemas']['PreparationRead'] = {
  id: 'prep', created_at: timestamp, created_by: 'operator', checked_at: timestamp,
  checks: [], launch_available: false, archived_at: null, archived_by: null,
  snapshot: {
    plan_version_id: 'plan', plan_title: 'Checkout plan', plan_checksum: 'plan-checksum',
    script_version_id: 'script', script_title: 'Approved checkout', script_checksum: 'script-checksum',
    script_source_checksum: 'source-checksum', script_format: 'apex_native_json',
    readiness_version_id: 'readiness', readiness_checksum: 'readiness-checksum',
    connection_id: 'connection', connection_version: timestamp, environment_id: 'environment',
    environment_version: 2, engine: 'apex-load', mode: 'live', launch_authority: 'pipeline',
    execution_policy: 'sequential', workload: { vusers: 1, ramp_s: 1, duration_s: 5 },
  },
}
const receipt: DispatchAttempt = {
  id: 'dispatch', preparation_id: 'prep', request_key: 'original-key', status: 'uncertain',
  created_by: 'operator', created_at: timestamp, updated_at: timestamp,
  thread_id: null, run_id: null, engine_run_id: null, external_run_id: null,
  detail: 'The original launch may have been accepted. Recover its receipt.',
}
const ready: DispatchWorkspace = { revision: 7, checks: [], can_dispatch: true, can_resume: false, can_cancel: false, attempt: null }
const endpoint = '*/v1/cycles/cycle/executions/prep/dispatch'

function mount(canEdit = true, archived = false) {
  return render(<QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })}>
    <MemoryRouter><ExecutionDispatch cycleId="cycle" preparation={{ ...preparation, archived_at: archived ? timestamp : null }} environmentName="Performance staging" canEdit={canEdit} /></MemoryRouter>
  </QueryClientProvider>)
}

describe('Execution dispatch', () => {
  it('requires confirmation of the exact preparation and revision before launch', async () => {
    const user = userEvent.setup()
    let data = ready
    const requests: unknown[] = []
    server.use(http.get(endpoint, () => HttpResponse.json(data)), http.post(endpoint, async ({ request }) => {
      requests.push(await request.json())
      data = { ...ready, can_dispatch: false, attempt: { ...receipt, status: 'submitted', thread_id: 'thread', detail: 'Submitted to orchestration.' } }
      return HttpResponse.json(data)
    }))
    mount()
    await user.click(await screen.findByRole('button', { name: 'Launch approved execution' }))
    let dialog = screen.getByRole('dialog', { name: 'Launch approved execution' })
    expect(dialog).toHaveTextContent('Performance staging')
    expect(dialog).toHaveTextContent('1 VUsers · 1s ramp · 5s duration')
    expect(dialog).toHaveTextContent('Reviewed cycle revision7')
    expect(requests).toHaveLength(0)
    await user.click(within(dialog).getByRole('button', { name: 'Cancel' }))
    expect(requests).toHaveLength(0)
    await user.click(screen.getByRole('button', { name: 'Launch approved execution' }))
    dialog = screen.getByRole('dialog')
    await user.click(within(dialog).getByRole('button', { name: 'Confirm launch' }))
    await waitFor(() => expect(requests).toHaveLength(1))
    expect(requests[0]).toMatchObject({ expected_revision: 7, idempotency_key: expect.stringMatching(/^[a-f0-9]{32}$/) })
    expect(await screen.findByRole('link', { name: 'Open execution and results' })).toHaveAttribute('href', '/runs/thread/phases/execution')
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
  })

  it('recovers a durable uncertain receipt with its original key, only after confirmation', async () => {
    const user = userEvent.setup()
    const data = { ...ready, can_dispatch: false, can_resume: true, attempt: receipt }
    const requests: unknown[] = []
    server.use(http.get(endpoint, () => HttpResponse.json(data)), http.post(endpoint, async ({ request }) => {
      requests.push(await request.json())
      return HttpResponse.json(data)
    }))
    mount()
    await screen.findByText('Dispatch outcome unconfirmed')
    await user.click(screen.getByRole('button', { name: 'Refresh dispatch receipt' }))
    expect(requests).toHaveLength(0)
    await user.click(screen.getByRole('button', { name: 'Resume dispatch' }))
    await user.click(within(screen.getByRole('dialog')).getByRole('button', { name: 'Confirm resume dispatch' }))
    await waitFor(() => expect(requests).toEqual([{ expected_revision: 7, idempotency_key: 'original-key' }]))
  })

  it('keeps an uncertain local key and requires a fresh review after a conflicting revision', async () => {
    const user = userEvent.setup()
    let revision = 7
    const requests: Record<string, unknown>[] = []
    server.use(http.get(endpoint, () => HttpResponse.json({ ...ready, revision })), http.post(endpoint, async ({ request }) => {
      requests.push(await request.json() as Record<string, unknown>)
      revision = 8
      return HttpResponse.json({ detail: 'Cycle changed. Review again.' }, { status: 409 })
    }))
    mount()
    await user.click(await screen.findByRole('button', { name: 'Launch approved execution' }))
    await user.click(within(screen.getByRole('dialog')).getByRole('button', { name: 'Confirm launch' }))
    await within(screen.getByRole('dialog')).findByRole('alert')
    expect(within(screen.getByRole('dialog')).getByRole('button', { name: 'Confirm launch' })).toBeDisabled()
    expect(screen.getByRole('dialog')).toHaveTextContent('Reviewed cycle revision7')
    await user.click(within(screen.getByRole('dialog')).getByRole('button', { name: 'Cancel' }))
    await waitFor(() => expect(screen.getByRole('button', { name: 'Launch approved execution' })).toBeEnabled())
    await user.click(screen.getByRole('button', { name: 'Launch approved execution' }))
    expect(screen.getByRole('dialog')).toHaveTextContent('Reviewed cycle revision8')
    await user.click(within(screen.getByRole('dialog')).getByRole('button', { name: 'Confirm launch' }))
    await waitFor(() => expect(requests).toHaveLength(2))
    expect(requests[1]?.idempotency_key).toBe(requests[0]?.idempotency_key)
    expect(requests[1]?.expected_revision).toBe(8)
  })

  it.each([['viewer', false, false], ['archived', true, true]] as const)('keeps %s receipts readable without mutation controls', async (_name, canEdit, archived) => {
    server.use(http.get(endpoint, () => HttpResponse.json({ ...ready, can_dispatch: false, can_resume: true, attempt: receipt })))
    mount(canEdit, archived)
    await screen.findByText('Dispatch outcome unconfirmed')
    expect(screen.queryByRole('button', { name: /Launch approved execution|Resume dispatch/ })).not.toBeInTheDocument()
  })

  it('keeps blockers explicit and does not suggest relaunching a completed preparation', async () => {
    server.use(http.get(endpoint, () => HttpResponse.json({ ...ready, can_dispatch: false, attempt: { ...receipt, status: 'completed', detail: 'Execution completed; inspect the recorded test result.' }, checks: [{ key: 'approval', label: 'Approved source', status: 'blocked', detail: 'A newer version requires review.' }] })))
    mount()
    await screen.findByText('Execution complete')
    expect(screen.getByRole('list', { name: 'Dispatch prerequisites' })).toHaveTextContent('A newer version requires review.')
    expect(screen.getByText(/Prepare a new execution after review/)).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Launch approved execution' })).not.toBeInTheDocument()
    expect(screen.queryByText(/test passed/i)).not.toBeInTheDocument()
  })

  it('confirms cancellation of an unsubmitted intent even when launch prerequisites changed', async () => {
    const user = userEvent.setup()
    let data: DispatchWorkspace = { ...ready, can_dispatch: false, can_cancel: true, attempt: { ...receipt, status: 'reserved' }, checks: [{ key: 'source', label: 'Source approval', status: 'blocked', detail: 'Review the changed source.' }] }
    const requests: unknown[] = []
    server.use(http.get(endpoint, () => HttpResponse.json(data)), http.post(`${endpoint}/cancel`, async ({ request }) => {
      requests.push(await request.json())
      data = { ...data, can_cancel: false, attempt: { ...receipt, status: 'aborted', canceled_by: 'canceling-operator', detail: 'Launch canceled before submission. No execution was started by this operation.' } }
      return HttpResponse.json(data)
    }))
    mount()
    await user.click(await screen.findByRole('button', { name: 'Cancel unsubmitted launch' }))
    const dialog = screen.getByRole('dialog', { name: 'Cancel unsubmitted launch' })
    expect(dialog).toHaveTextContent('If submission has begun, cancellation is refused')
    expect(requests).toHaveLength(0)
    await user.click(within(dialog).getByRole('button', { name: 'Confirm cancel launch' }))
    await screen.findByText('Dispatch canceled')
    expect(requests).toEqual([{ dispatch_id: 'dispatch' }])
    await user.click(screen.getByText('Dispatch receipt'))
    expect(screen.getByText('Canceled by')).toBeInTheDocument()
    expect(screen.getByText('canceling-operator', { selector: 'code' })).toBeVisible()
    expect(screen.getByText('operator', { selector: 'code' })).toBeVisible()
    expect(screen.queryByRole('button', { name: 'Launch approved execution' })).not.toBeInTheDocument()
  })
  it('explains reconciliation of a bound intent when current launch prerequisites block execution', async () => {
    const user = userEvent.setup()
    const data: DispatchWorkspace = { ...ready, can_dispatch: false, can_resume: true, attempt: { ...receipt, thread_id: 'original-thread' }, checks: [{ key: 'source', label: 'Source approval', status: 'blocked', detail: 'Review the changed source.' }] }
    const requests: unknown[] = []
    server.use(http.get(endpoint, () => HttpResponse.json(data)), http.post(endpoint, async ({ request }) => {
      requests.push(await request.json())
      return HttpResponse.json(data)
    }))
    mount()
    await user.click(await screen.findByRole('button', { name: 'Reconcile launch' }))
    const dialog = screen.getByRole('dialog', { name: 'Reconcile launch' })
    expect(dialog).toHaveTextContent('This does not bypass review or permissions.')
    expect(requests).toHaveLength(0)
    await user.click(within(dialog).getByRole('button', { name: 'Confirm reconciliation' }))
    await waitFor(() => expect(requests).toEqual([{ expected_revision: 7, idempotency_key: 'original-key' }]))
  })

  it('keeps unconfirmed cleanup separate from running and never offers a second launch', async () => {
    server.use(http.get(endpoint, () => HttpResponse.json({ ...ready, can_dispatch: false, attempt: { ...receipt, status: 'cleanup_pending', thread_id: 'thread', detail: 'Result collection or cleanup is not yet confirmed.' } })))
    mount()
    await screen.findByText('Awaiting results and cleanup')
    expect(screen.queryByText('Execution running')).not.toBeInTheDocument()
    expect(screen.getByRole('link', { name: 'Open execution and results' })).toHaveAttribute('href', '/runs/thread/phases/execution')
    expect(screen.queryByRole('button', { name: 'Launch approved execution' })).not.toBeInTheDocument()
  })

})

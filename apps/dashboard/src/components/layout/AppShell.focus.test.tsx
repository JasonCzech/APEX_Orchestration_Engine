import { act, screen } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'
import { http, HttpResponse } from 'msw'
import { authenticatedState, renderApp } from '@/test/render'
import { server } from '@/test/server'

describe('deferred route focus', () => {
  it('preserves focus already moved into the destination before its animation frame', async () => {
    server.use(http.get('*/v1/prompts', () => HttpResponse.json([])))
    const frames = new Map<number, FrameRequestCallback>()
    let nextFrame = 1
    const request = vi.spyOn(window, 'requestAnimationFrame').mockImplementation(callback => {
      const id = nextFrame++
      frames.set(id, callback)
      return id
    })
    const cancel = vi.spyOn(window, 'cancelAnimationFrame').mockImplementation(id => { frames.delete(id) })
    try {
      const { router } = renderApp({ initialEntries: ['/runs'], authState: authenticatedState() })
      await screen.findByRole('heading', { level: 1, name: 'Tests' })
      await act(async () => { await router.navigate('/prompts') })
      const search = await screen.findByRole('searchbox', { name: 'Search prompts' })
      search.focus()
      act(() => { for (const callback of frames.values()) callback(performance.now()) })
      expect(search).toHaveFocus()
    } finally {
      request.mockRestore()
      cancel.mockRestore()
    }
  })
})

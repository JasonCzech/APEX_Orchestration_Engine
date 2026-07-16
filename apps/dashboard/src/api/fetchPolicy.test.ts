import { describe, expect, it, vi } from 'vitest'

import { fetchWithoutRedirects } from './fetchPolicy'

describe('authenticated fetch policy', () => {
  it('rejects redirects without dropping caller headers or abort signals', async () => {
    const response = new Response('{}')
    const transport = vi.fn<typeof fetch>().mockResolvedValue(response)
    const controller = new AbortController()
    const headers = new Headers({ 'x-api-key': 'secret-canary' })

    await expect(
      fetchWithoutRedirects(
        'https://api.example.test/v1/system/info',
        { headers, method: 'POST', signal: controller.signal, redirect: 'follow' },
        transport,
      ),
    ).resolves.toBe(response)

    expect(transport).toHaveBeenCalledWith(
      'https://api.example.test/v1/system/info',
      expect.objectContaining({
        headers,
        method: 'POST',
        signal: controller.signal,
        redirect: 'error',
      }),
    )
  })
})

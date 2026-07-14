import { beforeEach, describe, expect, it, vi } from 'vitest'

const { clientConstructor } = vi.hoisted(() => ({
  clientConstructor: vi.fn(),
}))

vi.mock('@langchain/langgraph-sdk', () => ({
  Client: clientConstructor,
}))

import { getLangGraphClient, resetLangGraphClient } from './langgraphClient'

describe('LangGraph client singleton', () => {
  beforeEach(() => {
    resetLangGraphClient()
    clientConstructor.mockReset()
  })

  it('retries construction after the cached promise rejects', async () => {
    const recoveredClient = { assistants: {}, runs: {}, threads: {} }
    clientConstructor
      .mockImplementationOnce(() => {
        throw new Error('transient chunk failure')
      })
      .mockImplementationOnce(() => recoveredClient)

    await expect(getLangGraphClient()).rejects.toThrow('transient chunk failure')
    await expect(getLangGraphClient()).resolves.toBe(recoveredClient)
    expect(clientConstructor).toHaveBeenCalledTimes(2)
  })
})

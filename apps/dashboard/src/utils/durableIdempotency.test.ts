import { beforeEach, describe, expect, it, vi } from 'vitest'

import { bumpSessionRevision, setApiKey } from '@/auth/keyStorage'

import {
  getDurableIdempotencyAttempt,
  getDurableIdempotencyKey,
  idempotencyPayloadSignature,
  PIPELINE_LAUNCH_IDEMPOTENCY_STORAGE_KEY,
  retireDurableIdempotencyAttempt,
  retireDurableIdempotencyKey,
  SAFE_RETRY_RECOVERY_ERROR,
  SAFE_RETRY_STORAGE_ERROR,
  stableIdempotencyPayload,
} from './durableIdempotency'

describe('durable idempotency keys', () => {
  beforeEach(() => {
    window.sessionStorage.clear()
  })

  it('canonicalizes object keys while preserving meaningful array order', async () => {
    expect(
      stableIdempotencyPayload({
        configurable: { gates: { output: 'auto', prompt: 'gated' }, engine: 'sim' },
        phases: ['planning', 'execution'],
      }),
    ).toBe(
      stableIdempotencyPayload({
        phases: ['planning', 'execution'],
        configurable: { engine: 'sim', gates: { prompt: 'gated', output: 'auto' } },
      }),
    )
    expect(stableIdempotencyPayload({ phases: ['execution', 'planning'] })).not.toBe(
      stableIdempotencyPayload({ phases: ['planning', 'execution'] }),
    )
    await expect(idempotencyPayloadSignature({ request: 'abc' })).resolves.toMatch(
      /^[a-f0-9]{64}$/,
    )
  })

  it('recovers the original key after edit/revert and a fresh caller lifecycle', async () => {
    const createKey = vi
      .fn<() => string>()
      .mockReturnValueOnce('key-original')
      .mockReturnValueOnce('key-edited')
    const original = { title: 'Checkout', request: 'Run the soak test' }
    const edited = { title: 'Checkout', request: 'Run the smoke test' }

    expect(
      await getDurableIdempotencyKey(
        PIPELINE_LAUNCH_IDEMPOTENCY_STORAGE_KEY,
        original,
        createKey,
      ),
    ).toBe('key-original')
    expect(
      await getDurableIdempotencyKey(
        PIPELINE_LAUNCH_IDEMPOTENCY_STORAGE_KEY,
        edited,
        createKey,
      ),
    ).toBe('key-edited')
    expect(
      await getDurableIdempotencyKey(
        PIPELINE_LAUNCH_IDEMPOTENCY_STORAGE_KEY,
        { request: 'Run the soak test', title: 'Checkout' },
        createKey,
      ),
    ).toBe('key-original')
    expect(createKey).toHaveBeenCalledTimes(2)
  })

  it('preserves an unresolved basic key across a same-key semantic refresh', async () => {
    const storageKey = 'apex.idempotency.test-same-principal-refresh.v1'
    const payload = { title: 'Ambiguous same-principal launch' }
    setApiKey('apex_same_principal_basic')
    const createKey = vi
      .fn<() => string>()
      .mockReturnValueOnce('original-key')
      .mockReturnValueOnce('unsafe-replacement-key')

    await expect(
      getDurableIdempotencyKey(storageKey, payload, createKey),
    ).resolves.toBe('original-key')
    bumpSessionRevision()
    await expect(
      getDurableIdempotencyKey(storageKey, payload, createKey),
    ).resolves.toBe('original-key')
    expect(createKey).toHaveBeenCalledTimes(1)
  })

  it('retires only a confirmed matching key so a later attempt gets a fresh key', async () => {
    const createKey = vi
      .fn<() => string>()
      .mockReturnValueOnce('key-first')
      .mockReturnValueOnce('key-after-success')
    const payload = { thread_id: 'thread-1', phases: ['execution'] }

    const first = await getDurableIdempotencyKey(
      PIPELINE_LAUNCH_IDEMPOTENCY_STORAGE_KEY,
      payload,
      createKey,
    )
    await retireDurableIdempotencyKey(
      PIPELINE_LAUNCH_IDEMPOTENCY_STORAGE_KEY,
      payload,
      'stale-other-key',
    )
    expect(
      await getDurableIdempotencyKey(
        PIPELINE_LAUNCH_IDEMPOTENCY_STORAGE_KEY,
        payload,
        createKey,
      ),
    ).toBe(first)

    await retireDurableIdempotencyKey(
      PIPELINE_LAUNCH_IDEMPOTENCY_STORAGE_KEY,
      payload,
      first,
    )
    expect(
      await getDurableIdempotencyKey(
        PIPELINE_LAUNCH_IDEMPOTENCY_STORAGE_KEY,
        payload,
        createKey,
      ),
    ).toBe('key-after-success')
  })

  it('does not reuse a completed basic key when retirement storage silently fails', async () => {
    const storageKey = 'apex.idempotency.test-retirement-noop.v1'
    const payload = { title: 'Repeatable launch' }
    const first = await getDurableIdempotencyKey(storageKey, payload, () => 'completed-key')
    const setItem = vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => undefined)

    try {
      await retireDurableIdempotencyKey(storageKey, payload, first)
    } finally {
      setItem.mockRestore()
    }

    bumpSessionRevision()
    expect(window.sessionStorage.getItem(storageKey)).toContain('completed-key')
    await expect(
      getDurableIdempotencyKey(storageKey, payload, () => 'fresh-key'),
    ).resolves.toBe('fresh-key')
    expect(window.sessionStorage.getItem(storageKey)).toContain('fresh-key')
    expect(window.sessionStorage.getItem(storageKey)).not.toContain('completed-key')
  })

  it('quarantines a completed basic key left by a prior page runtime', async () => {
    const storageKey = 'apex.idempotency.test-retirement-reload.v1'
    const payload = { title: 'Repeatable launch after reload' }
    const first = await getDurableIdempotencyKey(storageKey, payload, () => 'completed-key')
    const setItem = vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => undefined)

    try {
      await retireDurableIdempotencyKey(storageKey, payload, first)
    } finally {
      setItem.mockRestore()
    }

    const signature = await idempotencyPayloadSignature(payload)
    const persisted = JSON.parse(window.sessionStorage.getItem(storageKey) ?? 'null')
    persisted.keysByPayload[signature].runtimeId = 'prior-page-runtime'
    persisted.keysByPayload[signature].keyRevision = Number.MAX_SAFE_INTEGER
    window.sessionStorage.setItem(storageKey, JSON.stringify(persisted))
    const createKey = vi.fn(() => 'unsafe-replacement-key')

    await expect(
      getDurableIdempotencyKey(storageKey, payload, createKey),
    ).rejects.toThrow(SAFE_RETRY_RECOVERY_ERROR)
    expect(createKey).not.toHaveBeenCalled()
  })

  it('preserves a prior-runtime basic quarantine while a different request proceeds', async () => {
    const storageKey = 'apex.idempotency.test-basic-quarantine-retention.v1'
    const priorPayload = { title: 'Prior ambiguous request' }
    const nextPayload = { title: 'Different safe request' }
    await getDurableIdempotencyKey(storageKey, priorPayload, () => 'prior-key')

    const priorSignature = await idempotencyPayloadSignature(priorPayload)
    const persisted = JSON.parse(window.sessionStorage.getItem(storageKey) ?? 'null')
    persisted.keysByPayload[priorSignature].runtimeId = 'prior-page-runtime'
    window.sessionStorage.setItem(storageKey, JSON.stringify(persisted))

    await expect(
      getDurableIdempotencyKey(storageKey, nextPayload, () => 'next-key'),
    ).resolves.toBe('next-key')
    const sanitized = window.sessionStorage.getItem(storageKey)
    expect(sanitized).not.toContain('prior-key')
    expect(JSON.parse(sanitized ?? 'null').blockedPayloads[priorSignature]).toBeTruthy()

    await expect(
      getDurableIdempotencyKey(storageKey, priorPayload, () => 'unsafe-replay-key'),
    ).rejects.toThrow(SAFE_RETRY_RECOVERY_ERROR)
  })

  it('does not evict unresolved attempts when many payloads are edited', async () => {
    let sequence = 0
    const createKey = vi.fn(() => `key-${++sequence}`)
    const original = { title: 'Original', request: 'Keep this unresolved' }
    const originalKey = await getDurableIdempotencyKey(
      PIPELINE_LAUNCH_IDEMPOTENCY_STORAGE_KEY,
      original,
      createKey,
    )

    for (let index = 0; index < 40; index += 1) {
      await getDurableIdempotencyKey(
        PIPELINE_LAUNCH_IDEMPOTENCY_STORAGE_KEY,
        { title: `Edit ${index}`, request: `Payload ${index}` },
        createKey,
      )
    }

    await expect(
      getDurableIdempotencyKey(
        PIPELINE_LAUNCH_IDEMPOTENCY_STORAGE_KEY,
        original,
        createKey,
      ),
    ).resolves.toBe(originalKey)
    expect(createKey).toHaveBeenCalledTimes(41)
  })

  it('fails closed when a new basic mapping cannot be verified in session storage', async () => {
    const storageKey = 'apex.idempotency.test-unverified-basic.v1'
    const existing = JSON.stringify({
      version: 1,
      keysByPayload: {
        ['a'.repeat(64)]: 'unrelated-existing-key',
      },
    })
    window.sessionStorage.setItem(storageKey, existing)
    const setItem = vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => undefined)

    try {
      await expect(
        getDurableIdempotencyKey(
          storageKey,
          { request: 'must be safely retryable' },
          () => 'new-unverified-key',
        ),
      ).rejects.toThrow(SAFE_RETRY_STORAGE_ERROR)
      expect(window.sessionStorage.getItem(storageKey)).toBe(existing)
    } finally {
      setItem.mockRestore()
    }
  })

  it('reuses the exact resolved request payload until its attempt succeeds', async () => {
    const storageKey = 'apex.idempotency.test-resolved-attempt.v1'
    const intent = { work_item_keys: ['PHX-241'] }
    const createKey = vi
      .fn<() => string>()
      .mockReturnValueOnce('attempt-key')
      .mockReturnValueOnce('next-attempt-key')
    const resolvePayload = vi
      .fn<() => Promise<{ context_packets: Array<{ text: string }> }>>()
      .mockResolvedValueOnce({
        context_packets: [{ text: 'Original ticket description.' }],
      })
      .mockResolvedValueOnce({
        context_packets: [{ text: 'Ticket changed after the first request.' }],
      })
    const validate = (
      value: unknown,
    ): value is { context_packets: Array<{ text: string }> } =>
      Boolean(
        value &&
          typeof value === 'object' &&
          Array.isArray(
            (value as { context_packets?: unknown }).context_packets,
          ),
      )

    const first = await getDurableIdempotencyAttempt({
      storageKey,
      intent,
      createKey,
      createRequestPayload: resolvePayload,
      validateRequestPayload: validate,
    })
    const retry = await getDurableIdempotencyAttempt({
      storageKey,
      intent,
      createKey,
      createRequestPayload: resolvePayload,
      validateRequestPayload: validate,
    })

    expect(retry).toEqual(first)
    expect(resolvePayload).toHaveBeenCalledTimes(1)

    await retireDurableIdempotencyAttempt(
      storageKey,
      intent,
      first.idempotencyKey,
    )
    const next = await getDurableIdempotencyAttempt({
      storageKey,
      intent,
      createKey,
      createRequestPayload: resolvePayload,
      validateRequestPayload: validate,
    })

    expect(next.idempotencyKey).toBe('next-attempt-key')
    expect(next.requestPayload.context_packets[0]?.text).toBe(
      'Ticket changed after the first request.',
    )
    expect(resolvePayload).toHaveBeenCalledTimes(2)
  })

  it('preserves a resolved attempt across a same-key semantic refresh', async () => {
    const storageKey = 'apex.idempotency.test-attempt-same-principal-refresh.v1'
    const intent = { work_item_keys: ['PHX-242'] }
    const isPacket = (value: unknown): value is { packet: string } =>
      Boolean(
        value &&
          typeof value === 'object' &&
          typeof (value as { packet?: unknown }).packet === 'string',
      )
    setApiKey('apex_same_principal_attempt')
    const resolvePayload = vi
      .fn<() => Promise<{ packet: string }>>()
      .mockResolvedValueOnce({ packet: 'Original resolved payload' })
      .mockResolvedValueOnce({ packet: 'Unsafe replacement payload' })

    const first = await getDurableIdempotencyAttempt({
      storageKey,
      intent,
      createKey: () => 'original-attempt-key',
      createRequestPayload: resolvePayload,
      validateRequestPayload: isPacket,
    })
    bumpSessionRevision()
    await expect(
      getDurableIdempotencyAttempt({
        storageKey,
        intent,
        createKey: () => 'unsafe-replacement-key',
        createRequestPayload: resolvePayload,
        validateRequestPayload: isPacket,
      }),
    ).resolves.toEqual(first)
    expect(resolvePayload).toHaveBeenCalledTimes(1)
  })

  it('does not restore a completed resolved attempt when retirement storage silently fails', async () => {
    const storageKey = 'apex.idempotency.test-attempt-retirement-noop.v1'
    const intent = { work_item_keys: ['PHX-241'] }
    const validate = (value: unknown): value is { packet: string } =>
      Boolean(
        value &&
          typeof value === 'object' &&
          typeof (value as { packet?: unknown }).packet === 'string',
      )
    const first = await getDurableIdempotencyAttempt({
      storageKey,
      intent,
      createKey: () => 'completed-attempt-key',
      createRequestPayload: async () => ({ packet: 'first' }),
      validateRequestPayload: validate,
    })
    const setItem = vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => undefined)

    try {
      await retireDurableIdempotencyAttempt(storageKey, intent, first.idempotencyKey)
    } finally {
      setItem.mockRestore()
    }

    bumpSessionRevision()
    expect(window.sessionStorage.getItem(storageKey)).toContain('completed-attempt-key')
    await expect(
      getDurableIdempotencyAttempt({
        storageKey,
        intent,
        createKey: () => 'fresh-attempt-key',
        createRequestPayload: async () => ({ packet: 'second' }),
        validateRequestPayload: validate,
      }),
    ).resolves.toEqual({
      idempotencyKey: 'fresh-attempt-key',
      requestPayload: { packet: 'second' },
    })
    expect(window.sessionStorage.getItem(storageKey)).toContain('fresh-attempt-key')
    expect(window.sessionStorage.getItem(storageKey)).not.toContain('completed-attempt-key')
  })

  it('does not expose or replay a resolved payload left by a prior page runtime', async () => {
    const storageKey = 'apex.idempotency.test-attempt-reload.v1'
    const intent = { work_item_keys: ['PRIVATE-241'] }
    const isPacket = (value: unknown): value is { packet: string } =>
      Boolean(
        value &&
          typeof value === 'object' &&
          typeof (value as { packet?: unknown }).packet === 'string',
      )
    await getDurableIdempotencyAttempt({
      storageKey,
      intent,
      createKey: () => 'prior-attempt-key',
      createRequestPayload: async () => ({ packet: 'Prior principal private payload' }),
      validateRequestPayload: isPacket,
    })

    const signature = await idempotencyPayloadSignature(intent)
    const persisted = JSON.parse(window.sessionStorage.getItem(storageKey) ?? 'null')
    persisted.attemptsByPayload[signature].runtimeId = 'prior-page-runtime'
    persisted.attemptsByPayload[signature].sessionRevision = Number.MAX_SAFE_INTEGER
    window.sessionStorage.setItem(storageKey, JSON.stringify(persisted))
    const observeSavedPayload = vi.fn()
    const validateSavedPayload = (value: unknown): value is { packet: string } => {
      observeSavedPayload(value)
      return isPacket(value)
    }
    const createRequestPayload = vi.fn(async () => ({ packet: 'unsafe replacement' }))

    await expect(
      getDurableIdempotencyAttempt({
        storageKey,
        intent,
        createKey: () => 'unsafe-replacement-key',
        createRequestPayload,
        validateRequestPayload: validateSavedPayload,
      }),
    ).rejects.toThrow(SAFE_RETRY_RECOVERY_ERROR)
    expect(observeSavedPayload).not.toHaveBeenCalled()
    expect(createRequestPayload).not.toHaveBeenCalled()
  })

  it('sanitizes and preserves a prior-runtime attempt quarantine during another request', async () => {
    const storageKey = 'apex.idempotency.test-attempt-quarantine-retention.v1'
    const priorIntent = { work_item_keys: ['PRIVATE-888'] }
    const nextIntent = { work_item_keys: ['PUBLIC-999'] }
    const isPacket = (value: unknown): value is { packet: string } =>
      Boolean(
        value &&
          typeof value === 'object' &&
          typeof (value as { packet?: unknown }).packet === 'string',
      )
    await getDurableIdempotencyAttempt({
      storageKey,
      intent: priorIntent,
      createKey: () => 'prior-attempt-key',
      createRequestPayload: async () => ({ packet: 'Prior principal private payload' }),
      validateRequestPayload: isPacket,
    })

    const priorSignature = await idempotencyPayloadSignature(priorIntent)
    const persisted = JSON.parse(window.sessionStorage.getItem(storageKey) ?? 'null')
    persisted.attemptsByPayload[priorSignature].runtimeId = 'prior-page-runtime'
    window.sessionStorage.setItem(storageKey, JSON.stringify(persisted))

    await expect(
      getDurableIdempotencyAttempt({
        storageKey,
        intent: nextIntent,
        createKey: () => 'next-attempt-key',
        createRequestPayload: async () => ({ packet: 'Different safe payload' }),
        validateRequestPayload: isPacket,
      }),
    ).resolves.toEqual({
      idempotencyKey: 'next-attempt-key',
      requestPayload: { packet: 'Different safe payload' },
    })
    const sanitized = window.sessionStorage.getItem(storageKey)
    expect(sanitized).not.toContain('Prior principal private payload')
    expect(sanitized).not.toContain('prior-attempt-key')
    expect(JSON.parse(sanitized ?? 'null').blockedPayloads[priorSignature]).toBeTruthy()

    const observePriorPayload = vi.fn()
    await expect(
      getDurableIdempotencyAttempt({
        storageKey,
        intent: priorIntent,
        createKey: () => 'unsafe-replay-key',
        createRequestPayload: async () => ({ packet: 'unsafe replay payload' }),
        validateRequestPayload: (value): value is { packet: string } => {
          observePriorPayload(value)
          return isPacket(value)
        },
      }),
    ).rejects.toThrow(SAFE_RETRY_RECOVERY_ERROR)
    expect(observePriorPayload).not.toHaveBeenCalled()
  })

  it('fails closed when a resolved attempt write cannot be verified', async () => {
    const storageKey = 'apex.idempotency.test-unverified-attempt.v1'
    const existing = JSON.stringify({
      version: 1,
      attemptsByPayload: {
        ['b'.repeat(64)]: {
          idempotencyKey: 'unrelated-existing-key',
          requestPayload: { request: 'unrelated' },
        },
      },
    })
    window.sessionStorage.setItem(storageKey, existing)
    const setItem = vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => undefined)

    try {
      await expect(
        getDurableIdempotencyAttempt({
          storageKey,
          intent: { request: 'must be safely retryable' },
          createKey: () => 'new-unverified-key',
          createRequestPayload: async () => ({ request: 'must be safely retryable' }),
          validateRequestPayload: (
            value,
          ): value is { request: string } =>
            Boolean(
              value &&
                typeof value === 'object' &&
                typeof (value as { request?: unknown }).request === 'string',
            ),
        }),
      ).rejects.toThrow(SAFE_RETRY_STORAGE_ERROR)
      expect(window.sessionStorage.getItem(storageKey)).toBe(existing)
    } finally {
      setItem.mockRestore()
    }
  })

  it('does not persist a resolved payload across a session transition', async () => {
    const storageKey = 'apex.idempotency.test-session-fence.v1'
    let release!: () => void
    const blocked = new Promise<void>((resolve) => {
      release = resolve
    })
    let markStarted!: () => void
    const started = new Promise<void>((resolve) => {
      markStarted = resolve
    })
    const pending = getDurableIdempotencyAttempt({
      storageKey,
      intent: { request: 'session-bound' },
      createKey: () => 'must-not-persist',
      createRequestPayload: async () => {
        markStarted()
        await blocked
        return { request: 'session-bound' }
      },
      validateRequestPayload: (
        value,
      ): value is { request: string } =>
        Boolean(
          value &&
            typeof value === 'object' &&
            typeof (value as { request?: unknown }).request === 'string',
        ),
    })

    await started
    bumpSessionRevision()

    await expect(
      getDurableIdempotencyAttempt({
        storageKey,
        intent: { request: 'session-bound' },
        createKey: () => 'replacement-session-key',
        createRequestPayload: async () => ({ request: 'session-bound' }),
        validateRequestPayload: (
          value,
        ): value is { request: string } =>
          Boolean(
            value &&
              typeof value === 'object' &&
              typeof (value as { request?: unknown }).request === 'string',
          ),
      }),
    ).resolves.toEqual({
      idempotencyKey: 'replacement-session-key',
      requestPayload: { request: 'session-bound' },
    })

    release()

    await expect(pending).rejects.toThrow(
      'Credentials changed while preparing the request',
    )
    expect(window.sessionStorage.getItem(storageKey)).toContain(
      'replacement-session-key',
    )
    expect(window.sessionStorage.getItem(storageKey)).not.toContain(
      'must-not-persist',
    )
  })

  it('ignores a prior principal resolved payload when storage purge silently fails', async () => {
    const storageKey = 'apex.idempotency.test-failed-session-purge.v1'
    const intent = { work_item_keys: ['PRIVATE-777'] }
    setApiKey('apex_prior_principal')
    const isPacket = (value: unknown): value is { packet: string } =>
      Boolean(
        value &&
          typeof value === 'object' &&
          typeof (value as { packet?: unknown }).packet === 'string',
      )
    await getDurableIdempotencyAttempt({
      storageKey,
      intent,
      createKey: () => 'prior-principal-key',
      createRequestPayload: async () => ({ packet: 'Prior principal private payload' }),
      validateRequestPayload: isPacket,
    })
    const removeItem = vi
      .spyOn(Storage.prototype, 'removeItem')
      .mockImplementation(() => undefined)
    try {
      setApiKey('apex_replacement_principal')
    } finally {
      removeItem.mockRestore()
    }
    expect(window.sessionStorage.getItem(storageKey)).toContain(
      'Prior principal private payload',
    )

    const observeReplacement = vi.fn()
    const validateReplacement = (value: unknown): value is { packet: string } => {
      observeReplacement(value)
      return Boolean(
        value &&
          typeof value === 'object' &&
          (value as { packet?: unknown }).packet === 'Replacement payload',
      )
    }
    await expect(
      getDurableIdempotencyAttempt({
        storageKey,
        intent,
        createKey: () => 'replacement-principal-key',
        createRequestPayload: async () => ({ packet: 'Replacement payload' }),
        validateRequestPayload: validateReplacement,
      }),
    ).resolves.toEqual({
      idempotencyKey: 'replacement-principal-key',
      requestPayload: { packet: 'Replacement payload' },
    })
    expect(observeReplacement).toHaveBeenCalledTimes(1)
    expect(observeReplacement).toHaveBeenCalledWith({ packet: 'Replacement payload' })
    expect(window.sessionStorage.getItem(storageKey)).not.toContain(
      'Prior principal private payload',
    )
  })
})

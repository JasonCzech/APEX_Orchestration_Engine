import { beforeEach, describe, expect, it, vi } from 'vitest'

import { bumpSessionRevision, setApiKey } from '@/auth/keyStorage'

import {
  bindDurableMutationDraft,
  clearDurableMutationDraft,
  initializeDurableMutationDraft,
  SAFE_RETRY_RECOVERY_ERROR,
  SAFE_RETRY_STORAGE_ERROR,
  scopedMutationStorageKey,
  stableMutationFingerprint,
  updateDurableMutationDraft,
} from './durableMutationDraft'

describe('durable mutation identities', () => {
  beforeEach(() => {
    window.sessionStorage.clear()
  })

  it('distinguishes global scope from a project literally named global', () => {
    expect(scopedMutationStorageKey('apex.work-items.create.v1', undefined)).not.toBe(
      scopedMutationStorageKey('apex.work-items.create.v1', 'global'),
    )
  })

  it('keeps delimiter-containing scope/resource pairs isolated', () => {
    expect(scopedMutationStorageKey('prefix', 'project:alpha', 'item')).not.toBe(
      scopedMutationStorageKey('prefix', 'project', 'alpha:item'),
    )
  })

  it('fingerprints equivalent objects independently of property insertion order', () => {
    expect(stableMutationFingerprint({ title: 'one', nested: { b: 2, a: 1 } })).toBe(
      stableMutationFingerprint({ nested: { a: 1, b: 2 }, title: 'one' }),
    )
  })

  it('fails closed when a submitted payload mapping cannot be verified', () => {
    const storageKey = 'apex.work-items.test-unverified'
    const state = initializeDurableMutationDraft(
      storageKey,
      { title: 'Retry-safe item' },
      (value): value is { title: string } =>
        Boolean(
          value &&
            typeof value === 'object' &&
            typeof (value as { title?: unknown }).title === 'string',
        ),
      stableMutationFingerprint,
      () => 'work-item-key',
    )
    const persistedDraft = window.sessionStorage.getItem(storageKey)
    const setItem = vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => undefined)

    try {
      expect(() =>
        bindDurableMutationDraft(storageKey, state, stableMutationFingerprint),
      ).toThrow(SAFE_RETRY_STORAGE_ERROR)
      expect(window.sessionStorage.getItem(storageKey)).toBe(persistedDraft)
    } finally {
      setItem.mockRestore()
    }
  })

  it('persists the exact submitted payload mapping before returning it', () => {
    const storageKey = 'apex.work-items.test-verified'
    const state = initializeDurableMutationDraft(
      storageKey,
      { title: 'Retry-safe item' },
      (value): value is { title: string } =>
        Boolean(
          value &&
            typeof value === 'object' &&
            typeof (value as { title?: unknown }).title === 'string',
        ),
      stableMutationFingerprint,
      () => 'work-item-key',
    )

    const bound = bindDurableMutationDraft(storageKey, state, stableMutationFingerprint)

    expect(JSON.parse(window.sessionStorage.getItem(storageKey) ?? 'null')).toEqual(bound)
    expect(bound.keysByPayload[stableMutationFingerprint(bound.draft)]).toBe('work-item-key')
  })

  it('restores a submitted draft across a same-key semantic refresh', () => {
    const storageKey = 'apex.work-items.test-same-principal-refresh'
    const validate = (value: unknown): value is { title: string } =>
      Boolean(
        value &&
          typeof value === 'object' &&
          typeof (value as { title?: unknown }).title === 'string',
      )
    setApiKey('apex_same_principal_work_item')
    const initial = initializeDurableMutationDraft(
      storageKey,
      { title: 'Original ambiguous work item' },
      validate,
      stableMutationFingerprint,
      () => 'original-work-item-key',
    )
    bindDurableMutationDraft(storageKey, initial, stableMutationFingerprint)

    bumpSessionRevision()
    const restored = initializeDurableMutationDraft(
      storageKey,
      { title: '' },
      validate,
      stableMutationFingerprint,
      () => 'unsafe-replacement-key',
    )
    expect(restored.draft).toEqual({ title: 'Original ambiguous work item' })
    expect(restored.idempotencyKey).toBe('original-work-item-key')
  })

  it('rotates a completed key when storage removal silently fails', () => {
    const storageKey = 'apex.work-items.test-retirement-noop'
    const validate = (value: unknown): value is { title: string } =>
      Boolean(
        value &&
          typeof value === 'object' &&
          typeof (value as { title?: unknown }).title === 'string',
      )
    const first = initializeDurableMutationDraft(
      storageKey,
      { title: 'Same item' },
      validate,
      stableMutationFingerprint,
      () => 'completed-work-item-key',
    )
    const bound = bindDurableMutationDraft(storageKey, first, stableMutationFingerprint)
    const removeItem = vi.spyOn(Storage.prototype, 'removeItem').mockImplementation(() => undefined)

    try {
      clearDurableMutationDraft(storageKey, bound.idempotencyKey)
    } finally {
      removeItem.mockRestore()
    }

    bumpSessionRevision()
    expect(window.sessionStorage.getItem(storageKey)).toContain('completed-work-item-key')
    const next = initializeDurableMutationDraft(
      storageKey,
      { title: 'Same item' },
      validate,
      stableMutationFingerprint,
      () => 'fresh-work-item-key',
    )
    expect(next.idempotencyKey).toBe('fresh-work-item-key')
    expect(window.sessionStorage.getItem(storageKey)).toContain('fresh-work-item-key')
    expect(window.sessionStorage.getItem(storageKey)).not.toContain('completed-work-item-key')
  })

  it('quarantines a submitted prior-page draft without restoring its raw values', () => {
    const storageKey = 'apex.work-items.test-reload-quarantine'
    const priorDraft = { title: 'Prior principal private work item' }
    const validate = (value: unknown): value is { title: string } =>
      Boolean(
        value &&
          typeof value === 'object' &&
          typeof (value as { title?: unknown }).title === 'string',
      )
    const initial = initializeDurableMutationDraft(
      storageKey,
      priorDraft,
      validate,
      stableMutationFingerprint,
      () => 'prior-page-key',
    )
    bindDurableMutationDraft(storageKey, initial, stableMutationFingerprint)

    const persisted = JSON.parse(window.sessionStorage.getItem(storageKey) ?? 'null')
    persisted.runtimeId = 'prior-page-runtime'
    persisted.keyRevision = Number.MAX_SAFE_INTEGER
    window.sessionStorage.setItem(storageKey, JSON.stringify(persisted))

    const recovered = initializeDurableMutationDraft(
      storageKey,
      { title: '' },
      validate,
      stableMutationFingerprint,
      () => 'current-page-key',
    )
    expect(recovered.draft).toEqual({ title: '' })
    expect(recovered.blockedPayloads).toEqual({ __all__: true })
    expect(window.sessionStorage.getItem(storageKey)).not.toContain(
      'Prior principal private work item',
    )

    const reverted = updateDurableMutationDraft(
      storageKey,
      recovered,
      priorDraft,
      stableMutationFingerprint,
      () => 'edited-key',
    )
    expect(() =>
      bindDurableMutationDraft(storageKey, reverted, stableMutationFingerprint),
    ).toThrow(SAFE_RETRY_RECOVERY_ERROR)
  })

  it('does not restore a prior principal draft when session purge silently fails', () => {
    const storageKey = 'apex.work-items.test-failed-session-purge'
    setApiKey('apex_prior_work_item_principal')
    const validate = (value: unknown): value is { title: string } =>
      Boolean(
        value &&
          typeof value === 'object' &&
          typeof (value as { title?: unknown }).title === 'string',
      )
    const initial = initializeDurableMutationDraft(
      storageKey,
      { title: 'Prior principal private work item' },
      validate,
      stableMutationFingerprint,
      () => 'prior-principal-key',
    )
    bindDurableMutationDraft(storageKey, initial, stableMutationFingerprint)
    const removeItem = vi
      .spyOn(Storage.prototype, 'removeItem')
      .mockImplementation(() => undefined)
    try {
      setApiKey('apex_replacement_work_item_principal')
    } finally {
      removeItem.mockRestore()
    }
    expect(window.sessionStorage.getItem(storageKey)).toContain(
      'Prior principal private work item',
    )

    const setItem = vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => undefined)
    let replacement
    try {
      replacement = initializeDurableMutationDraft(
        storageKey,
        { title: '' },
        validate,
        stableMutationFingerprint,
        () => 'replacement-principal-key',
      )
    } finally {
      setItem.mockRestore()
    }

    expect(replacement.draft).toEqual({ title: '' })
    expect(replacement.idempotencyKey).toBe('replacement-principal-key')
    expect(replacement.blockedPayloads).toEqual({})
  })
})

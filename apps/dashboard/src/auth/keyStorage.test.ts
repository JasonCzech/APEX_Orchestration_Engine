import { describe, expect, it, vi } from 'vitest'

import {
  API_KEY_STORAGE_KEY,
  bumpSessionRevision,
  clearApiKey,
  getApiKeyRevision,
  getSessionRevision,
  setApiKey,
  subscribeApiKey,
} from './keyStorage'

const WORK_ITEM_DRAFT_KEY = 'apex.work-items.create.v1:demo'
const IDEMPOTENCY_KEY = 'apex.idempotency.pipeline-launch.v1'
const UNRELATED_SESSION_KEY = 'apex.streaming.resume.thread-1'

function seedSessionStorage() {
  window.sessionStorage.setItem(WORK_ITEM_DRAFT_KEY, '{"title":"private"}')
  window.sessionStorage.setItem(IDEMPOTENCY_KEY, '{"keysByPayload":{"private":"launch-key"}}')
  window.sessionStorage.setItem(UNRELATED_SESSION_KEY, 'event-7')
}

function expectOnlyPrincipalDraftPurged() {
  expect(window.sessionStorage.getItem(WORK_ITEM_DRAFT_KEY)).toBeNull()
  expect(window.sessionStorage.getItem(IDEMPOTENCY_KEY)).toBeNull()
  expect(window.sessionStorage.getItem(UNRELATED_SESSION_KEY)).toBe('event-7')
}

describe('session-bound browser storage', () => {
  it('purges principal-scoped drafts and idempotency keys when credentials change', () => {
    seedSessionStorage()
    setApiKey('apex_first_principal')
    expectOnlyPrincipalDraftPurged()

    seedSessionStorage()
    clearApiKey()
    expectOnlyPrincipalDraftPurged()
  })

  it('preserves mutation recovery state across same-key semantic refreshes', () => {
    seedSessionStorage()
    const before = getSessionRevision()
    bumpSessionRevision()
    expect(getSessionRevision()).toBe(before + 1)
    expect(window.sessionStorage.getItem(WORK_ITEM_DRAFT_KEY)).not.toBeNull()
    expect(window.sessionStorage.getItem(IDEMPOTENCY_KEY)).not.toBeNull()
    expect(window.sessionStorage.getItem(UNRELATED_SESSION_KEY)).toBe('event-7')
  })

  it('does not rotate or purge the session when the identical key is saved again', () => {
    setApiKey('apex_same_key')
    seedSessionStorage()
    const before = getApiKeyRevision()
    const listener = vi.fn()
    const unsubscribe = subscribeApiKey(listener)
    try {
      setApiKey('apex_same_key')
      expect(getApiKeyRevision()).toBe(before)
      expect(listener).not.toHaveBeenCalled()
      expect(window.sessionStorage.getItem(WORK_ITEM_DRAFT_KEY)).not.toBeNull()
      expect(window.sessionStorage.getItem(IDEMPOTENCY_KEY)).not.toBeNull()
    } finally {
      unsubscribe()
    }
  })

  it('purges principal-scoped browser state when another tab rotates the credential', () => {
    seedSessionStorage()
    const listener = vi.fn()
    const unsubscribe = subscribeApiKey(listener)
    try {
      window.dispatchEvent(
        new StorageEvent('storage', {
          key: API_KEY_STORAGE_KEY,
          newValue: 'apex_other_tab',
          storageArea: window.localStorage,
        }),
      )

      expectOnlyPrincipalDraftPurged()
      expect(listener).toHaveBeenCalledWith('apex_other_tab')
    } finally {
      unsubscribe()
    }
  })
})

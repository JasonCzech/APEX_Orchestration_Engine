import { describe, expect, it } from 'vitest'
import { errorMessageOf } from './errors'

describe('errorMessageOf', () => {
  it.each([
    [{ title: 'environment target not found', status: 404 }, 'environment target not found'],
    [{ title: 'Invalid request', detail: 'Approve the environment target first.' }, 'Approve the environment target first.'],
    [{ detail: [{ loc: ['body', 'configurable', 'load_test', 'vusers'], msg: 'Must be positive' }] }, 'configurable.load_test.vusers: Must be positive'],
    [{ detail: { internal: 'not a message' }, title: 'Request rejected' }, 'Request rejected'],
    [{ title: ' ', detail: '' }, 'Could not launch'],
    [null, 'Could not launch'],
  ])('extracts a usable error from %j', (body, expected) => {
    expect(errorMessageOf(body, 'Could not launch')).toBe(expected)
  })
})

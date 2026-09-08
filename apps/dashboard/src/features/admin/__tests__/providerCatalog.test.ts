import { describe, expect, it } from 'vitest'

import {
  buildOptions,
  emptyDraft,
  guidedBaseUrl,
  guidedIssue,
  providerSpec,
  providersForKind,
  splitOptions,
  visibleOptionFields,
  type OptionsDraft,
} from '../providerCatalog'

const JIRA = providerSpec('work_tracking', 'jira')!
const ALIGN = providerSpec('work_tracking', 'jira_align')!

function draftFrom(options: Record<string, unknown>, spec = JIRA): OptionsDraft {
  return { ...emptyDraft(), ...splitOptions(spec, options) }
}

describe('providerCatalog', () => {
  it('offers the registered work-tracking providers and nothing for other kinds', () => {
    expect(providersForKind('work_tracking').map((spec) => spec.id)).toEqual([
      'jira',
      'jira_align',
      'stub',
    ])
    expect(providersForKind('cluster_inventory')).toEqual([])
    expect(providerSpec('work_tracking', 'rally')).toBeNull()
    expect(providerSpec('work_tracking', 'ado')).toBeNull()
    expect(providerSpec('work_tracking', 'JIRA')?.id).toBe('jira')
  })

  it('round-trips known options and preserves the ones it does not know', () => {
    const stored = {
      base_url: 'https://acme.atlassian.net',
      user_email: 'qa@example.com',
      _apex_trusted_private_host: true,
    }
    const draft = draftFrom(stored)

    expect(draft.guided).toEqual({
      base_url: 'https://acme.atlassian.net',
      user_email: 'qa@example.com',
    })
    expect(draft.extras).toEqual({ _apex_trusted_private_host: true })
    expect(buildOptions(JIRA, draft)).toEqual(stored)
  })

  it('omits an empty optional field rather than sending a blank string', () => {
    const draft = draftFrom({ base_url: 'https://acme.atlassian.net', project_key: '' })
    expect(buildOptions(JIRA, draft)).toEqual({ base_url: 'https://acme.atlassian.net' })
  })

  it('never String()-ifies a non-string known option back into the payload', () => {
    // A raw-JSON `"project_key": null` must not become the literal project
    // "null" on the next save; numbers coerce losslessly; null means unset.
    const draft = draftFrom({
      base_url: 'https://acme.atlassian.net',
      user_email: 'qa@example.com',
      project_key: null as unknown as string,
    })
    expect(draft.guided).toEqual({
      base_url: 'https://acme.atlassian.net',
      user_email: 'qa@example.com',
    })
    expect(buildOptions(JIRA, draft)).toEqual({
      base_url: 'https://acme.atlassian.net',
      user_email: 'qa@example.com',
    })

    const numeric = draftFrom({ base_url: 'https://a.jiraalign.com', program_id: 42 }, ALIGN)
    expect(numeric.guided['program_id']).toBe('42')
    expect(buildOptions(ALIGN, numeric)['program_id']).toBe('42')
  })

  it('preserves a garbage-valued known option instead of silently deleting it', () => {
    // A bool project_key fails the adapter closed today. Dropping it on an
    // unrelated save would rebuild the connection UNSCOPED — fail-open.
    const stored = {
      base_url: 'https://acme.atlassian.net',
      user_email: 'qa@example.com',
      project_key: true as unknown as string,
    }
    const draft = draftFrom(stored)
    expect(draft.guided['project_key']).toBeUndefined()
    expect(buildOptions(JIRA, draft)).toEqual(stored)

    // Typing a real value replaces the preserved garbage.
    const repaired = { ...draft, guided: { ...draft.guided, project_key: 'PHX' } }
    expect(buildOptions(JIRA, repaired)['project_key']).toBe('PHX')
  })

  it('names the first unmet requirement and clears once it is filled', () => {
    const empty = emptyDraft()
    expect(guidedIssue(JIRA, empty)).toBe('Jira Cloud requires base url.')

    const partial = draftFrom({ base_url: 'https://acme.atlassian.net' })
    expect(guidedIssue(JIRA, partial)).toBe('Jira Cloud requires user email.')

    const complete = draftFrom({
      base_url: 'https://acme.atlassian.net',
      user_email: 'qa@example.com',
    })
    expect(guidedIssue(JIRA, complete)).toBeNull()
  })

  it('reports the base URL that must also fill the connection column', () => {
    const draft = draftFrom({ base_url: 'https://acme.atlassian.net' })
    expect(guidedBaseUrl(JIRA, draft)).toBe('https://acme.atlassian.net')
    expect(guidedBaseUrl(JIRA, emptyDraft())).toBeNull()
  })

  describe('jira_align auth style', () => {
    it('hides the email field on bearer auth and requires it on basic', () => {
      const bearer = emptyDraft()
      expect(visibleOptionFields(ALIGN, bearer.controls).map((field) => field.key)).toEqual([
        'base_url',
        'program_id',
      ])

      const basic = { ...bearer, controls: { auth_style: 'basic' } }
      expect(visibleOptionFields(ALIGN, basic.controls).map((field) => field.key)).toEqual([
        'base_url',
        'user_email',
        'program_id',
      ])
    })

    it('infers basic auth from a stored email, since that is what selects it', () => {
      expect(draftFrom({ base_url: 'https://a.jiraalign.com' }, ALIGN).controls).toEqual({
        auth_style: 'bearer',
      })
      expect(
        draftFrom({ base_url: 'https://a.jiraalign.com', user_email: 'qa@x.test' }, ALIGN)
          .controls,
      ).toEqual({ auth_style: 'basic' })
    })

    it('drops the email when the form switches back to bearer', () => {
      const basic = draftFrom(
        { base_url: 'https://a.jiraalign.com', user_email: 'qa@x.test' },
        ALIGN,
      )
      const bearer: OptionsDraft = { ...basic, controls: { auth_style: 'bearer' } }
      expect(buildOptions(ALIGN, bearer)).toEqual({ base_url: 'https://a.jiraalign.com' })
    })
  })
})

import { describe, expect, it } from 'vitest'

import { filterGroups, groupEnvironments } from './environmentsLogic'
import { APP_CHECKOUT, APP_SEARCH, ENV_PROD, ENV_SEARCH_DEV, ENV_STAGING } from './environmentsTestHandlers'

const EMPTY_APP = { ...APP_CHECKOUT, id: 'app-empty', name: 'Ledger' }
const groups = groupEnvironments([APP_CHECKOUT, APP_SEARCH, EMPTY_APP], [ENV_STAGING, ENV_PROD, ENV_SEARCH_DEV])
const names = (result: ReturnType<typeof filterGroups>) =>
  result.map(app => `${app.label}:${app.environments.map(env => env.name).join(',')}`)

describe('groupEnvironments', () => {
  it('lists each application once, with every project it is linked to', () => {
    expect(groups.map(group => [group.label, group.projects])).toEqual([
      ['Checkout', ['proj-alpha']],
      ['Ledger', ['proj-alpha']],
      ['Search', ['proj-alpha', 'proj-beta']],
    ])
  })
})

describe('filterGroups', () => {
  it('keeps every application, including empty ones, when nothing is filtered', () => {
    expect(names(filterGroups(groups))).toEqual(['Checkout:production,staging', 'Ledger:', 'Search:dev'])
  })

  it('keeps all environments of an application whose name matches, and only matching ones otherwise', () => {
    expect(names(filterGroups(groups, { search: 'checkout' }))).toEqual(['Checkout:production,staging'])
    expect(names(filterGroups(groups, { search: 'stag' }))).toEqual(['Checkout:staging'])
  })

  it('keeps an empty application when its name matches, but not under a readiness filter', () => {
    expect(names(filterGroups(groups, { search: 'ledger' }))).toEqual(['Ledger:'])
    expect(names(filterGroups(groups, { search: 'ledger', readiness: 'approved' }))).toEqual([])
  })

  it('applies the readiness filter to environments', () => {
    expect(names(filterGroups(groups, { readiness: 'no-target' }))).toEqual(['Search:dev'])
  })

  it('matches any linked project, by ID or display name', () => {
    expect(names(filterGroups(groups, { search: 'proj-beta' }))).toEqual(['Search:dev'])
    const projectName = (id: string) => (id === 'proj-beta' ? 'Discovery team' : '')
    expect(names(filterGroups(groups, { search: 'discovery', projectName }))).toEqual(['Search:dev'])
    expect(names(filterGroups(groups, { search: 'discovery' }))).toEqual([])
  })
})

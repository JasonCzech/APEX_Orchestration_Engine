import { describe, expect, it } from 'vitest'

import { groupEnvironments, projectGroups } from './environmentsLogic'
import { APP_CHECKOUT, APP_SEARCH, ENV_PROD, ENV_SEARCH_DEV, ENV_STAGING } from './environmentsTestHandlers'

const EMPTY_APP = { ...APP_CHECKOUT, id: 'app-empty', name: 'Ledger' }
const groups = groupEnvironments([APP_CHECKOUT, APP_SEARCH, EMPTY_APP], [ENV_STAGING, ENV_PROD, ENV_SEARCH_DEV])
const names = (result: ReturnType<typeof projectGroups>) =>
  result.flatMap(project => project.applications.map(app => `${app.label}:${app.environments.map(env => env.name).join(',')}`))

describe('projectGroups', () => {
  it('keeps every application, including empty ones, when nothing is filtered', () => {
    expect(names(projectGroups(groups))).toEqual(expect.arrayContaining(['Checkout:production,staging', 'Ledger:', 'Search:dev']))
  })

  it('keeps all environments of an application whose name matches, and only matching ones otherwise', () => {
    expect(names(projectGroups(groups, { search: 'checkout' }))).toEqual(['Checkout:production,staging'])
    expect(names(projectGroups(groups, { search: 'stag' }))).toEqual(['Checkout:staging'])
  })

  it('keeps an empty application when its name matches, but not under a readiness filter', () => {
    expect(names(projectGroups(groups, { search: 'ledger' }))).toEqual(['Ledger:'])
    expect(names(projectGroups(groups, { search: 'ledger', readiness: 'approved' }))).toEqual([])
  })

  it('combines search and readiness', () => {
    const result = projectGroups(groups, { readiness: 'no-target' })
    expect(names(result)).toEqual(['Search:dev'])
    expect(result[0]!.environmentCount).toBe(1)
  })

  it('matches the project display name', () => {
    const projectName = (id: string) => (id === APP_SEARCH.project_id ? 'Discovery team' : '')
    expect(names(projectGroups(groups, { search: 'discovery', projectName }))).toEqual(['Checkout:production,staging', 'Ledger:', 'Search:dev'])
    expect(names(projectGroups(groups, { search: 'discovery' }))).toEqual([])
  })
})

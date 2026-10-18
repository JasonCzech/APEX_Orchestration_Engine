import { readFileSync } from 'node:fs'
import { runInNewContext } from 'node:vm'
import { describe, expect, it } from 'vitest'
import { DEFAULT_THEME, THEMES, THEME_STORAGE_KEY } from './useTheme'
import { PREFERENCE_KEY } from './preferences'

const source = readFileSync('public/theme-init.js', 'utf8')

function boot(values: Record<string, string>, dark = false, blocked = false, writeBlocked = false) {
  const dataset: Record<string, string> = {}
  runInNewContext(source, {
    document: { documentElement: { dataset } },
    localStorage: { getItem: (key: string) => {
      if (blocked) throw new Error('Storage denied')
      return values[key] ?? null
    }, setItem: (key: string, value: string) => {
      if (writeBlocked) throw new Error('Storage write denied')
      values[key] = value
    } },
    window: { matchMedia: () => ({ matches: dark }) },
  })
  return dataset
}

describe('theme before application bootstrap', () => {
  it.each(THEMES)('applies saved %s without waiting for runtime config', (theme) => {
    expect(boot({ [THEME_STORAGE_KEY]: theme }).theme).toBe(theme)
  })
  it('honors system theme and reduced motion before rendering', () => {
    const values = { [THEME_STORAGE_KEY]: 'system', [PREFERENCE_KEY]: '{"reduceMotion":true}' }
    expect(boot(values, true)).toEqual({ theme: 'dark', reduceMotion: 'true' })
    expect(boot(values, false).theme).toBe('light')
  })
  it('uses a valid default when storage is blocked or the saved theme is invalid', () => {
    expect(boot({}, false, true).theme).toBe(DEFAULT_THEME)
    expect(boot({ [THEME_STORAGE_KEY]: 'not-a-theme' }).theme).toBe(DEFAULT_THEME)
  })
  it('migrates the retired palette before application bootstrap', () => {
    const values = { [THEME_STORAGE_KEY]: 'apex-light', [PREFERENCE_KEY]: '{"reduceMotion":true}' }
    expect(boot(values)).toEqual({ theme: 'light', reduceMotion: 'true' })
    expect(values[THEME_STORAGE_KEY]).toBe('light')
  })
  it('still applies the migrated palette and motion preference when persistence is denied', () => {
    expect(boot({ [THEME_STORAGE_KEY]: 'apex-light', [PREFERENCE_KEY]: '{"reduceMotion":true}' }, false, false, true))
      .toEqual({ theme: 'light', reduceMotion: 'true' })
  })
  it('retains a valid theme when other preferences are malformed', () => {
    expect(boot({ [THEME_STORAGE_KEY]: 'dark', [PREFERENCE_KEY]: '{' }))
      .toEqual({ theme: 'dark', reduceMotion: 'false' })
  })
})
